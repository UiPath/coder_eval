#!/usr/bin/env bash
# convert-to-v2.sh <Name>
#
# Runs cs2fil against ./<Name>/ to produce the Flow v2 three-file split:
#   <Name>/<Name>.fil
#   <Name>/<Name>.manifest.flow
#   <Name>/<Name>.bindings.flow
#
# Then copies the three files into the project's parent dir (one level up
# from the .csproj) so the convert-to-v1 script and validators find them
# alongside any existing v1 artifacts. Symlinks (not copies) so edits stay
# in sync.
set -euo pipefail

NAME="${1:?usage: convert-to-v2.sh <Name>}"
CSHARP_ROOT="${CSHARP_ROOT:-$HOME/src/flow-v2/csharp}"
NODE_BIN="${NODE_BIN:-$HOME/.asdf/installs/nodejs/22.22.1/bin/node}"

if [[ ! -d "./$NAME" ]]; then
    echo "error: ./${NAME}/ not found. Did you run \`dotnet new csflow --name $NAME\`?" >&2
    exit 1
fi

# Build the npm CLI if its dist isn't there yet.
if [[ ! -d "$CSHARP_ROOT/cli/dist" ]]; then
    (cd "$CSHARP_ROOT/cli" && npm install >/dev/null 2>&1 && npm run build)
fi

"$NODE_BIN" "$CSHARP_ROOT/cli/dist/index.js" cs-to-fil \
    --project "./$NAME" \
    --csharp-root "$CSHARP_ROOT" \
    --output-dir "./$NAME" \
    --basename "$NAME"

echo "Wrote ./${NAME}/${NAME}.fil"
echo "Wrote ./${NAME}/${NAME}.manifest.flow"
echo "Wrote ./${NAME}/${NAME}.bindings.flow"
