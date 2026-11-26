"""Build scoped Lean evidence and check the candidate and its selected delta."""

import argparse
from collections import Counter
from itertools import groupby
from math import isqrt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib

from ci_contract import read_changed, write_paths
from ci_checks import CLI

SCRIBE = ["dotnet", "tools/StrataLint.Scribe/bin/Release/net10.0/StrataLint.Scribe.dll"]
MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*")


def report_batches(scope, limit=60_000):
    # Each root list is passed through make, Lake and dotnet as one argv string.
    # Keep every document with all its truth anchors when a large diff needs batches.
    all_targets = scope["targets"]
    if not all_targets:
        return
    if sum(len(name) + 1 for name in all_targets) <= limit:
        yield all_targets, scope["paths"]
        return
    definitions = scope["definitions"]
    document_targets = {name for roots in definitions.values() for name in roots}
    groups = [(refs, path) for path, refs in sorted(definitions.items())]
    groups.extend(([name], None) for name in all_targets if name not in document_targets)
    targets, documents, size = set(), [], 0
    for names, document in groups:
        added = set(names) - targets
        extra = sum(len(name) + 1 for name in added)
        if targets and size + extra > limit:
            yield sorted(targets), documents
            targets, documents, size = set(), [], 0
            extra = sum(len(name) + 1 for name in set(names))
        targets.update(names)
        size += extra
        if document is not None:
            documents.append(document)
    if targets:
        yield sorted(targets), documents


def plan_scope(manifest, base=None):
    planned = subprocess.run([*SCRIBE, "lean-inputs", "--affected", "--paths-from", str(manifest),
                              *(["--base", base] if base else [])],
                             stdout=subprocess.PIPE, text=True, check=True)
    return json.loads(planned.stdout)


def scope_modules(scope):
    modules = sorted(set(scope["targets"]) | set(scope["programs"]))
    if any(not MODULE.fullmatch(module) for module in modules):
        raise ValueError("invalid Lean module in scope")
    return modules


def report_cache_inputs(roots):
    # Use the report reader's source-derived scope, including named utility inputs.
    if not roots:
        return
    producer = subprocess.check_output(
        ["bash", str(Path(__file__).with_name("judge-lean-producer.sh")), str(Path(CLI).parent)],
        text=True).strip() or CLI
    for batch, _ in report_batches({"targets": sorted(roots), "paths": [], "definitions": {}}):
        output = subprocess.check_output(
            ["dotnet", producer, "lean-utility-input", "--modules", "--targets", " ".join(batch)], text=True)
        yield from json.loads(output)


def build_environment(programs, targets):
    environment = dict(os.environ, FULL_BUILD="0")
    # Hosted current restores the judge for this source key or builds it on a fresh runner.
    if ((programs or targets) and environment.get("GITHUB_EVENT_NAME") in ("pull_request", "push")
            and not environment.get("STRATALINT_LEAN_PRODUCER_DLL")):
        producer = subprocess.run(["bash", "tools/scripts/workflow/judge-lean-producer.sh",
                                   str(Path(CLI).parent)], stdout=subprocess.PIPE, text=True, check=True)
        environment["STRATALINT_LEAN_PRODUCER_DLL"] = producer.stdout.strip()
    return environment


def build_modules(modules, environment):
    subprocess.run(["bash", "tools/scripts/worktree/lean-cache-run.sh", "lake",
                    "-d", "tools/lean-inspector-reg", "build",
                    *["+" + module for module in modules]], env=environment, check=True)


def check_programs(programs, environment):
    if programs:
        build_modules(programs, environment)
    internal = [name for name in programs if Path("src", *name.split(".")).with_suffix(".lean").is_file()]
    if internal and tomllib.loads(Path("lakefile.toml").read_text()).get("testDriver"):
        subprocess.run(["bash", "tools/scripts/worktree/lean-cache-run.sh", "lake", "test", "--", *internal],
                       env=environment, check=True)


