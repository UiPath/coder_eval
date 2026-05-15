#!/usr/bin/env bash
# verify.sh [--live] [-- <extra flow-run args>]
#
# Runs flow-run against this v2 project for the inner authoring loop.
#
#   --dry-run (default): compile + binding check, no connector calls
#   --live:              actually executes nodes against your tenant
#
# Outputs (under .flow-run/):
#   history.yaml      — replay log; consumed by flow-run --resume
#   decisions.json    — one record per dispatched node, with parsed
#                       input + output. **Read this to debug.**
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/flow-v2-env.sh"
"$HERE/check-tools.sh"
"$HERE/check-library.sh" --json

ARGS=()
LIVE=0
for a in "$@"; do
  case "$a" in
    --live) LIVE=1 ;;
    --dry-run) LIVE=0 ;;
    *) ARGS+=("$a") ;;
  esac
done

if [[ "$LIVE" == "0" ]]; then
  ARGS+=("--dry-run")
fi

node "$FLOW_V2_FLOW_RUN" \
  . \
  --library "$FLOW_V2_LIBRARY_JSON" \
  ${ARGS[@]+"${ARGS[@]}"}
