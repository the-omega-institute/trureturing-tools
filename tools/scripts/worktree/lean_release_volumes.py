"""Physical volumes for incremental Lean Releases, with a filesystem Release transport."""

import argparse
from contextlib import contextmanager
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import lean_release_index as index
from lean_compile_layers import checkout
from lean_cache_release_io import MultipartArchiveOutput, archive_sha
from lean_artifact_inventory import lake_configuration

SCHEMA = "lean-release-volumes-v1"
MAX_VOLUMES = 100
VOLUME_BYTES = 1536 * 1024 * 1024
MANIFEST = "manifest.json"
INDEX = "index.json.gz"


def json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def snapshot_path(store, tag):
    if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", tag):
        raise ValueError("invalid snapshot tag")
    return store / "snapshots" / tag


@contextmanager
def store_lock(store, *, write):
    store.mkdir(parents=True, exist_ok=True)
    with (store / ".lock").open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        yield


def read_snapshot(store, tag):
    directory = snapshot_path(store, tag)
    manifest = json.loads((directory / MANIFEST).read_bytes())
    if (manifest.get("schema") != SCHEMA or manifest.get("tag") != tag
            or not all(index.valid_digest(manifest.get(key)) for key in
                       ("index_sha256", "archive_sha256", "lake_inputs_sha256"))
            or not re.fullmatch(r"[0-9a-f]{40}", manifest.get("producer_commit_sha", ""))
            or type(manifest.get("archive_bytes")) is not int):
        raise ValueError("invalid volume snapshot manifest")
    path = directory / INDEX
    if path.stat().st_size > 32 * 1024 * 1024 or archive_sha(path, None) != manifest["index_sha256"]:
        raise ValueError("volume index checksum or size mismatch")
    return manifest, read_tree(path, manifest)


def read_tree(path, manifest):
    with gzip.open(path, "rb") as stream:
        raw = stream.read(128 * 1024 * 1024 + 1)
    if len(raw) > 128 * 1024 * 1024:
        raise ValueError("volume index exceeds maximum size")
    tree = json.loads(raw)
    rows = index.validate_members(tree["members"], manifest["archive_bytes"])
    sizes = tree["volumes"]
    if (not isinstance(sizes, dict) or not 1 <= len(sizes) <= MAX_VOLUMES or
            any(not index.valid_digest(digest) or type(size) is not int or not 0 < size <= VOLUME_BYTES
                for digest, size in sizes.items())):
        raise ValueError("invalid physical volume inventory")
    if sum(sizes.values()) > 2 * manifest["archive_bytes"]:
        raise ValueError("physical volume inventory exceeds compaction bound")
    used = set()
    for row, spans in zip(rows, tree["locations"], strict=True):
        total = 0
        for digest, start, count in spans:
            if (digest not in sizes or type(start) is not int or start < 0 or type(count) is not int
                    or count <= 0 or start + count > sizes[digest]):
                raise ValueError("invalid physical volume range")
            used.add(digest)
            total += count
        if total != row["bytes"]:
            raise ValueError("incomplete physical member")
    if used != sizes.keys():
        raise ValueError("unreferenced volume in snapshot")
    return tree


def store_asset(store, part, source):
    """A GitHub transport can map each digest to its own immutable one-asset Release."""
    path = store / "volumes" / part["sha256"]
    path.parent.mkdir(exist_ok=True)
    if path.exists():
        if path.stat().st_size != part["bytes"] or archive_sha(path, None) != part["sha256"]:
            raise ValueError("existing content-addressed volume is corrupt")
        return 0
    if source.stat().st_size != part["bytes"] or archive_sha(source, None) != part["sha256"]:
        raise ValueError("staged volume checksum mismatch")
    os.replace(source, path)
    return part["bytes"]


def copy_range(source, offset, count, output):
    with source.open("rb") as stream:
        stream.seek(offset)
        while count:
            block = stream.read(min(count, 1024 * 1024))
            if not block:
                raise ValueError("truncated physical volume")
            output.write(block)
            count -= len(block)


class HashOutput:
    def __init__(self):
        self.digest = hashlib.sha256()
        self.size = 0

    def tell(self):
        return self.size

    def write(self, block):
        self.digest.update(block)
        self.size += len(block)
        return len(block)