def check_batch(batch, scratch, targets, selected_paths, environment):
    report = batch / "scoped-lean-report.json"
    pinned_targets = "LEAN_TARGETS=" + " ".join(targets)
    subprocess.run(["make", "lean-report-prepare", pinned_targets], env=environment, check=True)
    subprocess.run(["make", "lean-report-scoped", pinned_targets],
                   env=dict(environment, LEAN_REPORT=str(report), LEAN_NUM_THREADS="1"), check=True)
    roots = scratch / "roots"
    documents = scratch / "documents"
    write_paths(roots, [name.replace(".", "/") + ".lean" for name in targets])
    write_paths(documents, selected_paths)
    scribe_environment = os.environ.copy()
    if ("DOTNET_GCConserveMemory" not in scribe_environment
            and "COMPlus_GCConserveMemory" not in scribe_environment):
        scribe_environment["DOTNET_GCConserveMemory"] = "9"
    subprocess.run([*SCRIBE, "content-check", "--scoped", "--report", str(report),
                    "--targets-from", str(roots), "--paths-from", str(documents)],
                   env=scribe_environment, check=True)
    return report


def check_admission(directory, manifest, changed, base, protected_base, reports):
    report_list = directory / "reports"
    write_paths(report_list, [str(report) for report in reports])
    arguments = ["--changed-paths-from", str(manifest), "--scoped-reports-from", str(report_list)]
    current = subprocess.run(["dotnet", CLI, "check-current", *arguments,
                              *(["--scope-base", base] if base and "Meta/tools-release.json" in changed else [])])
    if protected_base is None:
        return current.returncode
    delta = subprocess.run(["dotnet", CLI, "check-delta", "--protected-base", protected_base, *arguments])
    return current.returncode or (0 if delta.returncode == 3 else delta.returncode)


def check(manifest, protected_base=None, scope_file=None, base=None):
    base = base or protected_base
    changed = read_changed(manifest)
    if changed is None:
        raise ValueError("missing changed-path manifest")
    with tempfile.TemporaryDirectory(prefix="ci-pr-lean-") as temporary:
        directory = Path(temporary)
        scope = plan_scope(manifest, base) if scope_file is None else json.loads(scope_file.read_text())
        programs, targets = scope["programs"], scope["targets"]
        scope_modules(scope)
        print(f"CI_PR_LEAN report_targets={len(targets)} program_targets={len(programs)}", flush=True)
        environment = build_environment(programs, targets)
        check_programs(programs, environment)
        reports = []
        for index, (targets, selected_paths) in enumerate(report_batches(scope)):
            batch = directory / str(index)
            batch.mkdir()
            reports.append(check_batch(batch, batch, targets, selected_paths, environment))
        return check_admission(directory, manifest, changed, base, protected_base, reports)


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def refine_batches(batches, definitions, cold, weights, limit=60_000):
    """Keep documents on their selected runners. Move families of independent roots together."""
    assigned = [set(roots) for roots, _ in batches]
    pinned = {root for names in definitions.values() for root in names}
    families = []
    counts = [Counter() for _ in batches]
    sizes = [sum(len(name) + 1 for name in roots) for roots in assigned]
    if limit is not None and max(sizes) > limit:
        return batches
    for lane, roots in enumerate(assigned):
        # One reference per group lets a move charge only its marginal closure on each runner.
        counts[lane].update(set().union(*(cold[n] for n in roots & pinned)))
        for _, members in groupby(sorted(roots - pinned), key=lambda name: name.rpartition('.')[0]):
            names = tuple(members)
            missing = set().union(*(cold[name] for name in names))
            counts[lane].update(missing)
            families.append((lane, names, missing, sum(len(name) + 1 for name in names)))

    def work(names):
        return len(names), sum(weights[name] for name in names)

    loads = [work(c) for c in counts]
    total_count, total_weight = map(sum, zip(*loads))
    if not total_count or not total_weight:
        return batches

    def score(trial):
        # Normalize the row-count and source-size coordinates to their respective initial loads.
        scaled = [value for count, weight in trial for value in (count * total_weight, weight * total_count)]
        return (max(0, sum(count for count, _ in trial) - total_count),
                max(scaled), sum(value * value for value in scaled))

    previous = score(loads)
    # Each pass visits every movable family once. Strict improvement precludes oscillation.
    while True:
        improved = False
        for pos, (src, names, missing, size) in enumerate(families):
            if len(assigned[src]) == len(names):
                continue
            removed = work({name for name in missing if counts[src][name] == 1})
            if not removed[0]:
                continue
            reduced = tuple(a - b for a, b in zip(loads[src], removed))
            best, move = previous, None
            for dest in range(len(batches)):
                if dest == src or (limit is not None and sizes[dest] + size > limit):
                    continue
                added = work(missing - counts[dest].keys())
                enlarged = tuple(a + b for a, b in zip(loads[dest], added))
                trial = loads.copy()
                trial[src], trial[dest] = reduced, enlarged
                value = score(trial)
                if value < best:
                    best, move = value, (dest, enlarged)
            if move is None:
                continue
            dest, enlarged = move
            assigned[src].difference_update(names)
            assigned[dest].update(names)
            sizes[src] -= size
            sizes[dest] += size
            counts[src].subtract(missing)
            for name in missing:
                if not counts[src][name]:
                    del counts[src][name]
            counts[dest].update(missing)
            loads[src], loads[dest] = reduced, enlarged
            families[pos] = (dest, names, missing, size)
            previous, improved = best, True
        if not improved:
            return [(sorted(roots), paths) for roots, (_, paths) in zip(assigned, batches)]


