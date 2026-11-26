#!/usr/bin/env bash
# Run in the content repository at the commit selected by the workflow.
set -euo pipefail
assets=${1:?release asset directory required}
scripts="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tool_root="$(git -C "$scripts" rev-parse --show-toplevel)"
producer="$(git -C "$tool_root" rev-parse HEAD)"
temp="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/truth-release.XXXXXXXX")"
trap 'rm -rf "$temp"' EXIT

project=tools/StrataLint.Cli/StrataLint.Cli.csproj
(cd "$tool_root" && dotnet restore "$project" --locked-mode &&
  dotnet build "$project" --no-restore --configuration Release)
issuer="$tool_root/tools/StrataLint.Cli/bin/Release/net10.0/StrataLint.dll"

report="$temp/raw-lean-report.json"
python3 -B "$scripts/report.py" "$report"
pack="$temp/scribe-resources.zip"
if [[ -n "${TRUTH_RELEASE_SCRIBE_CACHE:-}" ]]; then
  export TRUTH_RELEASE_SCRIBE_RECEIPT="$temp/scribe-cache-result"
  TRUTH_RELEASE_SCRIBE_INPUT="$(
    { git ls-tree HEAD -- Meta/tools-release.json Golden/Projection global.json; dotnet --version; } | git hash-object --stdin
  )"
  export TRUTH_RELEASE_SCRIBE_INPUT
fi
dotnet run --project tools/StrataLint.Scribe/StrataLint.Scribe.csproj --configuration Release -- \
  resources pack --out "$pack"
digest="$(unzip -p "$pack" manifest.json | jq -er .totalSha256)"
if [[ -f "$temp/scribe-cache-result" ]]; then
  cat "$temp/scribe-cache-result"
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo 'scribe_cache_ready=true' >> "$GITHUB_OUTPUT"
  fi
fi
base=()
if [[ -n "${TRUTH_RELEASE_BASE:-}" && -f "$TRUTH_RELEASE_BASE/reuse.json" ]]; then
  base=(--base-release "$TRUTH_RELEASE_BASE")
fi
mkdir -p "$assets"
dotnet "$issuer" truth-release --candidate-lean-report "$report" --out "$temp/bundle" \
  --dag-output "$assets/DAG.md" --scribe-pack "$pack" --scribe-pack-digest "$digest" \
  --producer-package-commit "$producer" --produced-at "$(git show -s --format=%cI HEAD)" \
  --commit-on-protected-dev true "${base[@]}"
python3 "$scripts/publish.py" prepare "$temp/bundle" "$assets"
