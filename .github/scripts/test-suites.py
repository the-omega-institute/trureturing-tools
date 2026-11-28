"""Exercise the producer's test suites and its pinned consumer contracts."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


STANDALONE = (
    "StrataLint.CliIntegration.Tests",
    "StrataLint.Configuration.Tests",
    "StrataLint.Engineering.Tests",
    "StrataLint.HeaderScript.Tests",
    "StrataLint.ReportSupervisor.Tests",
    "StrataLint.ResourceObservation.Tests",
    "StrataLint.Rules.Tests",
    "StrataLint.Runtime.Tests",
    "StrataLint.TestEvidence.Tests",
    "Trureturing.Truth.Tests",
)

PERFORMANCE_GOALS = Path(__file__).with_name("ci-performance.json")

FOURIER_MODULES = ("D5.S3.Fourier.IntegerCharacterCoercivity",
                   "D5.S3.TotalVariation.PrimitiveBridgeCancellation")


def lake_modules(*targets):
    command = ["lake", "-d", "tools/lean-inspector-reg", "--json", "query", *targets]
    output = subprocess.check_output(command, text=True, env=dict(os.environ, MATHLIB_NO_CACHE_ON_UPDATE="1"))
    return {module for line in output.splitlines() for module in json.loads(line)}


def prepare_mathlib(roots, compiled_judge):
    modules = set(roots)
    if compiled_judge:
        modules.update(lake_modules("regInspector/LeanInformationAuditRegTests:modules"))
        modules.update(("Inspector", "LeanInformationAudit.Contract.InputDiscovery"))
    imports = lake_modules(*(f"+{module}:transImports" for module in sorted(modules)))
    mathlib = ["Mathlib"] if "Mathlib" in imports else sorted(module for module in imports if module.startswith("Mathlib."))
    if mathlib:
        print(f"Preparing the pinned Mathlib closure ({len(mathlib)} modules)", flush=True)
        subprocess.run(["lake", "exe", "cache", "get", *mathlib], check=True)


def check(*arguments):
    print("+", *arguments, flush=True)
    subprocess.run(arguments, check=True)


def standalone():
    check(sys.executable, ".github/scripts/test-preview-source.py")
    check(sys.executable, ".github/scripts/test-adopt-tools.py")
    check(sys.executable, "-B", ".github/scripts/truth_release/test_report.py")
    check(sys.executable, "-m", "unittest", "discover", "-s", "tools/scripts/worktree",
          "-p", "test_lean_compile_layers.py")
    # Protocol fixtures run without a consumer checkout. The DAG test uses a
    # real, pinned, zero-dependency Lake project.
    install_lean()
    for suite in ("test_lean_cache_generation.py", "test_lean_generation_*.py", "test_lean_module_*.py"):
        check(sys.executable, "-m", "unittest", "discover", "-s", "tools/scripts/worktree", "-p", suite)
    for name in STANDALONE:
        test_project(name, "--no-build")
    for proof in ("banned-api-proof", "capability-proof"):
        check("make", "-C", "tools", "compile-proof", f"PROOF={proof}")
    check("bash", "tools/scripts/agent/integration-mirror-test.sh")
    check(sys.executable, "-m", "unittest", "discover", "-s", "tools/scripts/lean-fork-contribute/tests")


LEAN_PROJECT = "StrataLint.Lean.Tests"
ARCHITECTURE_PROJECT = "StrataLint.ArchitectureTests"
TEMPLATE_PROJECT = "StrataLint.DeclaredTemplate.Tests"
LEAN_CLASSES = {
    "recovery": ("InspectorNativeRecoveryTests", "InspectorNativePackagingTests"),
    "scoped": ("LeanReportScopedTests", "InspectorNativeInterfaceTests"),
    "modules": ("InspectorNativeModuleCacheTests", "InspectorNativeInvalidationTests"),
}
EVIDENCE_PREPARED = os.environ.get("CI_DOTNET_EVIDENCE_PREPARED") == "1"


def install_lean():
    check("bash", "tools/scripts/workflow/install-lean-toolchain.sh", "lean-toolchain")
    os.environ["PATH"] = str(Path.home() / ".elan/bin") + os.pathsep + os.environ["PATH"]


def test_project(name, *args):
    global EVIDENCE_PREPARED
    inherited = os.environ.get("CI_DOTNET_EVIDENCE_PREPARED")
    try:
        if EVIDENCE_PREPARED:
            os.environ["CI_DOTNET_EVIDENCE_PREPARED"] = "1"
        else:
            os.environ.pop("CI_DOTNET_EVIDENCE_PREPARED", None)
        check("bash", "tools/scripts/dotnet-test.sh", "--project-assembly",
              f"tools/tests/{name}/{name}.csproj", *args)
        EVIDENCE_PREPARED = True
    finally:
        if inherited is None:
            os.environ.pop("CI_DOTNET_EVIDENCE_PREPARED", None)
        else:
            os.environ["CI_DOTNET_EVIDENCE_PREPARED"] = inherited


def contract(shard):
    if shard == "judge":
        install_lean()
        prepare_mathlib([], True)
        check("make", "-C", "tools", "compiled-judge-test")
    elif shard == "templates":
        install_lean()
        roots = [prefix + module for module in FOURIER_MODULES for prefix in ("", "Reg.")]
        prepare_mathlib(roots, False)
        check("make", "lean-report-prepare", "LEAN_TARGETS=" + " ".join(roots))
        check("bash", "tools/scripts/worktree/lean-cache-run.sh", "lake", "-d",
              "tools/lean-inspector-reg", "build", *(f"+{module}:report" for module in roots))
        test_project(TEMPLATE_PROJECT)
    elif shard == "architecture":
        test_project(ARCHITECTURE_PROJECT)
    elif shard == "frozen":
        install_lean()
        check("lake", "exe", "cache", "get", "Mathlib.Data.Nat.Prime.Basic")
        check(sys.executable, "-B", "tools/tests/StrataLint.Lean.Tests/FrozenDependencies/run.py")
    elif shard in LEAN_CLASSES or shard == "remaining":
        install_lean()
        if shard == "remaining":
            # The complement also runs newly added tests in this assembly.
            filters = (f"FullyQualifiedName!~.{name}." for group in LEAN_CLASSES.values() for name in group)
            expression = "&".join(filters)
        else:
            expression = "|".join(f"FullyQualifiedName~.{name}." for name in LEAN_CLASSES[shard])
        test_project(LEAN_PROJECT, "--filter", expression)
    else:
        if shard not in {"0", "1", "2", "3"}:
            raise ValueError(f"unknown consumer fixture shard {shard}")
        dedicated = {ARCHITECTURE_PROJECT, TEMPLATE_PROJECT, LEAN_PROJECT}
        selected = sorted(p.stem for p in Path("tools/tests").glob("*/*.csproj")
                          if p.stem not in STANDALONE and p.stem not in dedicated
                          and ET.parse(p).findtext(".//IsTestProject") == "true")[int(shard)::4]
        if shard == "0" or {"StrataLint.Cache.Native.Tests", "StrataLint.ScriptTests", "StrataLint.Tests"}.intersection(selected):
            install_lean()
        for name in selected:
            test_project(name)
        if shard == "0":
            check(sys.executable, "-B", "tools/scripts/worktree/test_lean_release_volumes.py")
            check("make", "-C", "tools", "selftest")
            check("bash", "tools/scripts/workflow/ci-entry-test.sh")
            check(sys.executable, "-m", "unittest", "discover", "-s", "tools/scripts/workflow", "-p", "test_ci_*.py")


def performance_key(arguments):
    if arguments == ["standalone"]:
        return "standalone"
    if len(arguments) == 2 and arguments[0] == "contract":
        return "contract:frozen" if arguments[1] == "frozen" else "contract"
    return None


def report_performance(key, elapsed):
    goals = json.loads(PERFORMANCE_GOALS.read_text())
    target = goals["targets"][key]
    status = "within-target" if elapsed <= target["target_seconds"] else "over-target"
    if elapsed > target["budget_seconds"]:
        status = "over-budget"
    print(
        "CI_PERFORMANCE "
        f"name={key} elapsed_seconds={elapsed:.1f} "
        f"target_seconds={target['target_seconds']} "
        f"budget_seconds={target['budget_seconds']} status={status}",
        flush=True,
    )
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("### CI performance\n\n| Suite | Elapsed | Target | Budget | Status |\n")
            stream.write("| --- | ---: | ---: | ---: | --- |\n")
            stream.write(
                f"| `{key}` | {elapsed:.1f}s | {target['target_seconds']}s | "
                f"{target['budget_seconds']}s | {status} |\n"
            )


if __name__ == "__main__":
    key = performance_key(sys.argv[1:])
    if key is None:
        sys.exit("usage: test-suites.py standalone | contract SHARD")
    started = time.monotonic()
    try:
        if sys.argv[1:] == ["standalone"]:
            standalone()
        else:
            contract(sys.argv[2])
    finally:
        report_performance(key, time.monotonic() - started)
