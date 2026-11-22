#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 4 ]]; then
  echo "usage: scribe-content-checks.sh REPORT SCRIBE_DLL PATHS_FILE TARGETS_FILE" >&2
  exit 2
fi
REPORT="$1"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
SCRIBE_DLL="$2"
PATHS_FILE="$3"
TARGETS_FILE="$4"
[[ -s "$REPORT" && -f "$SCRIBE_DLL" ]] || { echo "scribe-content-checks: report or candidate DLL missing" >&2; exit 2; }
cd "$REPO_ROOT"
if [[ ! -f "$PATHS_FILE" || ! -r "$PATHS_FILE" ]]; then
  echo "scribe-content-checks: PATHS_FILE must be a readable regular file: $PATHS_FILE" >&2
  exit 2
fi
if [[ ! -f "$TARGETS_FILE" || ! -r "$TARGETS_FILE" ]]; then
  echo "scribe-content-checks: TARGETS_FILE must be a readable regular file: $TARGETS_FILE" >&2
  exit 2
fi
STRATALINT_LEAN_REPORT="$REPORT" exec dotnet "$SCRIBE_DLL" content-check --scoped --report "$REPORT" \
  --targets-from "$TARGETS_FILE" --paths-from "$PATHS_FILE"
