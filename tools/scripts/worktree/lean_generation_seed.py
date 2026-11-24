"""Migrate a pinned protected-dev Actions shard index into checked target frames."""

import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import zipfile

import lean_actions_shards as legacy
import lean_artifact_inventory as inventory
import lean_compile_layers as compiler
import lean_generation_checkpoints as checkpoints
from lean_cache_generation import stage_frames
from lean_cache_release_io import archive_sha
from lean_generation_facets import all_targets, facet_paths, report_has_inputs, use_known_projections
from lean_generation_index import matches, read
from lean_generation_plan import module_targets, package_inputs, write
from lean_release_index import safe_path, write_frame


def pinned_seed(seed, directory):
    descriptor = read(directory / "seed.json")
    path = directory / "index.json"
    if (compiler.checkout(seed) != descriptor["commit"]
            or archive_sha(path, None) != descriptor["index_sha256"]):
        raise ValueError("seed checkout or index digest differs from selected producer")
    identity = legacy.binding(seed, "complete")
    record = legacy.read_index(path, identity, "complete")
    owner = {"ref": "refs/heads/dev", "commit": descriptor["commit"],
             "run": str(descriptor["run"]), "attempt": str(descriptor["attempt"])}
    if record is None or record["owner"] != owner:
        raise ValueError("seed index differs from pinned protected-dev producer")
    addresses = legacy.index_addresses(record["binding"])
    key = addresses["snapshot_prefix"] + owner["run"] + "-" + owner["attempt"]
    if key != descriptor["key"] or addresses["prefix"] != descriptor["prefix"]:
        raise ValueError("seed address does not bind selected producer and configuration")
    return descriptor, record


def shareable(seed, root, record):
    modules = inventory.workspace_modules(seed)
    graph = inventory.project_imports(seed, modules)
    owners = {n: key for key, group in record["layout"].items() for shard in group for n in shard}
    required = legacy.required_outputs(seed, modules, graph, owners)
    if required != record["required"]:
        raise ValueError("seed required outputs differ from its producer's target inventory")
    before = all_targets(seed, module_targets(seed, modules, graph))
    after = all_targets(root)
    groups = defaultdict(dict)
    for name in before.keys() & after.keys():
        original, target = before[name], after[name]
        if (not matches(original, target)
                and not matches(original.get("empty_projection", original), target.get("empty_projection", target))):
            continue
        if name.startswith("native/"):
            group = legacy.EXTRAS
            paths = facet_paths(name)
        else:
            module = name.removeprefix("report/")
            if (module not in owners
                    or record["sources"][module] != archive_sha(seed / modules[module].source, None)):
                continue
            group = owners[module]
            paths = (facet_paths(name) if name.startswith("report/") else
                     inventory.lean_artifact_paths(module, modules[module]))
        if group in record["entries"]:
            groups[group][name] = set(map(str, paths))
    return before, after, modules, groups


def extract_shard(archive_path, digest, fingerprint, expected, selected, output):
    """Validate every old entry in the fingerprint; materialize only reusable paths."""
    if archive_sha(archive_path, None) != digest:
        raise ValueError("old payload digest mismatch")
    observed = {}
    with tarfile.open(archive_path, "r|gz") as archive:
        for entry in archive:
            name = str(safe_path(entry.name))
            if not entry.isfile() or name in observed:
                raise ValueError("duplicate or non-regular legacy artifact")
            checksum = hashlib.sha256()
            mode = entry.mode & 0o777
            destination = None
            if name in selected:
                if entry.size > shutil.disk_usage(output).free:
                    raise OSError("insufficient disk for a migrated seed frame")
                path = output / name
                path.parent.mkdir(parents=True, exist_ok=True)
                destination = path.open("xb")
            try:
                with archive.extractfile(entry) as source:
                    while block := source.read(1024 * 1024):
                        checksum.update(block)
                        if destination:
                            destination.write(block)
            finally:
                if destination:
                    destination.close()
            if destination:
                path.chmod(mode)
            observed[name] = (mode, checksum.hexdigest())
    actual = legacy.digest([(n, *observed[n]) for n in sorted(observed)])
    if actual != fingerprint or not set(expected) <= observed.keys():
        raise ValueError("seed fingerprint or required path mismatch")
    return observed.keys() & selected


def pack_target(scratch, name, paths, available, modules, directory):
    existing = sorted(paths & available)
    build = scratch / inventory.PROJECT_BUILD
    if name.startswith(("native/", "report/")):
        if len(existing) != len(paths):
            raise ValueError("seed facet missing required outputs")
        file = build / existing[0]
        if name.startswith("native/") and (file.stat().st_size == 0 or not file.stat().st_mode & 0o111):
            raise ValueError("seed native executable missing")
        if name.startswith("report/"):
            with zipfile.ZipFile(file) as report:
                if not report.namelist() or report.testzip():
                    raise ValueError("seed report is empty or corrupt")
    else:
        compiler.check_outputs(build, {name: modules[name]}, {name: {safe_path(p) for p in existing}})
    artifacts = []
    for path in sorted(map(safe_path, existing)):
        stat = (build / path).stat()
        artifacts.append(inventory.Artifact(path, stat.st_mode, stat.st_size, None, "lean"))
    with (directory / "frame").open("wb") as stream:
        row = write_frame(scratch, {}, None, artifacts, stream, None, stable_timestamps=True)
    (directory / "frame").replace(directory / (row["sha256"] + ".tar.gz"))
    return {"sha256": row["sha256"], "paths": existing}


