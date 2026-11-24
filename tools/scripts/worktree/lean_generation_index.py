"""Authenticated target and physical-volume inventory used by both Lean cache readers."""

from graphlib import TopologicalSorter
import hashlib
import json
from pathlib import Path
import re

from lean_release_index import safe_path, valid_digest

SCHEMA = "lean-cache-generation-v1"
MAX_VOLUME = 1536 * 1024 * 1024
MAX_INDEX = 128 * 1024 * 1024


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate field in generation index")
        return result

    with Path(path).open("rb") as stream:
        data = stream.read(MAX_INDEX + 1)
    if len(data) > MAX_INDEX:
        raise ValueError("generation index exceeds size limit")
    return json.loads(data, object_pairs_hook=unique)


def validate(index, *, complete):
    if (not re.fullmatch(r"[0-9a-f]{40}", index["source"])
            or index.get("schema", SCHEMA) != SCHEMA):
        raise ValueError("invalid generation source or schema")
    rows, volumes = index["targets"], index["volumes"]
    if not isinstance(rows, dict) or not isinstance(volumes, dict) or (complete and not rows):
        raise ValueError("invalid generation inventory")
    for digest, size in volumes.items():
        if not valid_digest(digest) or type(size) is not int or not 0 < size <= MAX_VOLUME:
            raise ValueError("invalid physical volume size or address")
    path_owner = set()
    content_by_key = {}
    ranges = {}
    used = set()
    for name, row in rows.items():
        deps, paths = row["deps"], row["paths"]
        start, length, volume = row["offset"], row["bytes"], row["volume"]
        if (not name or not valid_digest(row["key"]) or not valid_digest(row["sha256"])
                or not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps)
                or len(set(deps)) != len(deps) or (complete and not set(deps) <= rows.keys())
                or volume not in volumes or type(start) is not int or type(length) is not int
                or start < 0 or length <= 0 or start + length > volumes[volume]
                or not paths or not isinstance(paths, list)):
            raise ValueError(f"invalid generation target or extent: {name}")
        content = (row["sha256"], length, sorted(paths))
        if content_by_key.setdefault(row["key"], content) != content:
            raise ValueError("conflicting artifacts for input key " + row["key"])
        for path in paths:
            safe_path(path)
            if path in path_owner:
                raise ValueError(f"duplicate generation path: {path}")
            path_owner.add(path)
        used.add(volume)
        ranges.setdefault(volume, []).append((start, start + length))
    for path in path_owner:
        if any(str(parent) in path_owner for parent in safe_path(path).parents):
            raise ValueError("generation archive contains a file ancestor: " + path)
    if volumes.keys() != used:
        raise ValueError("unreferenced generation volumes")
    for extents in ranges.values():
        end = 0
        for lower, upper in sorted(extents):
            if lower < end:
                raise ValueError("overlapping generation frame ranges")
            end = upper
    tuple(TopologicalSorter({name: row["deps"] for name, row in rows.items()}).static_order())


def matches(row, expect):
    return row["key"] == expect["key"] and sorted(row["deps"]) == sorted(expect["deps"])


def assemble(plan, base, receipts):
    expected = plan["targets"]
    rows, sizes = {}, {}

    def include(name, row, size):
        old = rows.setdefault(name, row)
        if (old["sha256"], old["bytes"], sorted(old["paths"])) != (
                row["sha256"], row["bytes"], sorted(row["paths"])):
            raise ValueError(f"same input key produced conflicting artifacts: {name}")
        if sizes.setdefault(row["volume"], size) != size:
            raise ValueError("conflicting physical volume sizes")

    if base is not None:
        validate(base, complete=True)
        for name, row in base["targets"].items():
            if name in expected and matches(row, expected[name]):
                include(name, row, base["volumes"][row["volume"]])
    for receipt in receipts:
        validate(receipt, complete=False)
        if receipt["source"] != plan["source"]:
            raise ValueError("staged shard belongs to another source")
        for name, row in receipt["targets"].items():
            if name not in expected or not matches(row, expected[name]):
                raise ValueError(f"staged shard input mismatch: {name}")
            include(name, row, receipt["volumes"][row["volume"]])
    if rows.keys() != expected.keys():
        raise ValueError(f"missing planned targets: {sorted(expected.keys() - rows.keys())[:8]}")
    result = {"schema": SCHEMA, "source": plan["source"], "targets": rows,
              "volumes": {v: sizes[v] for v in {row["volume"] for row in rows.values()}}}
    validate(result, complete=True)
    return result