def lane_parts(scope, roots, paths):
    selection = dict(targets=sorted(roots), paths=sorted(paths),
                     definitions={p: scope["definitions"][p] for p in paths})
    return [dict(targets=names, paths=docs) for names, docs in report_batches(selection)]


def isolate_new_roots(scope, batches, fresh, cold, weights, maximum_lanes):
    isolated = {name for name, modules in fresh.items() if modules}
    definitions = scope["definitions"]
    # Documents sharing an anchor may connect additional roots to the new mathematics.
    while isolated:
        expanded = isolated.union(*(roots for roots in definitions.values() if isolated.intersection(roots)))
        if expanded == isolated:
            break
        isolated = expanded
    ordinary = set(scope["targets"]) - isolated
    if not (isolated and ordinary) or sum(len(n) + 1 for n in isolated) > 60_000:
        return None
    new_docs = sorted(path for path, refs in definitions.items() if isolated.intersection(refs))
    other_docs = sorted(set(scope["paths"]) - set(new_docs))
    pieces = lane_parts(scope, ordinary, other_docs)
    count = min(maximum_lanes, len(batches), len(pieces) + 1) - 1
    lanes = [(set(), []) for _ in range(count)]
    costs = [0] * count
    for part in pieces:
        lane = min(range(count), key=costs.__getitem__)
        roots, paths = lanes[lane]
        roots.update(part["targets"])
        paths.extend(part["paths"])
        costs[lane] += sum(len(n) + 1 for n in part["targets"])
    initial = [(sorted(roots), sorted(paths)) for roots, paths in lanes]
    balanced = refine_batches(initial, {p: definitions[p] for p in other_docs}, cold, weights, limit=None)
    return [(sorted(isolated), new_docs), *balanced]


