#!/usr/bin/env bash
# convert-to-v1.sh <Name>
#
# Reads ./<Name>/<Name>.fil + .manifest.flow + .bindings.flow and produces
# ./<Name>.flow (v1 JSON) for the deploy gate.
set -euo pipefail

NAME="${1:?usage: convert-to-v1.sh <Name>}"
FLOW_V2="${FLOW_V2:-$HOME/src/flow-v2}"
NODE_BIN="${NODE_BIN:-$HOME/.asdf/installs/nodejs/22.22.1/bin/node}"

PROJECT_DIR="./$NAME"

for f in "$NAME.fil" "$NAME.manifest.flow" "$NAME.bindings.flow"; do
    if [[ ! -f "$PROJECT_DIR/$f" ]]; then
        echo "error: $PROJECT_DIR/$f not found. Run convert-to-v2.sh first." >&2
        exit 1
    fi
done

if [[ ! -d "$FLOW_V2/v2-to-v1/dist" ]]; then
    echo "error: v2-to-v1 dist not built; run \`npm run build\` in $FLOW_V2/v2-to-v1" >&2
    exit 2
fi

# v2-to-v1 takes a manifest path and (separately) a fil + bindings; output is the v1 .flow.
"$NODE_BIN" "$FLOW_V2/v2-to-v1/dist/cli.js" \
    "$PROJECT_DIR/$NAME.manifest.flow" \
    --fil "$PROJECT_DIR/$NAME.fil" \
    --bindings "$PROJECT_DIR/$NAME.bindings.flow" \
    --out "./$NAME.flow"

echo "Wrote ./${NAME}.flow"
