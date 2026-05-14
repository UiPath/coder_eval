#!/usr/bin/env bash
# convert.sh <BaseName>
#
# Converts a v2 project (2 files: <BaseName>.fil + bindings.json) into the v1
# artifact set (<BaseName>.flow + bindings.json) via the v2-to-v1 CLI.
#
# Output: <BaseName>.flow + bindings.json in the same directory.
set -euo pipefail

NAME="${1:?usage: convert.sh <BaseName>}"
FLOW_V2="${FLOW_V2:-$HOME/src/flow-v2}"
NODE_BIN="${NODE_BIN:-$HOME/.asdf/installs/nodejs/22.22.1/bin/node}"

if [[ ! -d "$FLOW_V2/v2-to-v1/dist" ]]; then
  echo "error: v2-to-v1 dist not built; run \`npm run build\` in $FLOW_V2/v2-to-v1" >&2
  exit 2
fi

"$NODE_BIN" "$FLOW_V2/v2-to-v1/dist/cli.js" "${NAME}.fil" --out "${NAME}.flow"