def balance_plan(scope, index, root):
    # Read the same compiler graph and input keys as the published cache reader.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'worktree'))
    from lean_artifact_inventory import INSPECTOR, import_closure, project_imports
    from lean_ci_cache_scope import REPORT_PROGRAMS, inspector_program_root
    from scoped import runtime_targets
    from lean_module_cache import checkout_targets

    scope = {k: v for k, v in scope.items() if k != "report_batches"}
    batches = distributed_plan(scope)
    if (not 2 <= len(batches) <= 256 or not index["targets"]
            or any(sum(len(n) + 1 for n in names) > 60_000 for names, _ in batches)):
        return scope
    references = {}
    modules, targets = checkout_targets(root, scope_inputs=references)
    graph = {name: target['deps'] for name, target in targets.items()}
    # The published compiler inventory omits inspector programs and some runtime support.
    graph.update(project_imports(root, modules, sources=sorted(modules.keys() - graph.keys())))
    saved = index['targets']
    weights = {name: max(1, isqrt((root / modules[name].source).stat().st_size))
               for name, row in targets.items()
               if modules[name].report and row['key'] != saved.get(name, {}).get('key')}
    new_d5 = {name for name in modules if name.startswith("D5.") and name not in saved}
    cold, fresh = {}, {}
    inspector = (root / INSPECTOR / 'lakefile.lean').read_text()
    executables = {inspector_program_root(inspector, prog.split('/')[1]) for prog in REPORT_PROGRAMS}
    for name in scope['targets']:
        pending = [name, *references.get(str(modules[name].source), [])]
        visited = set()
        while pending:
            selected = import_closure(graph, pending) - visited
            visited.update(selected)
            # The inspector follows utility claims throughout the import closure.
            pending = {utility['claimModule'] for module in selected
                       if (utility := modules[module].report_utility)} - visited
        cold[name] = visited & weights.keys()
        runtime = {item.removeprefix('+') for item in runtime_targets(visited)}
        fresh[name] = (visited | import_closure(graph, runtime)) & new_d5
    isolated = isolate_new_roots(scope, batches, fresh, cold, weights, maximum_lanes=6)
    refined = isolated or refine_batches(batches, scope['definitions'], cold, weights)
    common = import_closure(graph, executables) & new_d5
    programs = import_closure(graph, scope["programs"]) & new_d5
    rows = []
    for lane, (names, paths) in enumerate(refined):
        new = common.union(*(fresh[name] for name in names))
        if lane == 0:
            new.update(programs)
        row = dict(targets=names, paths=paths, new_d5_modules=sorted(new))
        if isolated is not None:
            row["parts"] = lane_parts(scope, names, paths)
        rows.append(row)
    result = dict(scope, report_batches=rows)
    distributed_plan(result)
    return result



def distributed_plan(scope):
    scope_modules(scope)
    batches = list(report_batches(scope))
    stored = scope.get("report_batches")
    if stored is not None:
        partitioned = any("parts" in entry for entry in stored)
        if (partitioned and not 2 <= len(stored) <= min(6, len(batches))
                or not partitioned and len(stored) != len(batches)):
            raise ValueError("incorrect number of report batches")
        batches = [(entry["targets"], entry["paths"]) for entry in stored]
        for entry, (targets, paths) in zip(stored, batches):
            new = entry["new_d5_modules"]
            if (not isinstance(new, list) or new != sorted(set(new))
                    or any(not name.startswith("D5.") or not MODULE.fullmatch(name) for name in new)):
                raise ValueError("invalid new D5 module in stored report batch")
            parts = entry.get("parts", [dict(targets=targets, paths=paths)])
            if partitioned and entry.get("parts") != lane_parts(scope, targets, paths):
                raise ValueError("incomplete or unexpected report sub-batches")
            if (not targets or len(targets) != len(set(targets))
                    or any(sum(len(n) + 1 for n in part["targets"]) > 60_000 for part in parts)
                    or any(not set(scope["definitions"][path]) <= set(targets) for path in paths)):
                raise ValueError("invalid stored report batch")
    if not scope["targets"]:
        return batches
    if {name for targets, _ in batches for name in targets} != set(scope["targets"]):
        raise ValueError("report batches do not cover the planned modules")
    documents = [name for _, paths in batches for name in paths]
    if sorted(documents) != sorted(scope["paths"]):
        raise ValueError("report batches do not cover the planned Scribe paths")
    return batches


