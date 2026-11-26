"""Build scoped Lean evidence and check the candidate and its selected delta."""

import argparse
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


def distributed_plan(scope):
    scope_modules(scope)
    batches = list(report_batches(scope))
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
    return dict(scope, targets=targets, paths=paths, programs=scope["programs"] if index == 0 else [],
                definitions={p: scope["definitions"][p] for p in paths if p in scope["definitions"]})


def batch_receipt(directory, plan_identity, index):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lean-inspector"))
    from publication import SUFFIXES

    names = ["scoped-lean-report.json" + suffix for suffix in SUFFIXES]
    return dict(schema=1, **plan_identity, batch=index, files={name: file_hash(directory / name) for name in names})


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
        check_batch(scratch, scratch, selected["targets"], selected["paths"], environment)
        receipt = batch_receipt(scratch, identity, index)
        staged = scratch / "bundle"
        staged.mkdir()
        for name in receipt["files"]:
            (scratch / name).rename(staged / name)
        (staged / "receipt.json").write_text(json.dumps(receipt))
        staged.rename(destination)



def read_batches(root, count, identity):
    if {entry.name for entry in root.iterdir()} != {f"batch-{i}" for i in range(count)}:
        raise ValueError("incomplete or unexpected report batch artifacts")
    reports = []
    for index in range(count):
        directory = root / f"batch-{index}"
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(f"invalid directory for report batch {index}")
        if any(path.is_symlink() or not path.is_file() for path in directory.iterdir()):
            raise ValueError(f"invalid file in report batch {index}")
        expected = batch_receipt(directory, identity, index)
        if {path.name for path in directory.iterdir()} != {"receipt.json", *expected["files"]}:
            raise ValueError(f"unexpected files in report batch {index}")
        if json.loads((directory / "receipt.json").read_text()) != expected:
            raise ValueError(f"report batch {index} does not match this plan and checkout")
        reports.append(directory / "scoped-lean-report.json")
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
    reports = read_batches(root, len(batches), identity)
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
    batches.add_argument("--batch-matrix", action="store_true")
    batches.add_argument("--batch", type=int)
    batches.add_argument("--finalize", action="store_true")
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args()
    try:
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
