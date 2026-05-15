#!/usr/bin/env bash
# check-library.sh [--json | --md | --both]
#
# Guard script invoked by template verify.sh / convert.sh. Exits 0 if the
# requested library caches exist; otherwise exits non-zero with a message
# pointing the operator at the setup task.
#
# Default check is --both. The flow-v2-library template needs --both; the
# plain flow-v2 and flow-v2-process-resources templates only need --json.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/flow-v2-env.sh"

WANT="both"
case "${1:-}" in
  --json) WANT="json" ;;
  --md) WANT="md" ;;
  --both|"") WANT="both" ;;
  *) echo "usage: check-library.sh [--json|--md|--both]" >&2; exit 2 ;;
esac

missing=()
if [[ "$WANT" == "json" || "$WANT" == "both" ]]; then
  [[ -f "$FLOW_V2_LIBRARY_JSON/index.json" ]] || missing+=("$FLOW_V2_LIBRARY_JSON")
fi
if [[ "$WANT" == "md" || "$WANT" == "both" ]]; then
  [[ -f "$FLOW_V2_LIBRARY_MD/index.json" ]] || missing+=("$FLOW_V2_LIBRARY_MD")
fi

if (( ${#missing[@]} )); then
  {
    echo "error: flow-v2 library cache missing:"
    for m in "${missing[@]}"; do echo "  - $m"; done
    echo ""
    echo "Run the setup task once before running flow-v2 tasks:"
    echo "  coder-eval run tasks/uipath_flow/setup/build_library.yaml"
    echo "or invoke the script directly:"
    echo "  bash <flow-v2-shared>/setup-library.sh"
    echo ""
    echo "Use --force to rebuild an existing cache."
  } >&2
  exit 1
fi
