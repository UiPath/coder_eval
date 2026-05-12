#!/usr/bin/env bash
# parse-fil.sh <FilFile>
#
# Compiles a FIL source file to WAT (output discarded) just to verify the
# FIL parses cleanly. Exit code 0 on a clean parse, non-zero with a stderr
# error message otherwise.
set -euo pipefail

FILE="${1:?usage: parse-fil.sh <file.fil>}"
FLOW_V2="${FLOW_V2:-$HOME/src/flow-v2}"
NODE_BIN="${NODE_BIN:-$HOME/.asdf/installs/nodejs/22.22.1/bin/node}"

if [[ ! -d "$FLOW_V2/fil/dist" ]]; then
  echo "error: fil dist not built; run \`npm run build\` in $FLOW_V2/fil" >&2
  exit 2
fi

"$NODE_BIN" "$FLOW_V2/fil/dist/index.js" "$FILE" -o /tmp/.fil-parse-check.wat >/dev/null
