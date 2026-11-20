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
    "StrataLint.LeanReportScript.Tests",
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


def contract(shard, shard_count):
    units = json.loads(Path("Meta/ci-units.json").read_text())["units"]
    # The complementary Fourier source evidence requires the actual downstream Lean report.
    registered = {u["project"] for u in units.values() if u["project"]}
    for unit in units.values():
        for arg in unit["command"].split():
            if arg.startswith("TEST_PROJECT="):
                registered.add(arg.removeprefix("TEST_PROJECT="))
    discovered = {str(p) for p in Path("tools/tests").glob("*/*.csproj")
                  if ET.parse(p).findtext(".//IsTestProject") == "true"}
    additional = sorted(project for project in discovered - registered
                        if Path(project).stem not in STANDALONE)
    for project in additional[shard::shard_count]:
        check("bash", "tools/scripts/dotnet-test.sh", "--project-assembly", project)
    downstream = {"filemap", "tests-fib"}
    producer_commands = {"banned-api-proof", "capability-proof", "tests-integration-mirror"}
    fixture = [name for name, unit in sorted(units.items()) if name not in downstream | producer_commands
               and Path(unit["project"]).stem not in STANDALONE]
    selected = fixture[shard::shard_count]
    if not selected and not additional[shard::shard_count]:
        raise ValueError(f"empty consumer fixture shard {shard}")
    if any(units[name]["lean"] != "none" for name in selected):
        check("bash", "tools/scripts/workflow/install-lean-toolchain.sh", "lean-toolchain")
        os.environ["PATH"] = str(Path.home() / ".elan/bin") + os.pathsep + os.environ["PATH"]
    for name in selected:
        check("make", "ci-unit", f"UNIT={name}")


if __name__ == "__main__":
    if sys.argv[1:] == ["standalone"]:
        standalone()
    elif len(sys.argv) == 4 and sys.argv[1] == "contract":
        contract(int(sys.argv[2]), int(sys.argv[3]))
    else:
        sys.exit("usage: test-suites.py standalone | contract SHARD SHARD_COUNT")
