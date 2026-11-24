"""Stage, select and consume the same immutable generation from CI and Release."""

import argparse
from contextlib import closing
import datetime
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile

import lean_cache_release as host
import lean_release_index as frames
from lean_generation_index import MAX_VOLUME, SCHEMA, assemble, encode, matches, read, sha, validate
from lean_generation_remote import GitHub


def latest(remote, transport, output, marker_output=None):
    revision, marker = remote.current()
    if marker_output:
        marker_output.write_text(revision)
    if marker is None:
        output.unlink(missing_ok=True)
        return False
    index = remote.get_index(marker["index"], output, transport)
    if index["source"] != marker["source"] or index.get("schema") != SCHEMA:
        raise ValueError("published marker and generation index disagree")
    validate(index, complete=True)
    return True


def stage_indexes(remote, record, directory):
    encoded = encode(record)
    digest = sha(encoded)
    index_path = Path(directory) / "index"
    index_path.write_bytes(encoded)
    remote.stage_indexes(digest, index_path)
    for transport in ("ci", "release"):
        if remote.get_index(digest, Path(directory) / transport, transport) != record:
            raise ValueError(f"{transport} readback differs from staged index")
    return digest


def publish(remote, plan, base, receipts, expected):
    current, marker = remote.current()
    if current != expected:
        raise ValueError("stale generation publisher")
    if marker and (base is None or sha(encode(base)) != marker["index"]):
        raise ValueError("publisher base differs from its pinned generation")
    result = assemble(plan, base, receipts)
    for volume, size in result["volumes"].items():
        remote.volume(volume, size)
    with tempfile.TemporaryDirectory() as directory:
        digest = stage_indexes(remote, result, Path(directory))
    new = {"schema": SCHEMA, "source": plan["source"], "index": digest}
    revision = remote.advance(expected, new)
    return dict(marker=revision, source=plan["source"], ci_index_sha256=digest,
                release_index_sha256=digest, targets=len(result["targets"]), inherited_frame_bytes_read=0)


def stage_frames(remote, plan, directory, limit):
    """At most one additional physical volume is allocated beside the worker frames."""
    if not 0 < limit <= MAX_VOLUME:
        raise ValueError("invalid physical volume budget")
    inventory = read(directory / "frames.json")
    rows, sizes = {}, {}
    count = uploaded = 0
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        path = Path(temporary) / "volume"
        with path.open("w+b") as output:
            pending = []

            def finish():
                nonlocal count, uploaded
                size = output.tell()
                if not size:
                    return
                output.flush()
                volume = host.archive_sha(path, remote.deadline)
                if remote.ensure_volume(volume, size, path):
                    count += 1
                    uploaded += size
                for name in pending:
                    rows[name]["volume"] = volume
                sizes[volume] = size
                pending.clear()
                output.seek(0)
                output.truncate()

            for name, descriptor in sorted(inventory.items()):
                digest = descriptor["sha256"]
                if not frames.valid_digest(digest) or name not in plan["targets"]:
                    raise ValueError(f"invalid staged input: {name}")
                frame = directory / (digest + ".tar.gz")
                size = frame.stat().st_size
                if not 0 < size <= MAX_VOLUME:
                    raise ValueError(f"frame exceeds physical volume capacity: {name}")
                if output.tell() + size > limit:
                    finish()
                paths = descriptor["paths"]
                with tarfile.open(frame, "r|gz") as archive:
                    members = []
                    for entry in archive:
                        if not entry.isfile():
                            raise ValueError(f"invalid frame entry: {entry.name}")
                        frames.safe_path(entry.name)
                        members.append(entry.name)
                if sorted(members) != sorted(paths) or len(set(paths)) != len(paths):
                    raise ValueError(f"frame inventory disagrees with archive: {name}")
                start = output.tell()
                with frame.open("rb") as source:
                    checksum = hashlib.sha256()
                    while block := source.read(1024 * 1024):
                        checksum.update(block)
                        output.write(block)
                if checksum.hexdigest() != digest or output.tell() - start != size:
                    raise ValueError(f"staged frame checksum or size mismatch: {name}")
                target = plan["targets"][name]
                rows[name] = {"key": target["key"], "deps": target["deps"], "paths": paths,
                              "offset": start, "bytes": size, "sha256": digest}
                pending.append(name)
            finish()
    result = {"source": plan["source"], "targets": rows, "volumes": sizes}
    validate(result, complete=False)
    return result, dict(uploaded_bytes=uploaded, new_volumes=count, targets=len(rows))


