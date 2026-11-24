"""Plan disjoint compiler jobs from effective module inputs and checked generation receipts."""

import argparse
from itertools import batched
from graphlib import TopologicalSorter
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import tomllib

import lean_artifact_inventory as inventory
from lean_cache import SHARED_SEED_PLATFORMS, binary_platform
import lean_generation_checkpoints as checkpoints
from lean_generation_index import validate
import lean_compile_layers as compiler
from lean_release_index import write_frame


def digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def package_inputs(root):
    pins = {}
    configs = {}
    packages = {Path("."): "trureturing", Path("Reg"): "reg", inventory.INSPECTOR: "leanInspector",
                inventory.REG_INSPECTOR: "regInspector", inventory.INTERFACE: "leanInspectorInterface"}
    for directory, package in packages.items():
        manifest = root / directory / "lake-manifest.json"
        if manifest.is_file():
            pins[str(directory)] = json.loads(manifest.read_bytes())
        dsl = root / directory / "lakefile.lean"
        if dsl.exists():
            # Executable Lake configuration has no safe syntactic target projection.
            configs[package] = {"lakefile.lean": file_digest(dsl)}
            continue
        config = tomllib.loads((root / directory / "lakefile.toml").read_text())
        configs[config["name"]] = config
    platform = "-".join(binary_platform())
    family = next((group for group in SHARED_SEED_PLATFORMS if platform in group), (platform,))
    environment = digest(["lean-olean-v1", family, (root / "lean-toolchain").read_text().strip(), pins])
    return environment, configs


def module_config(config, name):
    # A different project's library root or executable test driver does not change
    # Lake's compiler arguments for this library. Keep unknown package/target options.
    settings = {key: value for key, value in config.items()
                if key not in {"lean_lib", "lean_exe", "defaultTargets", "testDriver", "lintDriver", "keywords"}}

    def owns(lib):
        patterns = inventory.library_globs(lib)
        return any(inventory.matches_module_glob(name, p) for p in patterns) or any(
            name == r or name.startswith(r + ".") for r in inventory.library_roots(lib))

    settings["lean_lib"] = [lib for lib in config.get("lean_lib", []) if owns(lib)]
    settings["lean_exe"] = [exe for exe in config.get("lean_exe", []) if exe.get("root", exe["name"]) == name]
    return digest(settings)


def module_targets(root, modules=None, graph=None):
    modules = inventory.workspace_modules(root) if modules is None else modules
    graph = inventory.project_imports(root, modules) if graph is None else graph
    environment, configs = package_inputs(root)
    targets = {}
    for name in TopologicalSorter(graph).static_order():
        module = modules[name]
        deps = sorted(graph[name])
        key = digest([environment, module.package, module.source.as_posix(), module.build.as_posix(),
                      module_config(configs[module.package], name), file_digest(root / module.source),
                      [(dep, targets[dep]["key"]) for dep in deps]])
        targets[name] = {"key": key, "deps": deps}
    return targets


def checked_hits(targets, snapshots):
    rows, volumes = {}, {}
    for snapshot in snapshots:
        for name, row in snapshot["targets"].items():
            expected = targets.get(name)
            if not expected or row["key"] != expected["key"] or row["deps"] != expected["deps"]:
                continue
            if name in rows and (rows[name]["sha256"], rows[name]["paths"]) != (row["sha256"], row["paths"]):
                raise ValueError(f"conflicting payloads for {name} with input key {row['key']}")
            volume = row["volume"]
            size = snapshot["volumes"][volume]
            if volume in volumes and volumes[volume] != size:
                raise ValueError("conflicting volume lengths")
            rows[name] = row
            volumes[volume] = size
    # Drop dependents of a corrupt/missing prerequisite and recompute them together.
    valid = {}
    for name in TopologicalSorter({name: target["deps"] for name, target in targets.items()}).static_order():
        if name in rows and all(dep in valid for dep in targets[name]["deps"]):
            valid[name] = rows[name]
    return valid, {row["volume"]: volumes[row["volume"]] for row in valid.values()}


def plan_layers(source, targets, snapshots):
    hits, volumes = checked_hits(targets, snapshots)
    missing = targets.keys() - hits.keys()
    graph = {name: set(row["deps"]) for name, row in targets.items()}
    # Partition the missing induced DAG, then add exactly the inherited inputs
    # needed by each private runner. The common stage closes before any worker.
    missing_graph = {name: graph[name] & missing for name in missing}
    stages = {}
    if missing_graph:
        for stage, layer in compiler.partition(missing_graph, missing).items():
            if layer.owned:
                stages[stage] = {"owned": sorted(layer.owned), "roots": sorted(layer.roots),
                                 "restore": sorted(inventory.import_closure(graph, layer.owned) - layer.owned)}
    return {"source": source, "targets": targets, "stages": stages}, {
        "source": source, "targets": hits, "volumes": volumes}


