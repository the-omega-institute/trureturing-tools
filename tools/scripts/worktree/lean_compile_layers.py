"""Build and transfer disjoint compiler layers for a single checked-out source commit."""

import argparse
from collections import defaultdict
from dataclasses import dataclass
from graphlib import TopologicalSorter
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import tomllib

from lean_artifact_inventory import (
    PROJECT_BUILD, REG_INSPECTOR, build_inventory, import_closure, lake_configuration,
    lean_artifact_paths, project_imports, required_compiler_paths, workspace_modules, library_globs, matches_module_glob,
)
from lean_cache import binary_platform
from lean_cache_release import cache_guard, install_scoped, report_reuse
from lean_cache_release_io import copy_chunks
from lean_release_index import MemberInput, check_compiler_outputs, safe_path

STAGES = ("common", "0", "1", "2", "3")
MANIFEST = "compile-layer.json"


@dataclass(frozen=True)
class Layer:
    roots: tuple[str, ...]
    owned: frozenset[str]


def terminal(nodes, graph):
    return nodes.difference(dep for name in nodes for dep in graph[name])


def partition(graph, targets):
    """Assign terminal closures by size, then extract imports used by multiple workers."""
    targets = set(targets)
    unknown = targets - graph.keys()
    if unknown:
        raise ValueError(f"unknown Lean modules: {sorted(unknown)}")
    scope = import_closure(graph, targets)
    if not scope:
        raise ValueError("compile scope is empty")
    graph = {name: graph[name] for name in sorted(scope)}
    order = tuple(TopologicalSorter(graph).static_order())
    bits = {name: 1 << i for i, name in enumerate(order)}
    closures = {}
    for name in order:
        mask = bits[name]
        for dep in graph[name]:
            mask |= closures[dep]
        closures[name] = mask

    workers = [0] * 4
    roots = [[] for _ in workers]
    for name in sorted(terminal(scope, graph), key=lambda n: (-closures[n].bit_count(), n)):
        mask = closures[name]
        i = min(range(4), key=lambda i: ((workers[i] | mask).bit_count(),
                                        (mask & ~workers[i]).bit_count(), i))
        workers[i] |= mask
        roots[i].append(name)

    seen = shared = 0
    for mask in workers:
        shared |= seen & mask
        seen |= mask
    common = frozenset(name for name in order if bits[name] & shared)
    layers = {"common": Layer(tuple(sorted(terminal(common, graph))), common)}
    covered = set(common)
    for i, mask in enumerate(workers):
        exclusive = mask & ~shared
        owned = frozenset(name for name in order if bits[name] & exclusive)
        if covered & owned or any(graph[name] - owned - common for name in owned):
            raise ValueError("worker closure overlaps another worker or lacks an import")
        covered.update(owned)
        layers[str(i)] = Layer(tuple(sorted(roots[i])), owned)
    if covered != scope or any(graph[name] - common for name in common):
        raise ValueError("incomplete compiler partition")
    return layers


def production_targets(root, modules):
    # The ordinary full build includes both default inspector libraries. Release
    # report preparation also builds the analyzer and the compiled FIB regression.
    output = subprocess.check_output(
        ["lake", "-d", str(REG_INSPECTOR), "--json", "query", "@leanInspector/LeanInformationAudit:modules"],
        cwd=root, text=True,
    )
    targets = set(report_reuse.publication.selection.Selection(root).modules())
    targets.update(json.loads(output))
    for directory in (Path("."), Path("Reg"), REG_INSPECTOR, Path("tools/lean-inspector-interface")):
        config = tomllib.loads((root / directory / "lakefile.toml").read_text())
        names = set(config["defaultTargets"])
        if directory == REG_INSPECTOR:
            names.add("LeanInformationAuditFibTests")
            executables = {exe["name"]: exe.get("root", exe["name"]) for exe in config["lean_exe"]}
            targets.update((executables["auricFibAnalysis"], executables["compiledFibTests"]))
        globs = [pattern for lib in config["lean_lib"] if lib["name"] in names
                 for pattern in library_globs(lib)]
        targets.update(name for name, mod in modules.items() if mod.package == config["name"]
                       and any(matches_module_glob(name, pattern) for pattern in globs))
    targets.update(("Inspector", "LeanInformationAudit.Contract.InputDiscovery"))
    return targets


def checkout(root):
    # Packaged inspector sources are independently checked against their pinned release
    # by workspace_modules. Tracked edits and untracked Lean sources cannot impersonate HEAD.
    sha = subprocess.check_output(["git", "rev-parse", "--verify", "HEAD"], cwd=root, text=True).strip()
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "--",
         "*.lean", "lean-toolchain", "*lake-manifest.json", "*lakefile.toml",
         "Meta/tools-release.json", "lean-report-inputs.json"], cwd=root,
    )
    if dirty:
        raise ValueError("compile inputs differ from the checked-out commit")
    return sha


def compile_roots(root, modules, roots, *, no_build=False):
    # Each invocation uses the downstream Lake workspace that owns all five packages.
    # Bound total argv bytes as well as the number of targets on ordinary Linux runners.
    batch = []
    size = 0
    for name in roots:
        target = f"@{modules[name].package}/+{name}:olean"
        length = len(os.fsencode(target)) + 1
        if batch and (size + length > 48 * 1024 or len(batch) >= 512):
            lake_build(root, batch, no_build)
            batch = []
            size = 0
        batch.append(target)
        size += length
    if batch:
        lake_build(root, batch, no_build)


