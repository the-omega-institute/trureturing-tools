"""Exercise the producer's test suites and its pinned consumer contracts."""

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


def check(*arguments):
    print("+", *arguments, flush=True)
    subprocess.run(arguments, check=True)


def standalone():
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
    for project in selected:
        if project.stem == "StrataLint.DeclaredTemplate.Tests":
            # Prepare the two real source/mirror pairs before the assertion's bounded report reader.
            modules = ("D5.S3.Fourier.IntegerCharacterCoercivity", "D5.S3.TotalVariation.PrimitiveBridgeCancellation")
            reports = [f"+{prefix}{module}:report" for module in modules for prefix in ("", "Reg.")]
            check("bash", "tools/scripts/worktree/lean-cache-run.sh", "lake", "-d",
                  "tools/lean-inspector-reg", "build", *reports)
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
