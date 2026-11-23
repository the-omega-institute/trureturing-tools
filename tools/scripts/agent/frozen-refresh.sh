#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 0 || ! -f lakefile.toml || ! -f lean-toolchain ]]; then
  echo 'usage: tools/scripts/agent/frozen-refresh.sh (from the consumer root)' >&2
  exit 2
fi

exec python3 -B - <<'PY'
from pathlib import Path
import os
import re
import subprocess
import tempfile

state = Path("Golden/Frozen/state")
module_path = re.compile(r"(?:[A-Z][A-Za-z0-9_]*/)*[A-Z][A-Za-z0-9_]*\.lean")
paths = []
if state.is_symlink() or state.exists() and not state.is_dir():
    raise SystemExit(f"FROZEN_REFRESH_INVALID state root {state}")
for entry in sorted(state.rglob("*")):
    if entry.is_dir() and not entry.is_symlink():
        continue
    module = entry.relative_to(state).as_posix().removesuffix(".json")
    if (entry.is_symlink() or not entry.is_file() or entry.suffix != ".json"
            or not module_path.fullmatch(module)):
        raise SystemExit(f"FROZEN_REFRESH_INVALID state path {entry}")
    if not Path(module).is_file():
        raise SystemExit(f"FROZEN_REFRESH_INVALID missing source {module}")
    paths.append(module)
if not paths:
    print("FROZEN_REFRESH_OK selectors=0")
    raise SystemExit(0)

project = "tools/StrataLint.Cli/StrataLint.Cli.csproj"
cli = "tools/StrataLint.Cli/bin/Release/net10.0/StrataLint.dll"
batch_size = 32
try:
    subprocess.run(["dotnet", "build", project, "-c", "Release", "--nologo"], check=True)
    producer = Path("tools/StrataLint.Lean/bin/Release/net10.0/StrataLint.Lean.dll")
    os.environ.setdefault("STRATALINT_LEAN_PRODUCER_DLL", str(producer.resolve()))
    with tempfile.TemporaryDirectory(prefix="frozen-refresh-") as temporary:
        for index in range(0, len(paths), batch_size):
            batch = paths[index:index + batch_size]
            targets = " ".join(path.removesuffix(".lean").replace("/", ".") for path in batch)
            report = Path(temporary) / "report.json"
            print(f"FROZEN_REFRESH_BATCH selectors={index + 1}-{index + len(batch)}/{len(paths)}", flush=True)
            subprocess.run(["python3", "-B", "tools/lean-inspector/build_inputs.py",
                            "prepare", "--targets", targets], check=True)
            subprocess.run(["bash", "tools/scripts/report/lean-report-scoped.sh",
                            "--targets", targets, "--output", str(report)], check=True)
            selectors = [part for path in batch for part in ("--selector", path)]
            subprocess.run(["dotnet", cli, "ledger-align", "--refresh-annotations",
                            "--candidate-lean-report", str(report), *selectors], check=True)
except subprocess.CalledProcessError as error:
    raise SystemExit(f"FROZEN_REFRESH_FAILED exit={error.returncode}")
print(f"FROZEN_REFRESH_OK selectors={len(paths)}")
PY
