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
#
# flow-run surfaces:
#   FIL compile errors as file:line:col with the offending source line
#   stub UUIDs in bindings.json (00000000-...) with a fix message
#   bindings that don't match a real connection in `uip is connections list`,
#     plus candidate IDs for that connector key
#   non-CRUD or unrecognized node types
set -euo pipefail

FLOW_V2="${FLOW_V2:-$HOME/src/flow-v2}"
NODE_BIN="${NODE_BIN:-$HOME/.asdf/installs/nodejs/22.22.1/bin/node}"

if [[ ! -d "$FLOW_V2/flow-run/dist" ]]; then
  echo "error: flow-run dist not built; run \`npm run build\` in $FLOW_V2/flow-run" >&2
  exit 2
fi

ARGS=()
LIVE=0
for a in "$@"; do
  case "$a" in
    --live)    LIVE=1 ;;
    --dry-run) LIVE=0 ;;
    *)         ARGS+=("$a") ;;
  esac
done

if [[ "$LIVE" == "0" ]]; then
  ARGS+=("--dry-run")
fi

"$NODE_BIN" "$FLOW_V2/flow-run/dist/cli.js" \
  . \
  --library "$FLOW_V2/integrations/library" \
  ${ARGS[@]+"${ARGS[@]}"}
