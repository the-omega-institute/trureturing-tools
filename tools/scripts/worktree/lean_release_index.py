"""Indexed, independently decompressible members for Lean Release snapshots."""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import gzip
from graphlib import TopologicalSorter
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import tarfile

from lean_artifact_inventory import (
    PROJECT_BUILD, build_inventory, import_closure, inspector_build_path, lake_configuration, lean_artifact_paths,
    project_imports, release_scope, required_compiler_paths, retired_report_path, workspace_modules,
)
from lean_cache_release_io import (
    INDEXED_ASSET, INDEXED_SCHEMA, ArchiveInput, MultipartArchiveOutput, archive_sha, copy_chunks, remaining,
)

ASSET = INDEXED_ASSET
INDEX = "index.json.gz"
SCHEMA = INDEXED_SCHEMA
BATCH_BYTES = 32 * 1024 * 1024


class MemberOutput:
    """Count and hash a frame as it is written into the volume stream."""

    def __init__(self, output):
        self.output = output
        self.digest = hashlib.sha256()
        self.start = output.tell()

    def write(self, block):
        amount = self.output.write(block)
        self.digest.update(memoryview(block)[:amount])
        return amount

    def record(self, module):
        return {"module": module, "offset": self.start, "bytes": self.output.tell() - self.start,
                "sha256": self.digest.hexdigest()}


def ordered_modules(modules, graph):
    """Traverse local dependencies before each module within its own package."""
    tuple(TopologicalSorter(graph).static_order())  # Reject cycles before the traversal.
    visited = set()
    for name in sorted(modules, key=lambda name: (modules[name].package, name)):
        pending = [(name, False)]
        while pending:
            current, emit = pending.pop()
            if current in visited:
                continue
            if emit:
                visited.add(current)
                yield current
            else:
                pending.append((current, True))
                pending.extend((dep, False) for dep in sorted(graph[current], reverse=True)
                               if modules[dep].package == modules[current].package)


def member_groups(inventory, graph, scope):
    by_module = defaultdict(list)
    residual = []
    for artifact in inventory.artifacts:
        if artifact.kind == "lean":
            if artifact.module in scope:
                by_module[artifact.module].append(artifact)
        elif not inspector_build_path(artifact.path):
            residual.append(artifact)
    for name in ordered_modules(inventory.modules, graph):
        if by_module[name]:
            yield name, by_module[name]
    batch = []
    size = 0
    for artifact in sorted(residual, key=lambda item: (item.kind, str(item.path))):
        if batch and size + artifact.size > BATCH_BYTES:
            yield None, batch
            batch, size = [], 0
        batch.append(artifact)
        size += artifact.size
    if batch:
        yield None, batch


def frame_inventory(root):
    modules = workspace_modules(root)
    graph = project_imports(root, modules)
    return modules, list(member_groups(build_inventory(root, modules), graph, release_scope(root, modules, graph)))


def write_frame(root, modules, module, artifacts, output, deadline, *, stable_timestamps):
    build = root / PROJECT_BUILD
    frame = MemberOutput(output)
    with gzip.GzipFile(filename="", fileobj=frame, mode="wb", compresslevel=1, mtime=0) as compressed:
        with tarfile.open(mode="w|", fileobj=compressed) as archive:
            for artifact in artifacts:
                source = build / artifact.path
                tar = archive.gettarinfo(str(source), arcname=str(artifact.path))
                if not tar.isfile() or tar.size != artifact.size:
                    raise ValueError(f"Lake artifact changed during snapshot: {artifact.path}")
                tar.uid = tar.gid = 0
                tar.uname = tar.gname = ""
                if stable_timestamps:
                    tar.mtime = 0
                    tar.pax_headers.pop("mtime", None)
                tar.mode = artifact.mode & 0o777
                with source.open("rb") as data:
                    archive.addfile(tar, ArchiveInput(data, deadline))
    row = frame.record(module)
    if module is not None:
        row["source_sha256"] = hashlib.sha256((root / modules[module].source).read_bytes()).hexdigest()
        row["paths"] = [str(artifact.path) for artifact in artifacts]
    return row


def pack(root, stage, chunk_bytes, deadline, *, stable_timestamps=False):
    modules, groups = frame_inventory(root)
    with MultipartArchiveOutput(stage, ASSET, chunk_bytes, deadline) as output:
        frames = [write_frame(root, modules, name, artifacts, output, deadline,
                              stable_timestamps=stable_timestamps) for name, artifacts in groups]
    with (stage / INDEX).open("wb") as encoded:
        with gzip.GzipFile(filename="", fileobj=encoded, mode="wb", mtime=0) as index:
            index.write(json.dumps({"members": frames}, separators=(",", ":")).encode())
    return {**output.metadata(), "lake_inputs_sha256": lake_configuration(root), "index": {"name": INDEX, "bytes": (stage / INDEX).stat().st_size,
                                           "sha256": archive_sha(stage / INDEX, deadline)}}


def valid_digest(digest):
    return isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None


def safe_path(name):
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or ".." in path.parts or str(path) != name:
        raise ValueError(f"invalid member path: {name}")
    return path


