#!/usr/bin/env bash
set -euo pipefail

NAME="${1:?usage: convert.sh <BaseName>}"
if [[ -z "${FLOW_V2:-}" ]]; then
  if [[ -d "$HOME/root/flow-v2" ]]; then
    FLOW_V2="$HOME/root/flow-v2"
  else
    FLOW_V2="$HOME/src/flow-v2"
  fi
fi
NODE_BIN="${NODE_BIN:-$(command -v node)}"

if [[ ! -d "$FLOW_V2/v2-to-v1/dist" ]]; then
  echo "error: v2-to-v1 dist not built; run \`npm run build\` in $FLOW_V2/v2-to-v1" >&2
  exit 2
fi

"$NODE_BIN" "$FLOW_V2/v2-to-v1/dist/cli.js" "${NAME}.fil" --out "${NAME}.flow"
