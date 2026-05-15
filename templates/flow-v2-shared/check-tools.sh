#!/usr/bin/env bash
# check-tools.sh
#
# Guard invoked by template parse-fil.sh / verify.sh / convert.sh. Exits 0
# when the vendored toolchain is in place, otherwise exits non-zero with a
# pointer at sync-tools.sh (or `npm ci`, when only node_modules is missing).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/flow-v2-env.sh"

dist_missing=()
for entry in "$FLOW_V2_FIL" "$FLOW_V2_FLOW_RUN" "$FLOW_V2_V2_TO_V1"; do
  [[ -f "$entry" ]] || dist_missing+=("$entry")
done
deps_missing=0
[[ -d "$FLOW_V2_TOOLS_DIR/node_modules/wabt" ]] || deps_missing=1

if (( ${#dist_missing[@]} )); then
  {
    echo "error: flow-v2 vendored toolchain incomplete:"
    for m in "${dist_missing[@]}"; do echo "  - $m"; done
    echo ""
    echo "Re-vendor from your flow-v2 working copy:"
    echo "  bash <flow-v2-shared>/sync-tools.sh"
    echo "  (or set FLOW_V2 / pass --source to point at the repo)"
  } >&2
  exit 1
fi

if (( deps_missing )); then
  {
    echo "error: tools/node_modules is missing — wabt (flow-run runtime dep) cannot be resolved."
    echo ""
    echo "Reproducible install from the committed package-lock.json:"
    echo "  ( cd $FLOW_V2_TOOLS_DIR && npm ci --omit=dev )"
  } >&2
  exit 1
fi