def read_index(path, size):
    def unique(pairs):
        values = {}
        for key, value in pairs:
            if key in values:
                raise ValueError("duplicate indexed field")
            values[key] = value
        return values

    with gzip.open(path, "rb") as stream:
        maximum = 128 * 1024 * 1024
        encoded = stream.read(maximum + 1)
    if len(encoded) > maximum:
        raise ValueError("member index exceeds maximum size")
    record = json.loads(encoded, object_pairs_hook=unique)
    if not isinstance(record, dict) or set(record) != {"members"}:
        raise ValueError("invalid member index")
    return validate_members(record["members"], size)


def validate_members(rows, size):
    if not isinstance(rows, list):
        raise ValueError("invalid member index")
    end, modules, paths = 0, set(), set()
    for row in rows:
        if (not isinstance(row, dict) or type(row.get("offset")) is not int or row["offset"] != end
                or type(row.get("bytes")) is not int or row["bytes"] <= 0 or not valid_digest(row.get("sha256"))):
            raise ValueError("invalid member extent")
        end += row["bytes"]
        module = row.get("module")
        fields = {"module", "offset", "bytes", "sha256"}
        if module is not None:
            if (not isinstance(module, str) or not module or module in modules
                    or not valid_digest(row.get("source_sha256"))
                    or not isinstance(row.get("paths"), list) or not row["paths"]):
                raise ValueError("invalid indexed module")
            modules.add(module)
            for name in row["paths"]:
                if not isinstance(name, str) or safe_path(name) in paths:
                    raise ValueError("duplicate or invalid indexed path")
                paths.add(safe_path(name))
            fields |= {"source_sha256", "paths"}
        if set(row) != fields:
            raise ValueError("invalid indexed fields")
    if end != size or not rows:
        raise ValueError("member index does not cover archive")
    return rows


def volume_ranges(parts, offset, length):
    """Yield volume-relative contiguous ranges covering a member or run of members."""
    end = offset + length
    start = 0
    for part in parts:
        boundary = start + part["bytes"]
        if offset < boundary and end > start:
            lower = max(start, offset)
            yield part, lower - start, min(end, boundary) - lower
        if end <= boundary:
            return
        start = boundary
    raise ValueError("member extent is outside Release volumes")


def selected_volume_ranges(parts, assets, rows):
    for group in adjacent_members(rows):
        start = group[0]["offset"]
        end = group[-1]["offset"] + group[-1]["bytes"]
        for part, offset, length in volume_ranges(parts, start, end - start):
            yield assets[part["name"]], offset, length


class MemberInput:
    def __init__(self, stream, size, deadline):
        self.stream = stream
        self.left = size
        self.deadline = deadline
        self.digest = hashlib.sha256()

    def read(self, size):
        remaining(self.deadline)
        block = self.stream.read(min(size, self.left))
        self.left -= len(block)
        self.digest.update(block)
        return block


def extract_frame(stream, row, destination, installed, deadline):
    member = MemberInput(stream, row["bytes"], deadline)
    expected = set(row["paths"]) if row["module"] is not None else None
    observed = set()
    with gzip.GzipFile(fileobj=member) as compressed:
        with tarfile.open(mode="r|", fileobj=compressed) as archive:
            for entry in archive:
                path = safe_path(entry.name)
                if (not entry.isfile() or path in installed or
                        (expected is not None and entry.name not in expected)):
                    raise ValueError(f"invalid archived member path: {entry.name}")
                installed.add(path)
                observed.add(entry.name)
                if retired_report_path(path):
                    continue
                target = destination / path
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as output:
                    if copy_chunks(archive.extractfile(entry), output, deadline) != entry.size:
                        raise ValueError(f"truncated artifact: {entry.name}")
                target.chmod(entry.mode & 0o777)
                os.utime(target, (entry.mtime, entry.mtime))
        while compressed.read(1024 * 1024):
            remaining(deadline)
    if member.left or member.digest.hexdigest() != row["sha256"] or (expected is not None and observed != expected):
        raise ValueError("Release member checksum, size or inventory mismatch")


class BlockInput:
    """Present successive transport blocks as one file across asset and frame boundaries."""

    def __init__(self, blocks):
        self.blocks = blocks
        self.pending = memoryview(b"")

    def read(self, size):
        if size < 0:
            raise ValueError("Release frame reads need a finite size")
        pieces = []
        while size:
            if not self.pending:
                self.pending = memoryview(next(self.blocks, b""))
                if not self.pending:
                    break
            block = self.pending[:size]
            pieces.append(block)
            self.pending = self.pending[len(block):]
            size -= len(block)
        return b"".join(pieces)


def extract_ranges(range_blocks, spans, rows, destination, installed, deadline):
    requests = transferred = 0

    def blocks():
        nonlocal requests, transferred
        for asset, start, length in spans:
            yield from range_blocks(asset, start, length, deadline)
            requests += 1
            transferred += length

    with closing(blocks()) as source:
        reader = BlockInput(source)
        for row in rows:
            extract_frame(reader, row, destination, installed, deadline)
            if row["module"] is not None:
                check_compiler_outputs(destination, row)
        # Finish the last response, including its exact length and transport exit.
        if reader.read(1):
            raise ValueError("Release ranges exceed the selected members")
    return requests, transferred


