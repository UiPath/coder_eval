#!/usr/bin/env bash
# verify.sh [--live] [-- <extra flow-run args>]
#
# Runs flow-run against this v2 project for the inner authoring loop.
#
#   --dry-run (default): compile + binding check, no live connector/HTTP calls
#   --live:              executes supported live nodes against your tenant
#                        (fails explicitly for published/inline Agent nodes)
#
# Outputs (under .flow-run/):
#   history.yaml      — replay log; consumed by flow-run --resume
#   decisions.json    — one record per dispatched node, with parsed
#                       input + output. **Read this to debug.**
#
# flow-run surfaces:
#   FIL compile errors as file:line:col with the offending source line
#   stub UUIDs in bindings.json (00000000-...) with a fix message
#   bindings that don't match a real connection in `uip is connections list`,
#     plus candidate IDs for that connector key
#   published Agent resource bindings with missing/wrong process metadata
#   inline Agent rawInputs.source/prompt/model/variable placeholder issues
#   Summarize rawInputs.attachment/prompt/returnCitations issues
#   non-CRUD or unrecognized node types
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
