"""Exercise the producer's test suites and its pinned consumer contracts."""

import json
import os
from pathlib import Path
import subprocess
import sys
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
    check(sys.executable, "-m", "unittest", "discover", "-s", "tools/scripts/worktree",
          "-p", "test_lean_compile_layers.py")
    for name in STANDALONE:
        check("bash", "tools/scripts/dotnet-test.sh", "--project-assembly",
              f"tools/tests/{name}/{name}.csproj", "--no-build")
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


def install_lean():
    check("bash", "tools/scripts/workflow/install-lean-toolchain.sh", "lean-toolchain")
    os.environ["PATH"] = str(Path.home() / ".elan/bin") + os.pathsep + os.environ["PATH"]


def test_project(name, *args):
    check("bash", "tools/scripts/dotnet-test.sh", "--project-assembly",
          f"tools/tests/{name}/{name}.csproj", *args)


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


if __name__ == "__main__":
    if sys.argv[1:] == ["standalone"]:
        standalone()
    elif len(sys.argv) == 3 and sys.argv[1] == "contract":
        contract(sys.argv[2])
    else:
        sys.exit("usage: test-suites.py standalone | contract SHARD")
