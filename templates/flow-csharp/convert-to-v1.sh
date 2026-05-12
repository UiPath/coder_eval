#!/usr/bin/env bash
# convert-to-v1.sh <Name>
#
# Converts a C# Flow project at ./<Name>/ into the Flow v1 artifact set:
#   ./<Name>.flow      — v1 flow JSON
#   ./bindings.json    — bindings (resolved from appsettings.json + user-secrets + env)
#
# The C# project is the developer's source of truth — it's already been
# tested via `dotnet run -- --test`, so this script does NOT execute the FIL.
# It only produces the v1 artifacts:
#
#   1. cs2fil cs-to-fil → <project>/.cs2v1/<Name>.fil + .manifest.flow
#      (FIL compile errors here abort the script).
#   2. dotnet run -- --emit-bindings <project>/.cs2v1/bindings.json
#      (resolves bindings via the C# project's own configuration builder —
#      the canonical source of binding values, including user-secrets and
#      env overrides).
#   3. v2-to-v1 → ./<Name>.flow + ./bindings.json (verbatim copy of step 2).
set -euo pipefail

NAME="${1:?usage: convert-to-v1.sh <Name>}"
CSHARP_ROOT="${CSHARP_ROOT:-$HOME/src/flow-v2/csharp}"
FLOW_V2="${FLOW_V2:-$HOME/src/flow-v2}"
NODE_BIN="${NODE_BIN:-$HOME/.asdf/installs/nodejs/22.22.1/bin/node}"

PROJECT_DIR="./$NAME"
WORK_DIR="$PROJECT_DIR/.cs2v1"

if [[ ! -d "$PROJECT_DIR" ]]; then
    echo "error: $PROJECT_DIR not found. Did you run \`dotnet new csflow --name $NAME\`?" >&2
    exit 1
fi

mkdir -p "$WORK_DIR"

# Step 1: cs2fil → <Name>.fil + <Name>.manifest.flow (analyzers + FIL emit).
# cs2fil also emits a stub bindings.json derived from the analyzed C#; we'll
# overwrite that in step 2 with the project's resolved bindings.
if [[ ! -d "$CSHARP_ROOT/cli/dist" ]]; then
    (cd "$CSHARP_ROOT/cli" && npm install >/dev/null 2>&1 && npm run build)
fi

echo "==> Compiling C# → FIL..."
"$NODE_BIN" "$CSHARP_ROOT/cli/dist/index.js" cs-to-fil \
    --project "$PROJECT_DIR" \
    --csharp-root "$CSHARP_ROOT" \
    --output-dir "$WORK_DIR" \
    --basename "$NAME"

# Step 2: resolve bindings via the C# project's configuration builder. This
# applies user-secrets and env-var overrides on top of appsettings.json.
echo "==> Resolving bindings from $PROJECT_DIR (appsettings + user-secrets + env)..."
dotnet run --project "$PROJECT_DIR" -- --emit-bindings "$WORK_DIR/bindings.json"

# Step 3: v2-to-v1 → <Name>.flow + bindings.json at the project root.
if [[ ! -d "$FLOW_V2/v2-to-v1/dist" ]]; then
    echo "error: v2-to-v1 dist not built; run \`npm run build\` in $FLOW_V2/v2-to-v1" >&2
    exit 2
fi

echo "==> Converting FIL → v1 .flow..."
"$NODE_BIN" "$FLOW_V2/v2-to-v1/dist/cli.js" \
    "$WORK_DIR/$NAME.manifest.flow" \
    --fil "$WORK_DIR/$NAME.fil" \
    --bindings "$WORK_DIR/bindings.json" \
    --out "./$NAME.flow"

echo
echo "Wrote ./$NAME.flow"
echo "Wrote ./bindings.json"
