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
import time
import tomllib

import lean_artifact_inventory as inventory
from lean_cache_release import release_key
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
    # The release execution projection checks compiler environment overrides
    # and groups platforms with compatible Lean compiler outputs.
    environment = digest(["lean-olean-v1", release_key(root)["execution_sha256"], pins])
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

    # The selectors decide whether these settings apply; adding another selected
    # module within the same library changes no already-owned compiler command.
    settings["lean_lib"] = [{key: value for key, value in lib.items() if key not in {"roots", "globs"}}
                            for lib in config.get("lean_lib", []) if owns(lib)]
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


def verified_inputs(remote, domain, targets, base):
    """Reconcile a committed plan or a failed-job retry with durable partials."""
    validate(base, complete=False)
    snapshots = [base]
    gaps = targets.keys() - checked_hits(targets, snapshots)[0].keys()
    if gaps:
        snapshots.extend(checkpoints.discover(remote, domain, {targets[name]["key"] for name in gaps}))
    rows, sizes = checked_hits(targets, snapshots)
    confirmed = remote.confirmed_volumes(sizes)
    rows = {name: row for name, row in rows.items() if row["volume"] in confirmed}
    return checked_hits(targets, [{"targets": rows, "volumes": confirmed}])


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
    totals = {"uploaded_bytes": 0, "new_volumes": 0}
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
            for field in totals:
                totals[field] += metrics[field]
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
    return result, totals


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
        domain, _ = package_inputs(root)
        rows, volumes = verified_inputs(remote, domain, targets, base)
        plan, hits = plan_layers(source, targets, [{"targets": rows, "volumes": volumes}])
        plan["domain"] = domain
        write(output / "plan.json", plan)
        write(output / "hits.json", hits)
        common = set(plan["stages"].get("common", {}).get("owned", []))
        workers, independent = [], []
        for name, layer in plan["stages"].items():
            if name != "common":
                (workers if common.intersection(layer["restore"]) else independent).append(name)
        if github_output := os.environ.get("GITHUB_OUTPUT"):
            with open(github_output, "a") as out:
                print("shared=" + str("common" in plan["stages"]).lower(), file=out)
                print("workers=" + json.dumps(workers), file=out)
                print("independent=" + json.dumps(independent), file=out)
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
    scope = inventory.import_closure(graph, owned)
    # When only failed jobs rerun, Actions keeps the original plan and hits
    # artifacts. Reopen checkpoints written since that plan before compiling.
    if int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")) > 1:
        inputs = {name: plan["targets"][name] for name in scope}
        rows, sizes = verified_inputs(remote, plan["domain"], inputs, hits)
        if scope - owned - rows.keys():
            raise ValueError("a prerequisite of the pinned stage is unavailable")
        hits = {"source": plan["source"], "targets": rows, "volumes": sizes}
    reused = {name: hits["targets"][name] for name in owned & hits["targets"].keys()}
    result = {"source": plan["source"], "targets": reused.copy(),
              "volumes": {r["volume"]: hits["volumes"][r["volume"]] for r in reused.values()}}
    pending = owned - reused.keys()
    order = TopologicalSorter({name: graph[name] & pending for name in sorted(pending)}).static_order()
    installed = set()
    started = time.monotonic()
    initial_free = shutil.disk_usage(root).free
    stats = {"compiled_targets": 0, "checkpoint_targets": len(reused), "restored_targets": 0, "downloaded_bytes": 0, "uploaded_bytes": 0,
             "new_volumes": 0, "sampled_peak_disk_growth_bytes": 0}

    def sample_disk():
        stats["sampled_peak_disk_growth_bytes"] = max(stats["sampled_peak_disk_growth_bytes"],
                                                      initial_free - shutil.disk_usage(root).free)

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
            stats["downloaded_bytes"] += restored["transferred_bytes"]
            stats["restored_targets"] += restored["restored"]
            sample_disk()
            compiler.compile_roots(root, modules, sorted(compiler.terminal(missing, graph)), no_build=True)
            installed.update(missing)
        compiler.compile_roots(root, modules, sorted(compiler.terminal(targets, graph)))
        sample_disk()
        receipt, staged = batch_frames(root, plan, modules, batch, output, plan["domain"], remote)
        stats["compiled_targets"] += len(receipt["targets"])
        stats["uploaded_bytes"] += staged["uploaded_bytes"]
        stats["new_volumes"] += staged["new_volumes"]
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
    stats["elapsed_seconds"] = round(time.monotonic() - started, 3)
    result["metrics"] = stats
    write(output, result)
    print("LEAN_GENERATION_WORKER " + json.dumps(stats), flush=True)


if __name__ == "__main__":
    main()
