"""Build scoped Lean evidence and check the candidate and its selected delta."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

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
        environment = dict(os.environ, FULL_BUILD="0")
        # Hosted current restores the judge for this source key or builds it on a fresh runner.
        if ((programs or targets) and environment.get("GITHUB_EVENT_NAME") in ("pull_request", "push")
                and not environment.get("STRATALINT_LEAN_PRODUCER_DLL")):
            producer = subprocess.run(["bash", "tools/scripts/workflow/judge-lean-producer.sh",
                                       str(Path(CLI).parent)], stdout=subprocess.PIPE, text=True, check=True)
            environment["STRATALINT_LEAN_PRODUCER_DLL"] = producer.stdout.strip()
        if programs:
            subprocess.run(["bash", "tools/scripts/worktree/lean-cache-run.sh", "lake",
                            "-d", "tools/lean-inspector-reg", "build",
                            *["+" + module for module in programs]], env=environment, check=True)
        reports = []
        for index, (targets, selected_paths) in enumerate(report_batches(scope)):
            batch = directory / str(index)
            batch.mkdir()
            report = batch / "scoped-lean-report.json"
            subprocess.run(["bash", "tools/scripts/report/lean-report-scoped.sh", "--prepare",
                            "--targets", " ".join(targets), "--output", str(report)],
                           env=dict(environment, STRATALINT_SCOPED_REPORT_THREADS="1"), check=True)
            reports.append(str(report))
            roots = batch / "roots"
            documents = batch / "documents"
            write_paths(roots, [name.replace(".", "/") + ".lean" for name in targets])
            write_paths(documents, selected_paths)
            scribe_environment = os.environ.copy()
            if ("DOTNET_GCConserveMemory" not in scribe_environment
                    and "COMPlus_GCConserveMemory" not in scribe_environment):
                scribe_environment["DOTNET_GCConserveMemory"] = "9"
            subprocess.run([*SCRIBE, "content-check", "--scoped", "--report", str(report),
                            "--targets-from", str(roots), "--paths-from", str(documents)],
                           env=scribe_environment, check=True)

        report_list = directory / "reports"
        write_paths(report_list, reports)
        arguments = ["--changed-paths-from", str(manifest), "--scoped-reports-from", str(report_list)]
        scope_arguments = ["--scope-base", base] if base and "Meta/tools-release.json" in changed else []
        if protected_base is None:
            return subprocess.run(["dotnet", CLI, "check-current", *arguments, *scope_arguments]).returncode
        return subprocess.run(["dotnet", CLI, "check-current-and-delta", "--protected-base", protected_base,
                               *arguments, *scope_arguments]).returncode


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--base")
    parser.add_argument("--protected-base")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--write-plan", type=Path)
    modes.add_argument("--read-plan", type=Path)
    parser.add_argument("--cache-targets", action="store_true")
    args = parser.parse_args()
    try:
        if args.cache_targets:
            if args.write_plan:
                parser.error("--cache-targets cannot write a build plan")
            scope = (json.loads(args.read_plan.read_text()) if args.read_plan
                     else plan_scope(args.manifest, args.base or args.protected_base))
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