def consecutive_frames(names, rows):
    group = []
    for name in names:
        row = rows[name]
        if group:
            previous = rows[group[-1]]
            if row["volume"] != previous["volume"] or row["offset"] != previous["offset"] + previous["bytes"]:
                yield group
                group = []
        group.append(name)
    if group:
        yield group


def install_frame(reader, row, name, destination, deadline):
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        unpacked = Path(temporary)
        paths = set()
        frames.extract_frame(reader, {**row, "module": name}, unpacked, paths, deadline)
        missing = set()
        for path in paths:
            if not (unpacked / path).is_file():
                continue
            target = destination / path
            if host.has_unsafe_parent(destination, path) or target.is_symlink():
                raise ValueError("unsafe local build path: " + str(target))
            if not target.exists():
                missing.add(path)
            elif not target.is_file() or host.archive_sha(target, deadline) != host.archive_sha(unpacked / path, deadline):
                return False
        host.install_scoped(destination, unpacked, missing, deadline)
        return True


def restore(remote, index, plan, names, destination):
    validate(index, complete=False)
    rows = index["targets"]
    planned = plan["targets"]
    wanted = set()
    pending = list(planned if names is None else names)
    if names is not None:
        pending.extend("report/" + name for name in names if "report/" + name in planned)
    while pending:
        name = pending.pop()
        if name in planned and name not in wanted:
            wanted.add(name)
            pending.extend(planned[name]["deps"])
    selected = sorted((name for name in wanted & rows.keys() if matches(rows[name], planned[name])),
                      key=lambda name: (rows[name]["volume"], rows[name]["offset"]))
    if destination.is_symlink() or destination.parent.is_symlink():
        raise ValueError("generation reader requires a private build directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    assets = {}
    restored = transferred = conflicts = 0
    # One request per contiguous range. Only one unpacked frame must fit next
    # to the installed build even when thousands of targets share a volume.
    for group in consecutive_frames(selected, rows):
        row = rows[group[0]]
        volume = row["volume"]
        if volume not in assets:
            assets[volume] = remote.volume(volume, index["volumes"][volume])
        length = sum(rows[name]["bytes"] for name in group)
        with closing(remote.blocks(assets[volume], row["offset"], length)) as stream:
            reader = frames.BlockInput(stream)
            for name in group:
                if install_frame(reader, rows[name], name, destination, remote.deadline):
                    restored += 1
                else:
                    conflicts += 1
            if reader.read(1):
                raise ValueError("volume range exceeds its planned frames")
        transferred += length
    return {"source": index["source"], "requested": len(wanted), "restored": restored,
            "deferred": conflicts, "transferred_bytes": transferred}


def checkout_plan(root):
    from lean_generation_facets import all_targets
    return {"targets": all_targets(root)}


def fetch_published(root, targets, deadline, *, refresh_stale):
    """Return None before the first cutover so ordinary fetch can use its legacy seed."""
    remote = GitHub(root, deadline)
    with tempfile.TemporaryDirectory() as metadata:
        path = Path(metadata) / "index"
        if not latest(remote, "release", path):
            return None
        index = read(path)
    destination = root / ".lake/build"
    if destination.parent.is_symlink() or destination.is_symlink():
        raise ValueError("generation reader requires a private build directory")
    destination.parent.mkdir(exist_ok=True)
    if not targets and destination.is_dir() and any(destination.iterdir()) and not refresh_stale:
        host.receipt("fetch", "skipped", reason="local build already exists")
        return 0
    # Full refresh retains the old directory until every selected frame was
    # authenticated and installed. Scoped consumers only add missing artifacts.
    with tempfile.TemporaryDirectory(dir=destination.parent) as directory:
        stage = Path(directory)
        output = destination if targets else stage / "build"
        result = restore(remote, index, checkout_plan(root), targets or None, output)
        if not result["restored"]:
            host.receipt("fetch", "miss", reason="no matching published generation targets", **result)
            return 1
        if not targets:
            stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
            (output / ".release-refreshed-at").write_text(stamp + "\n")
            backup = stage / "previous"
            try:
                if destination.exists():
                    destination.rename(backup)
                output.rename(destination)
            except BaseException:
                if backup.exists() and not destination.exists():
                    backup.rename(destination)
                raise
        host.receipt("fetch", "scoped" if targets else "unpacked", mode="partition",
                     producer_commit_sha=index["source"], **result)
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    get = commands.add_parser("latest")
    get.add_argument("--transport", choices=("ci", "release"), required=True)
    get.add_argument("--output", type=Path, required=True)
    get.add_argument("--marker-output", type=Path)
    load = commands.add_parser("restore")
    load.add_argument("--index", type=Path, required=True)
    load.add_argument("--targets-file", type=Path)
    load.add_argument("--plan", type=Path)
    load.add_argument("--destination", type=Path)
    pack = commands.add_parser("stage-frames")
    pack.add_argument("--plan", type=Path, required=True)
    pack.add_argument("--frames", type=Path, required=True)
    pack.add_argument("--output", type=Path, required=True)
    pack.add_argument("--volume-bytes", type=int, default=64 * 1024 * 1024)
    put = commands.add_parser("stage-volumes")
    put.add_argument("--index", type=Path, required=True)
    put.add_argument("--directory", type=Path, required=True)
    verify = commands.add_parser("probe")
    verify.add_argument("--index", type=Path, required=True)
    verify.add_argument("--output", type=Path, required=True)
    commit = commands.add_parser("commit")
    commit.add_argument("--plan", type=Path, required=True)
    commit.add_argument("--base", type=Path)
    commit.add_argument("--receipts", type=Path, required=True)
    commit.add_argument("--expected-marker", type=Path, required=True)
    args = parser.parse_args()
    root = args.repository.resolve()
    remote = GitHub(root, host.operation_deadline())
    if args.command == "latest":
        return 0 if latest(remote, args.transport, args.output, args.marker_output) else 3
    if args.command == "restore":
        names = args.targets_file.read_text().split() if args.targets_file else None
        result = restore(remote, read(args.index), read(args.plan) if args.plan else checkout_plan(root),
                         names, args.destination or root / ".lake/build")
    elif args.command == "commit":
        result = publish(remote, read(args.plan), read(args.base) if args.base else None,
                         (read(path) for path in sorted(args.receipts.rglob("*.json"))),
                         args.expected_marker.read_text().strip())
    elif args.command == "stage-frames":
        index, result = stage_frames(remote, read(args.plan), args.frames, args.volume_bytes)
        args.output.write_bytes(encode(index))
    elif args.command == "stage-volumes":
        index = read(args.index)
        validate(index, complete=False)
        count = total = 0
        for digest, size in index["volumes"].items():
            path = args.directory / digest
            if path.stat().st_size != size or host.archive_sha(path, remote.deadline) != digest:
                raise ValueError("physical volume digest or size mismatch")
            if remote.ensure_volume(digest, size, path):
                total += size
                count += 1
        result = dict(uploaded_bytes=total, new_volumes=count)
    else:
        index = read(args.index)
        validate(index, complete=False)
        confirmed = {v: n for v, n in index["volumes"].items() if remote.has_volume(v, n)}
        rows = {n: r for n, r in index["targets"].items() if r["volume"] in confirmed}
        args.output.write_bytes(encode({**index, "targets": rows, "volumes": confirmed}))
        result = dict(confirmed_targets=len(rows))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
