#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 2 ]]; then
  echo 'usage: frozen-dependencies.sh REQUEST.json RESPONSE.json' >&2
  exit 2
fi

exec bash tools/scripts/worktree/lean-cache-run.sh --prebuilt \
  "${LAKE_BIN:-lake}" -d tools/lean-inspector-reg script run leanInspector/frozenDependencies "$1" "$2"