def batch_scope(scope, batches, index):
    if not 0 <= index < len(batches):
        raise ValueError(f"report batch {index} is outside the plan")
    targets, paths = batches[index]
    stored = scope.get("report_batches")
    new_d5 = stored[index]["new_d5_modules"] if stored is not None else []
    parts = stored[index].get("parts") if stored is not None else None
    return dict(scope, targets=targets, paths=paths, new_d5_modules=new_d5, parts=parts,
                programs=scope["programs"] if index == 0 else [],
                definitions={p: scope["definitions"][p] for p in paths if p in scope["definitions"]})


def batch_receipt(directory, plan_identity, index, part):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lean-inspector"))
    from publication import SUFFIXES

    names = ["scoped-lean-report.json" + suffix for suffix in SUFFIXES]
    receipt = dict(schema=1, **plan_identity, batch=index, files={name: file_hash(directory / name) for name in names})
    if part is not None:
        receipt["part"] = part
    return receipt


def write_batch(root, index, selected, identity):
    destination = root / f"batch-{index}"
    root.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"batch artifact already exists: {destination.name}")
    environment = build_environment(selected["programs"], selected["targets"])
    # Previously unpublished mathematical proofs have no reusable compilation history.
    # Build them one at a time before Lake schedules the rest of the batch in parallel.
    new_modules = selected.get("new_d5_modules", [])
    serial = dict(environment, LEAN_NUM_THREADS="1")
    for start in range(0, len(new_modules), 256):
        build_modules(new_modules[start:start + 256], serial)
    check_programs(selected["programs"], environment)
    with tempfile.TemporaryDirectory(prefix=".batch-", dir=root) as temporary:
        scratch = Path(temporary)
        staged = scratch / "bundle"
        staged.mkdir()
        parts = selected.get("parts") or [selected]
        for i, part in enumerate(parts):
            bundle = staged if len(parts) == 1 else staged / f"part-{i}"
            bundle.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(dir=scratch) as working:
                work = Path(working)
                check_batch(work, work, part["targets"], part["paths"], environment)
                receipt = batch_receipt(work, identity, index, i if len(parts) > 1 else None)
                for name in receipt["files"]:
                    (work / name).rename(bundle / name)
                (bundle / "receipt.json").write_text(json.dumps(receipt))
        staged.rename(destination)



def read_batches(root, part_counts, identity):
    if {entry.name for entry in root.iterdir()} != {f"batch-{i}" for i in range(len(part_counts))}:
        raise ValueError("incomplete or unexpected report batch artifacts")
    reports = []
    for index, count in enumerate(part_counts):
        directory = root / f"batch-{index}"
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(f"invalid directory for report batch {index}")
        if count > 1 and {path.name for path in directory.iterdir()} != {f"part-{i}" for i in range(count)}:
            raise ValueError(f"incomplete or unexpected report sub-batches in batch {index}")
        for i in range(count):
            bundle = directory if count == 1 else directory / f"part-{i}"
            if bundle.is_symlink() or not bundle.is_dir():
                raise ValueError(f"invalid report bundle in batch {index}")
            if any(path.is_symlink() or not path.is_file() for path in bundle.iterdir()):
                raise ValueError(f"invalid file in report batch {index}")
            expected = batch_receipt(bundle, identity, index, i if count > 1 else None)
            if {path.name for path in bundle.iterdir()} != {"receipt.json", *expected["files"]}:
                raise ValueError(f"unexpected files in report batch {index}")
            if json.loads((bundle / "receipt.json").read_text()) != expected:
                raise ValueError(f"report batch {index} does not match this plan and checkout")
            reports.append(bundle / "scoped-lean-report.json")
    return reports