def pack_volumes(root, stage, previous, chunk_bytes, save_part, deadline=None):
    """Index a logical snapshot while staging at most one confirmed physical volume."""
    if not 0 < chunk_bytes <= VOLUME_BYTES:
        raise ValueError("invalid physical volume size")
    config = lake_configuration(root)
    modules, groups = index.frame_inventory(root)
    rows, sizes = [], {}
    compact = False
    compatible = previous is not None and previous[0]["lake_inputs_sha256"] == config
    if compatible:
        logical = HashOutput()
        for name, artifacts in groups:
            rows.append(index.write_frame(root, modules, name, artifacts, logical, deadline, stable_timestamps=True))
        logical_bytes, logical_sha = logical.size, logical.digest.hexdigest()
        old_tree = previous[1]
        reused = {row["sha256"]: spans for row, spans in zip(old_tree["members"], old_tree["locations"], strict=True)}
        locations = [reused.get(row["sha256"]) for row in rows]
        changed = sum(row["bytes"] for row, spans in zip(rows, locations, strict=True) if spans is None)
        sizes = {key: old_tree["volumes"][key] for spans in locations if spans for key, _, _ in spans}
        compact = (len(sizes) + (changed + chunk_bytes - 1) // chunk_bytes > MAX_VOLUMES
                   or sum(sizes.values()) + changed > 2 * logical_bytes)
        if compact:
            sizes.clear()
            locations = [None] * len(rows)
    else:
        if not groups:
            raise ValueError("member index does not cover archive")
        locations = [None] * len(groups)

    parts = []
    if any(spans is None for spans in locations):
        with MultipartArchiveOutput(stage, "volume", chunk_bytes, deadline, on_part=save_part) as output:
            for pos, (name, artifacts) in enumerate(groups):
                if locations[pos] is not None:
                    continue
                row = index.write_frame(root, modules, name, artifacts, output, deadline, stable_timestamps=True)
                if compatible:
                    row["offset"] = rows[pos]["offset"]
                    if row != rows[pos]:
                        raise ValueError("compiler frame changed between measurement and upload")
                else:
                    rows.append(row)
        parts = output.parts
        if not compatible:
            logical_bytes, logical_sha = output.archive_bytes, output.archive_hash.hexdigest()

    index.validate_members(rows, logical_bytes)
    if not 0 < logical_bytes <= MAX_VOLUMES * VOLUME_BYTES:
        raise ValueError("invalid logical volume archive size")
    written = 0
    for pos, row in enumerate(rows):
        if locations[pos] is None:
            locations[pos] = [[part["sha256"], start, count]
                              for part, start, count in index.volume_ranges(parts, written, row["bytes"])]
            written += row["bytes"]
    sizes.update({part["sha256"]: part["bytes"] for part in parts})
    tree = {"members": rows, "locations": locations, "volumes": sizes}
    contents = {"archive_bytes": logical_bytes, "archive_sha256": logical_sha, "lake_inputs_sha256": config}
    peak = max((part["bytes"] for part in parts), default=0)
    return contents, tree, written, compact, peak


def write_tree(path, tree):
    raw = json_bytes(tree)
    if len(raw) > 128 * 1024 * 1024:
        raise ValueError("volume index exceeds maximum size")
    with path.open("wb") as target:
        with gzip.GzipFile(filename="", fileobj=target, mode="wb", mtime=0) as encoded:
            encoded.write(raw)
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("compressed volume index exceeds maximum size")


def publish(store, tag, root, base=None, chunk_bytes=VOLUME_BYTES):
    store.mkdir(parents=True, exist_ok=True)
    destination = snapshot_path(store, tag)
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=store) as temporary, store_lock(store, write=True):
        stage = Path(temporary)
        producer = checkout(root)
        existing = read_snapshot(store, tag) if destination.exists() else None
        previous = existing or (read_snapshot(store, base) if base else None)
        uploaded = []

        def save_part(part, path):
            if existing:
                raise ValueError("snapshot tag is already bound to another build")
            if store_asset(store, part, path):
                uploaded.append(part)

        contents, tree, written, compact, peak = pack_volumes(root, stage, previous, chunk_bytes, save_part)
        if existing:
            if existing[0]["producer_commit_sha"] != producer or any(existing[0][k] != v for k, v in contents.items()):
                raise ValueError("snapshot tag is already bound to another build")
            return {"payload_bytes": 0, "metadata_bytes": 0, "new_volumes": [], "existing": True}
        for digest, size in tree["volumes"].items():
            if (store / "volumes" / digest).stat().st_size != size:
                raise ValueError("referenced physical volume is missing or truncated")
        new_tag = stage / "snapshot"
        new_tag.mkdir()
        write_tree(new_tag / INDEX, tree)
        manifest = dict(contents, schema=SCHEMA, tag=tag, producer_commit_sha=producer,
                        index_sha256=archive_sha(new_tag / INDEX, None))
        (new_tag / MANIFEST).write_bytes(json_bytes(manifest))
        meta_bytes = sum(path.stat().st_size for path in new_tag.iterdir())
        destination.parent.mkdir(exist_ok=True)
        new_tag.rename(destination)
        return {"payload_bytes": sum(part["bytes"] for part in uploaded), "metadata_bytes": meta_bytes,
                "new_volumes": [part["sha256"] for part in uploaded],
                "reused_bytes": contents["archive_bytes"] - written, "compacted": compact,
                "peak_staged_payload_bytes": peak}