class VolumeInput:
    """Read the indexed byte stream directly from its ordered volume files."""

    def __init__(self, directory, parts):
        self.directory = directory
        self.parts = iter(parts)
        self.stream = None

    def __enter__(self):
        return self

    def __exit__(self, *_error):
        if self.stream is not None:
            self.stream.close()

    def read(self, size):
        chunks = []
        while size:
            if self.stream is None:
                part = next(self.parts, None)
                if part is None:
                    break
                self.stream = (self.directory / part["name"]).open("rb")
            block = self.stream.read(size)
            if block:
                chunks.append(block)
                size -= len(block)
            else:
                self.stream.close()
                self.stream = None
        return b"".join(chunks)


def restore_full(stage, manifest, rows, destination, deadline):
    with VolumeInput(stage, manifest["parts"]) as source:
        restore_stream(source, manifest, rows, destination, deadline)


def restore_stream(source, manifest, rows, destination, deadline):
    installed = set()
    digest = hashlib.sha256()

    class VerifiedStream:
        def read(self, size):
            block = source.read(size)
            digest.update(block)
            return block

    stream = VerifiedStream()
    for row in rows:
        extract_frame(stream, row, destination, installed, deadline)
    if digest.hexdigest() != manifest["archive_sha256"]:
        raise ValueError("Release archive checksum mismatch")


def compatible_modules(root, rows, modules, graph, requested):
    by_name = {row["module"]: row for row in rows if row["module"] in requested}
    compatible = set()
    for name in TopologicalSorter({name: graph[name] for name in requested}).static_order():
        row = by_name.get(name)
        module = modules[name]
        if (row is None or not graph[name].issubset(compatible)
                or row["source_sha256"] != hashlib.sha256((root / module.source).read_bytes()).hexdigest()):
            continue
        # An indexed module may only supply its compiler outputs at the
        # corresponding Lake library and IR destinations in this checkout.
        allowed = lean_artifact_paths(name, module)
        paths = {PurePosixPath(path) for path in row["paths"]}
        if not paths.issubset(allowed):
            raise ValueError("indexed module paths disagree with the Lake workspace: " + name)
        if not required_compiler_paths(name, module).issubset(paths):
            continue
        compatible.add(name)
    return compatible


def scoped_members(root, rows, targets, target_build, *, require_complete=False):
    modules = workspace_modules(root)
    graph = project_imports(root, modules)
    unknown = set(targets) - modules.keys()
    if unknown:
        raise ValueError("unknown Lean cache targets: " + ", ".join(sorted(unknown)))
    requested = import_closure(graph, targets)
    compatible = compatible_modules(root, rows, modules, graph, requested)
    needed = {row["module"] for row in rows if row["module"] in compatible
              and any(not os.path.lexists(target_build / path) for path in row["paths"])}
    if require_complete and needed != requested:
        raise ValueError("scoped verification did not cover the requested import closure: "
                         + ", ".join(sorted(requested - needed)))
    return [row for row in rows if row["module"] in needed]


def check_compiler_outputs(destination, row):
    """Verify that the staged frame contains the outputs declared by its Lake trace."""
    paths = {PurePosixPath(path) for path in row["paths"]}
    traces = [path for path in paths if path.suffix == ".trace"]
    if len(traces) != 1:
        raise ValueError("module frame needs one compiler trace: " + row["module"])
    trace = traces[0]
    data = json.loads((destination / trace).read_bytes())
    outputs = data.get("outputs") if isinstance(data, dict) else None
    if not isinstance(outputs, dict) or not valid_artifact_description(outputs):
        raise ValueError("invalid Lake compiler trace: " + row["module"])
    library = ["olean", "ilean"]
    if outputs.get("m") is True:
        library.extend(("olean.server", "olean.private"))
    library.extend(suffix for field, suffix in (("r", "ir"), ("rs", "ir.sig")) if field in outputs)
    required = {trace.with_suffix("." + suffix) for suffix in library}
    if "b" in outputs and not any(path.name == trace.with_suffix(".bc").name for path in paths):
        raise ValueError("module frame omits the Lake bitcode output: " + row["module"])
    if not required.issubset(paths):
        raise ValueError("module frame omits Lake compiler outputs: " + row["module"])


def valid_artifact_description(outputs):
    oleans = outputs.get("o")
    return (isinstance(oleans, list) and len(oleans) == (3 if outputs.get("m") is True else 1)
            and all(isinstance(value, str) and value for value in oleans)
            and all(isinstance(outputs.get(field), str) and outputs[field] for field in ("i", "c")))


def adjacent_members(rows, *, max_bytes=None):
    group = []
    end = 0
    for row in rows:
        if group and (row["offset"] != end or
                      (max_bytes is not None and end - group[0]["offset"] + row["bytes"] > max_bytes)):
            yield group
            group = []
        group.append(row)
        end = row["offset"] + row["bytes"]
    if group:
        yield group
