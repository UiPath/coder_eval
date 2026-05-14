#!/usr/bin/env bash
set -euo pipefail

FILE="${1:?usage: parse-fil.sh <file.fil>}"
if [[ -z "${FLOW_V2:-}" ]]; then
  if [[ -d "$HOME/root/flow-v2" ]]; then
    FLOW_V2="$HOME/root/flow-v2"
  else
    FLOW_V2="$HOME/src/flow-v2"
  fi
fi
NODE_BIN="${NODE_BIN:-$(command -v node)}"

if [[ ! -d "$FLOW_V2/fil/dist" ]]; then
  echo "error: fil dist not built; run \`npm run build\` in $FLOW_V2/fil" >&2
  exit 2
fi

"$NODE_BIN" "$FLOW_V2/fil/dist/index.js" "$FILE" -o /tmp/.fil-parse-check.wat >/dev/null
