#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 2 ]]; then
  echo 'usage: frozen-dependencies.sh REQUEST.json RESPONSE.json' >&2
  exit 2
fi

request="$(python3 -c 'import pathlib, sys; print(pathlib.Path(sys.argv[1]).resolve())' "$1")"
response="$(python3 -c 'import pathlib, sys; print(pathlib.Path(sys.argv[1]).absolute())' "$2")"
temporary="$(mktemp -d "$(dirname "$response")/.frozen-dependencies.XXXXXX")"
trap 'rm -rf "$temporary"' EXIT

bash tools/scripts/worktree/lean-cache-run.sh --prebuilt \
  "${LAKE_BIN:-lake}" -d tools/lean-inspector-reg script run leanInspector/frozenDependencies "$request" "$temporary/response.json"

# Legacy source trees do not supply the portable measurement entry point.
python3 - "$temporary/response.json" <<'PYTHON'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
modules = json.loads(path.read_text())
portable = Path("src/OmegaChecks/portable.py")
for module in modules:
    if portable.is_file() and type(module.get("strictEligible")) is not bool:
        raise ValueError("current portable source requires a paired measurement response")
    module.setdefault("strictEligible", False)
path.write_text(json.dumps(modules, separators=(",", ":")) + "\n")
PYTHON
mv -f "$temporary/response.json" "$response"
