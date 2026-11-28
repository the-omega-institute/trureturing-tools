#!/bin/bash
set -euo pipefail

SCRIPT_DIRECTORY="$(dirname "${BASH_SOURCE[0]}")"
ROOT="$(cd "$SCRIPT_DIRECTORY/../.." && pwd -P)"
project_identity=false
if [[ "${1:-}" == "--project-assembly" ]]; then
  shift
  [[ "${1:-}" == *.csproj ]] || { echo "--project-assembly requires a test project" >&2; exit 2; }
  project_identity=true
fi
restore_args=()
[[ "${CI_DOTNET_PREPARED:-0}" != 1 ]] || restore_args=(--no-restore)
# Only this CI entry's projects were restored. Test-created projects retain
# their own restore behavior and execution/hang budgets.
unset CI_DOTNET_PREPARED
if [[ -n "${TEST_RESULTS_DIRECTORY:-}" ]]; then
  mkdir -p "$TEST_RESULTS_DIRECTORY"
  RESULTS_DIRECTORY="$(mktemp -d "$TEST_RESULTS_DIRECTORY/invocation.XXXXXXXX")"
else
  RESULTS_DIRECTORY="$(mktemp -d "${TMPDIR:-/tmp}/stratalint-test-results.XXXXXXXX")"
fi
completed=0

finish() {
  local rc="$1"
  # Bash 3.2 reports zero to an EXIT trap after a nounset expansion error. The
  # completion marker keeps that shell-level failure fail-closed.
  if [[ "$rc" -eq 0 && "$completed" -ne 1 ]]; then
    rc=1
  fi
  trap - EXIT
  if [[ -z "${TEST_RESULTS_DIRECTORY:-}" ]]; then rm -rf -- "$RESULTS_DIRECTORY"; fi
  exit "$rc"
}
trap 'finish "$?"' EXIT

dotnet test "$@" ${restore_args[@]+"${restore_args[@]}"} --configuration Release --verbosity normal -p:RestoreLockedMode=true -nr:false \
  --logger 'trx;LogFilePrefix=canonical' --results-directory "$RESULTS_DIRECTORY"

OWNER_ASSEMBLY_ARGS=()
test_target="${1:-$ROOT/tools/StrataLint.sln}"
if [[ "$test_target" == -* ]]; then test_target="$ROOT/tools/StrataLint.sln"; fi
filtered=false
for argument in "$@"; do
  if [[ "$argument" == "--filter" || "$argument" == --filter=* ]]; then
    filtered=true
  fi
done

# The selected test project may not build the evidence validator. A caller that
# already built the complete solution can carry that fact across project runs.
if [[ "${CI_DOTNET_EVIDENCE_PREPARED:-0}" != 1 ]]; then
  dotnet build "$ROOT/tools/StrataLint.TestEvidence/StrataLint.TestEvidence.csproj" --configuration Release \
    ${restore_args[@]+"${restore_args[@]}"} -p:RestoreLockedMode=true -nr:false -nologo --verbosity quiet
fi

if [[ "$project_identity" == true ]]; then
  # The standalone producer has no consumer engineering-project registry.
  owner_assembly="$(dotnet msbuild "$test_target" -nologo -p:Configuration=Release -getProperty:AssemblyName)"
  [[ -n "$owner_assembly" ]] || { echo "test project has no assembly identity" >&2; exit 2; }
  OWNER_ASSEMBLY_ARGS=(--required-assembly "$owner_assembly")
else
  owner_assemblies="$(dotnet run \
    --project "$ROOT/tools/StrataLint.TestEvidence/StrataLint.TestEvidence.csproj" \
    --configuration Release --no-build --no-launch-profile -- \
    list-test-owner-assemblies --repository "$ROOT" --target "$test_target" --filtered "$filtered")"
  while IFS= read -r owner_assembly; do
    [[ -n "$owner_assembly" ]] || continue
    OWNER_ASSEMBLY_ARGS+=(--required-assembly "$owner_assembly")
  done <<< "$owner_assemblies"
fi

dotnet run \
  --project "$ROOT/tools/StrataLint.TestEvidence/StrataLint.TestEvidence.csproj" \
  --configuration Release --no-build --no-launch-profile -- \
  verify-trx --results-directory "$RESULTS_DIRECTORY" \
  ${OWNER_ASSEMBLY_ARGS[@]+"${OWNER_ASSEMBLY_ARGS[@]}"}

completed=1
