#!/usr/bin/env bash
# Run in the clean, immutable source checkout after the workflow checks protected dev.
set -euo pipefail
out=${1:?output directory required}
checks=${2:?verified push checks file required}
scripts="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tool_root="$(git -C "$scripts" rev-parse --show-toplevel)"
producer="$(git -C "$tool_root" rev-parse HEAD)"
[[ "$producer" == "${TRUTH_TOOLS_COMMIT:?fixed tools revision required}" ]]
source_commit="$(git rev-parse --verify HEAD)"
[[ "$source_commit" == "${SOURCE_COMMIT:?verified source commit required}" ]]
git diff --quiet HEAD --

required=()
while IFS= read -r check || [[ -n "$check" ]]; do
  [[ "$check" == ?*=success ]] || { echo "Expected a successful required check: $check" >&2; exit 1; }
  required+=(--required-check "$check")
done < "$checks"
[[ ${#required[@]} -gt 0 ]] || { echo 'Missing verified checks' >&2; exit 1; }

mkdir -p "$(dirname "$out")"
temp="$(mktemp -d "$(dirname "$out")/.truth-release.XXXXXXXX")"
trap 'rm -rf "$temp"' EXIT
mkdir "$temp/release"
project="$tool_root/tools/StrataLint.Cli/StrataLint.Cli.csproj"
(cd "$tool_root" && dotnet restore "$project" --locked-mode &&
  dotnet build "$project" --no-restore --configuration Release)
issuer="$tool_root/tools/StrataLint.Cli/bin/Release/net10.0/StrataLint.dll"

report="$temp/raw-lean-report.json"
python3 -B "$scripts/report.py" "$report"

make scribe-release
pack="$PWD/Generated/scribe-release/scribe-resources.zip"
digest="$(unzip -p "$pack" manifest.json | jq -er .totalSha256)"
produced_at="$(git show -s --format=%cI "$source_commit")"
dotnet "$issuer" truth-release --candidate-lean-report "$report" --out "$temp/release/bundle" --dag-output "$temp/release/DAG.md" \
  --scribe-pack "$pack" --scribe-pack-digest "$digest" --producer-package-commit "$producer" \
  --produced-at "$produced_at" --commit-on-protected-dev true "${required[@]}"
expected="$(jq -er .dag_md_sha256 "$temp/release/bundle/source-snapshot.v1.json")"
actual="$(shasum -a 256 "$temp/release/DAG.md" | cut -d' ' -f1)"
[[ "$expected" == "sha256:$actual" ]] || { echo 'DAG.md does not match source snapshot' >&2; exit 1; }
[[ ! -e "$out" ]] || { echo "Output already exists: $out" >&2; exit 1; }
mv "$temp/release" "$out"
