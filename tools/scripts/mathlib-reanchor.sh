#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
PROJECT="$ROOT/tools/StrataLint.Cli/StrataLint.Cli.csproj"
cd "$ROOT"
[[ ( $# -eq 1 || $# -eq 2 ) && -n "$1" ]] || { echo "usage: mathlib-reanchor.sh BASE [OLD_REPORT]" >&2; exit 2; }
make -C "$ROOT" check-lean-report
base_sha="$(git -C "$ROOT" merge-base HEAD "$1")"
if [[ -f Meta/tools-release.json ]]; then
  python3 .github/scripts/install-tools.py --cache-revision "$base_sha"
fi
reanchor_args=(ledger-reanchor-mathlib --base "$base_sha")
if [[ -n "${2:-}" ]]; then
  reanchor_args+=(--old-report "$2")
fi
dotnet run --project "$PROJECT" --configuration Release -- \
  "${reanchor_args[@]}"