def distributed(args, scope):
    batches = distributed_plan(scope)
    if args.batch_matrix:
        print(json.dumps({"include": [{"batch": i} for i in range(len(batches))]}))
        return 0
    changed = read_changed(args.manifest)
    if changed is None:
        raise ValueError("missing changed-path manifest")
    if not batches:
        raise ValueError("no reports in the supplied batch plan")
    head = subprocess.check_output(["git", "rev-parse", "--verify", "HEAD"], text=True).strip()
    identity = dict(head=head, run=os.environ.get("GITHUB_RUN_ID", ""), plan=file_hash(args.read_plan),
                    manifest=file_hash(args.manifest))
    root = args.artifacts.absolute()
    if root.is_symlink():
        raise ValueError("batch artifact root is a symlink")
    if args.batch is not None:
        write_batch(root, args.batch, batch_scope(scope, batches, args.batch), identity)
        return 0
    part_counts = [len(row["parts"]) if "parts" in row else 1
                   for row in scope.get("report_batches", [{}] * len(batches))]
    reports = read_batches(root, part_counts, identity)
    with tempfile.TemporaryDirectory(prefix="ci-pr-lean-") as temporary:
        return check_admission(Path(temporary), args.manifest, changed, args.base or args.protected_base,
                               args.protected_base, reports)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--base")
    parser.add_argument("--protected-base")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--write-plan", type=Path)
    modes.add_argument("--read-plan", type=Path)
    parser.add_argument("--cache-targets", action="store_true")
    batches = parser.add_mutually_exclusive_group()
    batches.add_argument("--balance-plan", type=Path, metavar="INDEX")
    batches.add_argument("--batch-matrix", action="store_true")
    batches.add_argument("--batch", type=int)
    batches.add_argument("--finalize", action="store_true")
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args()
    try:
        if args.balance_plan is not None:
            if not args.read_plan or args.cache_targets or args.artifacts:
                parser.error("--balance-plan requires --read-plan")
            scope = json.loads(args.read_plan.read_bytes())
            result = balance_plan(scope, json.loads(args.balance_plan.read_bytes()), Path.cwd())
            with tempfile.NamedTemporaryFile(mode="w", dir=args.read_plan.parent, delete=False) as output:
                json.dump(result, output)
            os.replace(output.name, args.read_plan)
            sys.exit(0)
        batch_mode = args.batch_matrix or args.batch is not None or args.finalize
        if batch_mode and not args.read_plan:
            parser.error("distributed reports require --read-plan")
        if args.cache_targets and (args.batch_matrix or args.finalize):
            parser.error("--cache-targets accepts only a batch index")
        needs_artifacts = args.finalize or (args.batch is not None and not args.cache_targets)
        if bool(args.artifacts) != needs_artifacts:
            parser.error("--artifacts is required only for writing or finalizing report batches")
        if batch_mode and not args.cache_targets:
            sys.exit(distributed(args, json.loads(args.read_plan.read_text())))
        if args.cache_targets:
            if args.write_plan:
                parser.error("--cache-targets cannot write a build plan")
            scope = (json.loads(args.read_plan.read_text()) if args.read_plan
                     else plan_scope(args.manifest, args.base or args.protected_base))
            if args.batch is not None:
                scope = batch_scope(scope, distributed_plan(scope), args.batch)
            modules = scope_modules(scope)
            if "cache_suites" in scope and (modules or scope["cache_suites"]):
                sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worktree"))
                from lean_ci_cache_scope import cache_modules

                modules = cache_modules(Path.cwd(), scope, modules, report_cache_inputs)
            if modules:
                print(" ".join(modules))
            sys.exit(0)
        if args.write_plan:
            scope = plan_scope(args.manifest, args.base or args.protected_base)
            # Main-repository CI selects content reports and programs. The tool
            # repository runs its own compiled suites against the consumer fixture.
            args.write_plan.write_text(json.dumps({**scope, "cache_suites": []}))
            sys.exit(0)
        code = check(args.manifest, args.protected_base, args.read_plan, args.base)
        sys.exit(code if code >= 0 else 128 - code)
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode if error.returncode > 0 else 128 - error.returncode)
    except (OSError, ValueError, KeyError) as error:
        print("CI_PR_LEAN_ERROR " + str(error), file=sys.stderr)
        sys.exit(2)
