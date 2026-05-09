#!/usr/bin/env bash
# convert.sh <BaseName>
#
# Converts a v2 project (3 files: <BaseName>.fil.ts + <BaseName>.manifest.flow +
# bindings.json) into a v1 .flow file via the v2-to-v1 CLI.
#
# Output: <BaseName>.flow in the same directory.
set -euo pipefail

NAME="${1:?usage: convert.sh <BaseName>}"
FLOW_V2="${FLOW_V2:-$HOME/src/flow-v2}"
NODE_BIN="${NODE_BIN:-$HOME/.asdf/installs/nodejs/22.22.1/bin/node}"

if [[ ! -d "$FLOW_V2/v2-to-v1/dist" ]]; then
  echo "error: v2-to-v1 dist not built; run \`npm run build\` in $FLOW_V2/v2-to-v1" >&2
  exit 2
fi

"$NODE_BIN" "$FLOW_V2/v2-to-v1/dist/cli.js" "${NAME}.manifest.flow" --out "${NAME}.flow"
