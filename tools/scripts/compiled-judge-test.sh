#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
targets=(regInspector/LeanInformationAuditRegTests regInspector/compiledJudgeTests leanInspector/reportInspector leanInspector/inputDiscovery)
if [[ "${1:-}" == --print-lean-targets ]]; then
  printf '%s\n' "${targets[@]}"
  exit 0
fi
make -C "$ROOT" lean LEAN_TARGETS="${targets[*]}"
cd "$ROOT"
compute=()
[[ -z "${STRATALINT_CI_COMPUTE_BUDGET:-}" || "${STRATALINT_CI_COMPUTE_ACTIVE:-0}" == 1 ]] || compute=(python3 tools/scripts/workflow/ci_compute.py run --)
${compute[@]+"${compute[@]}"} lake -d tools/lean-inspector-reg env .lake/build/lean-inspector/reg/bin/compiledJudgeTests
cd "$ROOT/tools/lean-inspector/tests"
${compute[@]+"${compute[@]}"} python3 -B -m unittest test_typed_capacity -v