def lake_build(root, batch, no_build):
    options = ["--no-build"] if no_build else []
    subprocess.run(["bash", "tools/scripts/worktree/lean-cache-run.sh",
                    "lake", "-d", str(REG_INSPECTOR), *options, "build", *batch], cwd=root, check=True)


def check_outputs(build, modules, grouped):
    for name, module in modules.items():
        paths = grouped[name]
        if not required_compiler_paths(name, module) <= paths:
            raise ValueError(f"incomplete compiler artifacts: {name}")
        check_compiler_outputs(build, {"module": name, "paths": paths})
        if any(path.with_name(path.name + ".hash") not in paths for path in paths
               if path.suffix not in (".trace", ".hash")):
            raise ValueError(f"missing compiler output hash: {name}")


def pack(root, modules, destination, identity):
    build = root / PROJECT_BUILD
    rows = build_inventory(root, modules).by_module() if modules else {}
    grouped = {name: {a.path for a in rows.get(name, ()) if a.kind == "lean"} for name in modules}
    check_outputs(build, modules, grouped)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        candidate = Path(temporary) / "layer.tar.gz"
        with candidate.open("wb") as raw, gzip.GzipFile(
            filename="", fileobj=raw, mode="wb", mtime=0, compresslevel=1,
        ) as compressed, tarfile.open(fileobj=compressed, mode="w|") as archive:
            hashes = {}
            for name in sorted(modules):
                for path in sorted(grouped[name]):
                    member = tarfile.TarInfo(str(path))
                    member.size = (build / path).stat().st_size
                    with (build / path).open("rb") as source:
                        data = MemberInput(source, member.size, None)
                        archive.addfile(member, data)
                        hashes[str(path)] = data.digest.hexdigest()
            manifest = json.dumps({"identity": identity, "files": hashes},
                                  sort_keys=True, separators=(",", ":")).encode()
            member = tarfile.TarInfo(MANIFEST)
            member.size = len(manifest)
            archive.addfile(member, io.BytesIO(manifest))
        candidate.replace(destination)


def recover(root, modules, source, identity):
    lake = root / ".lake"
    if lake.is_symlink() or (lake / "build").is_symlink():
        raise ValueError("unsafe Lake build directory")
    lake.mkdir(exist_ok=True)
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"unavailable compile layer: {source}")
    allowed = {path: name for name, mod in modules.items() for path in lean_artifact_paths(name, mod)}
    with tempfile.TemporaryDirectory(dir=lake) as temporary:
        stage = Path(temporary)
        hashes = {}
        grouped = defaultdict(set)
        manifest = None
        with gzip.open(source, "rb") as compressed, tarfile.open(fileobj=compressed, mode="r|") as archive:
            for entry in archive:
                path = safe_path(entry.name)
                if entry.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or manifest is not None:
                    raise ValueError(f"unexpected compile layer member: {path}")
                with archive.extractfile(entry) as data:
                    if entry.name == MANIFEST:
                        if entry.size > 1024 + len(allowed) * 512:
                            raise ValueError("oversized compile layer manifest")
                        manifest = report_reuse.publication.read_json(data.read())
                        continue
                    if path not in allowed or entry.name in hashes:
                        raise ValueError(f"unexpected compile layer output: {path}")
                    destination = stage / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    content = MemberInput(data, entry.size, None)
                    with destination.open("xb") as output:
                        copy_chunks(content, output, None)
                    if content.left:
                        raise ValueError(f"truncated compile layer output: {path}")
                    hashes[entry.name] = content.digest.hexdigest()
                    grouped[allowed[path]].add(path)
            # Drain the gzip trailer to check its integrity, including for empty layers.
            while compressed.read(1024 * 1024):
                pass
        if manifest != {"identity": identity, "files": hashes}:
            raise ValueError("compile layer identity or file hashes differ")
        check_outputs(stage, modules, grouped)
        install_scoped(lake / "build", stage, (safe_path(path) for path in hashes), None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "recover", "verify"))
    parser.add_argument("--stage", choices=STAGES)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--target", action="extend", nargs="+", default=[],
                        help="scope to the import closure of named workspace modules")
    args = parser.parse_args()
    if not args.stage and args.action != "verify":
        parser.error("build and recover require --stage")
    root = Path.cwd()
    sha = checkout(root)
    modules = workspace_modules(root)
    targets = args.target or production_targets(root, modules)
    layers = partition(project_imports(root, modules), targets)
    if args.action == "verify":
        selected = [layers[args.stage]] if args.stage else layers.values()
        roots = sorted({name for layer in selected for name in layer.roots})
        compile_roots(root, modules, roots, no_build=True)
        print(f"LEAN_COMPILE_LAYER verified={args.stage or 'all'} source={sha} roots={len(roots)}")
        return
    layer = layers[args.stage]
    owned = {name: modules[name] for name in layer.owned}
    scope = [(key, sorted(part.owned)) for key, part in layers.items()]
    identity = {"schema": 1, "source": sha, "config": lake_configuration(root),
                "platform": list(binary_platform()), "stage": args.stage,
                "partition": hashlib.sha256(json.dumps(scope).encode()).hexdigest()}
    archive = args.artifacts / (args.stage + ".tar.gz")
    print(f"LEAN_COMPILE_LAYER stage={args.stage} source={sha} modules={len(owned)} "
          f"roots={len(layer.roots)}", flush=True)
    if args.action == "build":
        compile_roots(root, modules, layer.roots)
        with cache_guard(root, shared=True):
            pack(root, owned, archive, identity)
    else:
        with cache_guard(root):
            recover(root, owned, archive, identity)


if __name__ == "__main__":
    main()