def batch_frames(root, plan, modules, names, receipt, domain, remote, *, volume_limit=64 * 1024**2):
    from lean_cache_generation import stage_frames
    from lean_generation_index import MAX_VOLUME
    build = root / inventory.PROJECT_BUILD
    result = {"source": plan["source"], "targets": {}, "volumes": {}}
    receipt.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=receipt.parent) as temporary:
        temp = Path(temporary)
        frame_dir = temp / "frames"
        frame_dir.mkdir()
        pending = {}

        def flush():
            write(frame_dir / "frames.json", pending)
            available = shutil.disk_usage(temp).free
            budget = min(MAX_VOLUME, volume_limit, max(1, available // 4))
            batch, metrics = stage_frames(remote, plan, frame_dir, budget)
            checkpoints.save(remote, domain, batch, temp)
            print("LEAN_GENERATION_CHECKPOINT " + json.dumps(metrics), flush=True)
            if set(batch["targets"]) != pending.keys():
                raise ValueError("staged targets differ from generated frames")
            result["targets"].update(batch["targets"])
            result["volumes"].update(batch["volumes"])
            shutil.rmtree(frame_dir)
            frame_dir.mkdir()
            pending.clear()
        used = 0
        for name in sorted(names):
            artifacts = []
            for path in sorted(inventory.lean_artifact_paths(name, modules[name])):
                output = build / path
                if output.is_file() and not output.is_symlink():
                    stat = output.stat()
                    artifacts.append(inventory.Artifact(path, stat.st_mode, stat.st_size, name, "lean"))
            compiler.check_outputs(root / inventory.PROJECT_BUILD, {name: modules[name]},
                                   {name: {a.path for a in artifacts}})
            with (frame_dir / "frame").open("wb") as output:
                row = write_frame(root, modules, name, artifacts, output, None, stable_timestamps=True)
            (frame_dir / "frame").replace(frame_dir / (row["sha256"] + ".tar.gz"))
            pending[name] = {"sha256": row["sha256"], "paths": row["paths"]}
            used += row["bytes"]
            available = shutil.disk_usage(temp).free
            if used >= min(volume_limit, max(1, available // 8)):
                flush()
                used = 0
        if pending:
            flush()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inventory", "plan", "worker"))
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--stage", choices=compiler.STAGES)
    parser.add_argument("--common", type=Path)
    args = parser.parse_args()
    root = args.repository.resolve()
    output = args.output.resolve()
    source = compiler.checkout(root)
    if args.action == "inventory":
        write(output, {"source": source, "targets": module_targets(root)})
        return
    if args.action == "plan":
        remote = checkpoints.remote_for(root)
        targets = module_targets(root)
        base = json.loads(args.base.read_bytes())
        validate(base, complete=False)
        domain, _ = package_inputs(root)
        snapshots = [base]
        if checked_hits(targets, snapshots)[0].keys() != targets.keys():
            snapshots.extend(checkpoints.discover(remote, domain))
        candidates, sizes = checked_hits(targets, snapshots)
        present = {v: size for v, size in sizes.items() if remote.has_volume(v, size)}
        candidates = {n: r for n, r in candidates.items() if r["volume"] in present}
        plan, hits = plan_layers(source, targets, [{"targets": candidates, "volumes": present}])
        plan["domain"] = domain
        write(output / "plan.json", plan)
        write(output / "hits.json", hits)
        workers = [name for name in plan["stages"] if name != "common"]
        if github_output := os.environ.get("GITHUB_OUTPUT"):
            with open(github_output, "a") as out:
                print("shared=" + str("common" in plan["stages"]).lower(), file=out)
                print("workers=" + json.dumps(workers), file=out)
        return
    plan = json.loads(args.plan.read_bytes())
    if plan["source"] != source or not args.stage:
        raise ValueError("worker plan does not match the checked-out source and stage")
    layer = plan["stages"][args.stage]
    hits = json.loads(args.base.read_bytes())
    if args.common and args.common.exists():
        common = json.loads(args.common.read_bytes())
        if common["source"] != source or common["targets"].keys() != set(plan["stages"]["common"]["owned"]):
            raise ValueError("incomplete shared compiler layer")
        hits["targets"].update(common["targets"])
        hits["volumes"].update(common["volumes"])
    modules = inventory.workspace_modules(root)
    remote = checkpoints.remote_for(root)
    work_batches(root, modules, remote, plan, hits, layer, output)
    print(f"LEAN_GENERATION_LAYER stage={args.stage} planned={len(layer['owned'])}", flush=True)


def work_batches(root, modules, remote, plan, hits, layer, output, *, batch_size=32):
    from lean_cache_generation import restore
    graph = {name: set(row["deps"]) for name, row in plan["targets"].items()}
    owned = set(layer["owned"])
    order = TopologicalSorter({name: graph[name] & owned for name in sorted(owned)}).static_order()
    result = {"source": plan["source"], "targets": {}, "volumes": {}}
    installed = set()
    for batch in batched(order, batch_size):
        targets = set(batch)
        required = inventory.import_closure(graph, batch) - targets
        missing = required - installed
        if missing:
            rows = {name: hits["targets"][name] for name in missing}
            if any(any(row[k] != plan["targets"][n][k] for k in ("key", "deps")) for n, row in rows.items()):
                raise ValueError("worker dependency key differs from plan")
            subset = {"source": plan["source"], "targets": rows,
                      "volumes": {row["volume"]: hits["volumes"][row["volume"]] for row in rows.values()}}
            restored = restore(remote, subset, plan, missing, root / inventory.PROJECT_BUILD)
            if restored["restored"] != len(missing):
                raise ValueError("worker could not restore the planned dependency closure")
            compiler.compile_roots(root, modules, sorted(compiler.terminal(missing, graph)), no_build=True)
            installed.update(missing)
        compiler.compile_roots(root, modules, sorted(compiler.terminal(targets, graph)))
        receipt = batch_frames(root, plan, modules, batch, output, plan["domain"], remote)
        for field in ("targets", "volumes"):
            result[field].update(receipt[field])
            hits[field].update(receipt[field])
        installed.update(batch)
        free = shutil.disk_usage(root).free
        if free < 4 * 1024**3:
            # Only evict authenticated compiler outputs owned by this private worker.
            # The next batch will recover the subset of verified frames it needs.
            for name in installed:
                for path in hits["targets"][name]["paths"]:
                    (root / inventory.PROJECT_BUILD / path).unlink(missing_ok=True)
            installed.clear()
    write(output, result)


if __name__ == "__main__":
    main()
