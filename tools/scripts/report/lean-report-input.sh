#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

COMMAND="${1:-}"
if [[ -n "$COMMAND" ]]; then shift; fi
# The repository (R) and pair provenance (A) are different canonical preimages.
# Keep their byte definitions here for producers and bundle transport alike.
input_coordinates() {
  local selection
  selection="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lean-report-selection.py"
  python3 -B "$selection" coordinates "$@"
}
if [[ "$COMMAND" == "coordinates" ]]; then
  input_coordinates "$@"
  exit $?
fi
REPOSITORY=""
REPORT=""
TARGETS=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repository|--report|--targets)
      [[ $# -ge 2 && -n "$2" ]] || { echo "lean-report-input: $1 requires a value" >&2; exit 2; }
      case "$1" in --repository) REPOSITORY="$2" ;; --report) REPORT="$2" ;; --targets) TARGETS="$2" ;; esac
      shift 2 ;;
    *) echo "lean-report-input: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

[[ "$COMMAND" == "address" || "$COMMAND" == "modules" \
  || "$COMMAND" == "producer-paths" || "$COMMAND" == "scribe-producer-paths" || "$COMMAND" == "verify-scoped" ]] \
  || { echo "usage: lean-report-input.sh address|modules|producer-paths|scribe-producer-paths|verify-scoped --repository DIR [--report FILE] [--targets ROOTS]" >&2; exit 2; }
[[ -n "$REPOSITORY" && "$REPOSITORY" == /* && -d "$REPOSITORY" ]] \
  || { echo "lean-report-input: --repository requires an absolute directory" >&2; exit 2; }
REPOSITORY="$(cd "$REPOSITORY" && pwd -P)"
if [[ "$COMMAND" == "verify-scoped" ]]; then
  [[ "$REPORT" == /* && "$TARGETS" =~ [^[:space:]] ]] \
    || { echo 'lean-report-input: verify-scoped requires an absolute --report and nonempty --targets' >&2; exit 2; }
  SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
  exec python3 -B "$SCRIPT_DIRECTORY/../../lean-inspector/scoped.py" verify \
    --repository "$REPOSITORY" --report "$REPORT" --targets "$TARGETS"
fi
SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# The registered policy is validated before any command exposes inputs.
SELECTION="$SCRIPT_DIRECTORY/lean-report-selection.py"
[[ -r "$SELECTION" ]] || { echo "lean-report-input: selection loader is absent: $SELECTION" >&2; exit 2; }
exec python3 -B "$SELECTION" "$COMMAND" --repository "$REPOSITORY"
