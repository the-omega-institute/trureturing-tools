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
    check(sys.executable, "-m", "unittest", "discover", "-s", "tools/scripts/worktree",
          "-p", "test_lean_compile_layers.py")
    for name in STANDALONE:
        check("bash", "tools/scripts/dotnet-test.sh", "--project-assembly",
              f"tools/tests/{name}/{name}.csproj", "--no-build")
    for proof in ("banned-api-proof", "capability-proof"):
        check("make", "-C", "tools", "compile-proof", f"PROOF={proof}")
    check("bash", "tools/scripts/agent/integration-mirror-test.sh")
    check(sys.executable, "-m", "unittest", "discover", "-s", "tools/scripts/lean-fork-contribute/tests")


def contract(shard, shard_count):
    projects = sorted(p for p in Path("tools/tests").glob("*/*.csproj")
                      if p.stem not in STANDALONE and ET.parse(p).findtext(".//IsTestProject") == "true")
    selected = projects[shard::shard_count]
    if not selected:
        raise ValueError(f"empty consumer fixture shard {shard}")
    lean_projects = {"StrataLint.Cache.Native.Tests", "StrataLint.DeclaredTemplate.Tests",
                     "StrataLint.Lean.Tests", "StrataLint.ScriptTests", "StrataLint.Tests"}
    if shard == 0 or any(project.stem in lean_projects for project in selected):
        check("bash", "tools/scripts/workflow/install-lean-toolchain.sh", "lean-toolchain")
        os.environ["PATH"] = str(Path.home() / ".elan/bin") + os.pathsep + os.environ["PATH"]
    fourier = any(project.stem == "StrataLint.DeclaredTemplate.Tests" for project in selected)
    roots = [prefix + module for module in FOURIER_MODULES for prefix in ("", "Reg.")] if fourier else []
    if roots or shard == 0:
        prepare_mathlib(roots, shard == 0)
    for project in selected:
        if project.stem == "StrataLint.DeclaredTemplate.Tests":
            check("make", "lean-report-prepare", "LEAN_TARGETS=" + " ".join(roots))
            check("bash", "tools/scripts/worktree/lean-cache-run.sh", "lake", "-d",
                  "tools/lean-inspector-reg", "build", *(f"+{module}:report" for module in roots))
        check("bash", "tools/scripts/dotnet-test.sh", "--project-assembly", str(project))
    if shard == 0:
        check("make", "-C", "tools", "selftest")
        check("make", "-C", "tools", "compiled-judge-test")
        check("bash", "tools/scripts/workflow/ci-entry-test.sh")
        check(sys.executable, "-m", "unittest", "discover", "-s", "tools/scripts/workflow", "-p", "test_ci_*.py")


if __name__ == "__main__":
    if sys.argv[1:] == ["standalone"]:
        standalone()
    elif len(sys.argv) == 4 and sys.argv[1] == "contract":
        contract(int(sys.argv[2]), int(sys.argv[3]))
    else:
        sys.exit("usage: test-suites.py standalone | contract SHARD SHARD_COUNT")