def restore(store, tag, root, destination, targets=()):
    """Populate an empty build directory after validating all requested members."""
    if destination.exists() or destination.is_symlink():
        raise ValueError("volume restore requires a fresh build destination")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with store_lock(store, write=False), tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        manifest, tree = read_snapshot(store, tag)
        if lake_configuration(root) != manifest["lake_inputs_sha256"]:
            raise ValueError("snapshot Lake configuration mismatch")
        unpacked = Path(temporary) / "build"
        unpacked.mkdir()
        rows = tree["members"]
        if targets:
            rows = index.scoped_members(root, rows, targets, unpacked, require_complete=True)
        by_offset = {row["offset"]: spans for row, spans in zip(tree["members"], tree["locations"], strict=True)}
        installed, length, digest = set(), 0, hashlib.sha256()
        for row in rows:
            with tempfile.TemporaryFile(dir=temporary) as frame:
                for key, start, size in by_offset[row["offset"]]:
                    copy_range(store / "volumes" / key, start, size, frame)
                    length += size
                frame.seek(0)
                if not targets:
                    while block := frame.read(1024 * 1024):
                        digest.update(block)
                    frame.seek(0)
                index.extract_frame(frame, row, unpacked, installed, None)
                if row["module"] is not None:
                    index.check_compiler_outputs(unpacked, row)
        if not targets and digest.hexdigest() != manifest["archive_sha256"]:
            raise ValueError("restored logical stream checksum mismatch")
        unpacked.rename(destination)
        return {"payload_bytes": length, "members": len(rows)}


def retire(store, tags):
    """Mark every surviving snapshot, retire selected tags, then collect unreferenced volumes."""
    with store_lock(store, write=True):
        snapshots = store / "snapshots"
        retire_paths = {snapshot_path(store, tag) for tag in tags}
        all_tags = set(snapshots.iterdir())
        if not retire_paths.issubset(all_tags):
            raise ValueError("unknown snapshot selected for retirement")
        live = set()
        for path in all_tags - retire_paths:
            _, tree = read_snapshot(store, path.name)
            for digest, size in tree["volumes"].items():
                if (store / "volumes" / digest).stat().st_size != size:
                    raise ValueError("cannot collect with an incomplete retained snapshot")
                live.add(digest)
        with tempfile.TemporaryDirectory(prefix=".retired-", dir=store) as trash:
            for path in retire_paths:
                path.rename(Path(trash) / path.name)
            removed = []
            for path in (store / "volumes").iterdir():
                if index.valid_digest(path.name) and path.name not in live:
                    path.unlink()
                    removed.append(path.name)
            return sorted(removed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    pub = commands.add_parser("publish")
    pub.add_argument("tag")
    pub.add_argument("--repository", type=Path, required=True)
    pub.add_argument("--base")
    pub.add_argument("--volume-bytes", type=int, default=VOLUME_BYTES)
    get = commands.add_parser("restore")
    get.add_argument("tag")
    get.add_argument("--repository", type=Path, required=True)
    get.add_argument("--destination", type=Path, required=True)
    get.add_argument("--target", action="append", default=[])
    remove = commands.add_parser("retire")
    remove.add_argument("tags", nargs="+")
    args = parser.parse_args()
    if args.command == "publish":
        result = publish(args.store, args.tag, args.repository, args.base, args.volume_bytes)
    elif args.command == "restore":
        result = restore(args.store, args.tag, args.repository, args.destination, args.target)
    else:
        result = retire(args.store, args.tags)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