def checkpoint_rows(remote, domain, targets):
    rows, volumes = {}, {}
    present = {}
    snapshots = list(checkpoints.discover(remote, domain))
    use_known_projections(targets, snapshots)
    for snapshot in snapshots:
        for name, row in snapshot["targets"].items():
            if name not in targets or not matches(row, targets[name]):
                continue
            volume = row["volume"]
            size = snapshot["volumes"][volume]
            if volume not in present:
                present[volume] = remote.has_volume(volume, size)
            if not present[volume]:
                continue
            if name in rows and rows[name]["sha256"] != row["sha256"]:
                raise ValueError("conflicting prior checkpoint for seed migration: " + name)
            rows[name] = row
            volumes[volume] = size
    return rows, {row["volume"]: volumes[row["volume"]] for row in rows.values()}


def request_payloads(digests, protocol):
    print(json.dumps({"request": digests}), file=protocol, flush=True)
    answer = json.loads(sys.stdin.readline())
    if not isinstance(answer, list) or not set(answer) <= set(digests):
        raise ValueError("unexpected legacy payload download acknowledgment")
    return set(answer)


def migrate(root, seed, directory, request):
    descriptor, old = pinned_seed(seed, directory)
    original, targets, modules, grouped = shareable(seed, root, old)
    source = compiler.checkout(root)
    plan = {"source": source, "targets": targets}
    domain, _ = package_inputs(root)
    remote = checkpoints.remote_for(root)
    rows, volumes = checkpoint_rows(remote, domain, targets)
    resumed = len(rows)
    pending = [(k, {n: p for n, p in values.items() if n not in rows}) for k, values in sorted(grouped.items())]
    pending = [(k, values) for k, values in pending if values]
    uploaded = reused = source_bytes = 0
    # The transport serves only this outstanding batch. Release the old gzip
    # archive as soon as each group's canonical frames have been produced.
    for begin in range(0, len(pending), 8):
        batch = pending[begin:begin + 8]
        downloaded = request(list(dict.fromkeys(old["entries"][k] for k, _ in batch)))
        with tempfile.TemporaryDirectory(dir=directory) as temp:
            temp = Path(temp)
            frames_dir = temp / "frames"
            frames_dir.mkdir()
            prepared = {}
            for key, candidates in batch:
                digest = old["entries"][key]
                if digest not in downloaded:
                    continue
                archive = root / legacy.transport_payload(digest)
                with tempfile.TemporaryDirectory(dir=temp) as unpacked:
                    scratch = Path(unpacked)
                    build = scratch / inventory.PROJECT_BUILD
                    build.mkdir(parents=True)
                    source_bytes += archive.stat().st_size
                    try:
                        paths = extract_shard(archive, digest, old["fingerprints"][key], old["required"].get(key, []),
                                              set.union(*candidates.values()), build)
                    except (ValueError, EOFError, tarfile.TarError) as error:
                        print(f"seed payload {digest} rejected: {error}", file=sys.stderr)
                        continue
                    for name, expected in candidates.items():
                        try:
                            prior, current = original[name], targets[name]
                            if "empty_projection" in prior or "empty_projection" in current:
                                bundle = build / facet_paths(name)[0]
                                if not bundle.exists():
                                    continue
                                if not report_has_inputs(bundle, name.removeprefix("report/")):
                                    prior = prior.get("empty_projection", prior)
                                    current = current.get("empty_projection", current)
                                if not matches(prior, current):
                                    continue
                                targets[name] = current
                            prepared[name] = pack_target(scratch, name, expected, paths, modules, frames_dir)
                        except (ValueError, EOFError, zipfile.BadZipFile, KeyError) as error:
                            print(f"seed target {name} rejected: {error}", file=sys.stderr)
            if prepared:
                write(frames_dir / "frames.json", prepared)
                limit = min(64 * 1024**2, max(1, shutil.disk_usage(temp).free // 4))
                receipt, stats = stage_frames(remote, plan, frames_dir, limit)
                checkpoints.save(remote, domain, receipt, temp)
                rows.update(receipt["targets"])
                volumes.update(receipt["volumes"])
                reused += len(prepared)
                uploaded += stats["uploaded_bytes"]
        for digest in downloaded:
            (root / legacy.transport_payload(digest)).unlink(missing_ok=True)
    write(directory / "base.json", {"source": source, "targets": rows, "volumes": volumes})
    return {"seed": descriptor, "target_sha": source, "reused": reused, "resumed": resumed,
            "missing": len(targets) - len(rows), "uploaded_bytes": uploaded, "seed_bytes_read": source_bytes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-root", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    # Keep the subprocess pipe strictly request/response even when native tools,
    # npm or the Actions SDK write their own progress messages to stdout.
    with os.fdopen(os.dup(sys.stdout.fileno()), "w") as protocol:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        result = migrate(Path.cwd(), args.seed_root.resolve(), args.directory.resolve(),
                         lambda digests: request_payloads(digests, protocol))
        print(json.dumps({"result": result}), file=protocol, flush=True)


if __name__ == "__main__":
    main()
