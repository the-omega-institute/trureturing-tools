"""Compose a complete v6 archive from verified predecessor frames and a sparse build."""

import hashlib
from itertools import groupby

from lean_artifact_inventory import (PROJECT_BUILD, build_inventory, lake_configuration, lean_artifact_paths,
                                     project_imports, required_compiler_paths, workspace_modules)
import lean_release_index as index
import lean_release_volumes as volumes


class VerifiedFrames:
    """Accept a batched range stream only when each frame matches its indexed digest."""

    def __init__(self, rows, output):
        self.rows = rows
        self.output = output
        self.pos = self.used = 0
        self.digest = hashlib.sha256()

    def write(self, data):
        block = memoryview(data)
        while block:
            if self.pos == len(self.rows):
                raise ValueError("excess predecessor frame bytes")
            row = self.rows[self.pos]
            piece = block[:row["bytes"] - self.used]
            if self.output.write(piece) != len(piece):
                raise OSError("short sparse volume write")
            self.digest.update(piece)
            self.used += len(piece)
            block = block[len(piece):]
            if self.used == row["bytes"]:
                if self.digest.hexdigest() != row["sha256"]:
                    raise ValueError("predecessor frame checksum mismatch")
                self.pos += 1
                self.used = 0
                self.digest = hashlib.sha256()
        return len(data)

    def finish(self):
        if self.pos != len(self.rows):
            raise ValueError("truncated predecessor frames")


def planned_modules(plan, field):
    names = plan[field]
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names) or len(set(names)) != len(names):
        raise ValueError("invalid sparse module list: " + field)
    return set(names)


def overlay_frames(root, tree, plan):
    modules = workspace_modules(root)
    graph = project_imports(root, modules)
    old = {row["module"]: row for row in tree["members"] if row["module"] is not None}
    inherit = planned_modules(plan, "inherit_modules")
    rebuild = planned_modules(plan, "rebuild_modules")
    removed = planned_modules(plan, "removed_modules")
    restored = planned_modules(plan, "restore_modules")
    expected = inherit | rebuild
    if (inherit & rebuild or not restored <= inherit or not inherit <= old.keys() or not expected <= modules.keys()
            or removed != old.keys() - expected):
        raise ValueError("sparse plan does not cover the predecessor and target modules")

    live = {}
    residual = []
    removed_paths = {path for name in removed for path in old[name]["paths"]}
    inventory = build_inventory(root, modules)
    for name, artifacts in index.member_groups(inventory, graph, expected):
        if name is None:
            if any(str(item.path) in removed_paths for item in artifacts):
                raise ValueError("removed module artifacts remain in the sparse build")
            residual.append((None, artifacts))
        else:
            live[name] = artifacts
    if not rebuild <= live.keys() or not live.keys() <= expected:
        raise ValueError("sparse build does not cover its planned module frames")

    frames = []
    for name in index.ordered_modules(modules, graph):
        if name not in expected:
            continue
        mod = modules[name]
        if not graph[name] <= expected or (name in inherit and not graph[name] <= inherit):
            raise ValueError("sparse module plan is incomplete over imports: " + name)
        if name in inherit:
            row = old[name]
            if hashlib.sha256((root / mod.source).read_bytes()).hexdigest() != row["source_sha256"]:
                raise ValueError("inherited source differs from its predecessor: " + name)
            paths = {index.safe_path(path) for path in row["paths"]}
        else:
            paths = {item.path for item in live[name]}
            index.check_compiler_outputs(root / PROJECT_BUILD, {"module": name, "paths": paths})
        if not required_compiler_paths(name, mod) <= paths <= lean_artifact_paths(name, mod):
            raise ValueError("incomplete or misplaced compiler artifacts: " + name)
        frames.append((name, None if name in inherit else live[name]))
    return modules, old, frames + residual


def pack_overlay(root, stage, previous, plan, chunk_bytes, save_part, read_prior, deadline=None, *, progress=None):
    """Read predecessor rows in the supplied order. Emit only confirmed physical payloads."""
    manifest, tree = previous
    config = lake_configuration(root)
    if manifest["lake_inputs_sha256"] != config:
        raise ValueError("sparse predecessor Lake configuration differs from the target")
    index.validate_members(tree["members"], manifest["archive_bytes"])
    modules, old, frames = overlay_frames(root, tree, plan)

    def emit(selected, output):
        for inherited, positions in groupby(selected, key=lambda pos: frames[pos][1] is None):
            if inherited:
                rows = [old[frames[pos][0]] for pos in positions]
                start = output.tell()
                verified = VerifiedFrames(rows, output)
                read_prior(rows, verified)
                verified.finish()
                for row in rows:
                    yield dict(row, offset=start)
                    start += row["bytes"]
            else:
                for pos in positions:
                    name, artifacts = frames[pos]
                    yield index.write_frame(root, modules, name, artifacts, output, deadline, stable_timestamps=True)

    logical = volumes.HashOutput()
    rows = list(volumes.stream_frames(emit, range(len(frames)), logical, "volume_measure", progress))
    contents = {"lake_inputs_sha256": config, "archive_bytes": logical.size, "archive_sha256": logical.digest.hexdigest()}
    return volumes.pack_measured(stage, tree, rows, contents, chunk_bytes, save_part, emit, deadline, progress=progress)
