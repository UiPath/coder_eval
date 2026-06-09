#!/usr/bin/env bash
# convert-to-v1.sh <Name>
#
# Converts a C# Flow project at ./<Name>/ into the deployable artifact set:
#   ./<Name>.flow      — UiPath Flow JSON
#   ./bindings.json    — bindings (resolved from appsettings.json + user-secrets + env)
#
# The C# project is the developer's source of truth — already verified via
# ./verify.sh, so this script does NOT re-run the workflow. It only
# produces the deployable artifacts:
#
#   1. Build the workflow from C# (build errors here abort the script).
#      The intermediate output lands under <project>/.cs2v1/ and carries
#      the action/trigger metadata needed by step 3.
#   2. dotnet run -- --emit-bindings <project>/.cs2v1/bindings.json
#      (resolves bindings via the C# project's own configuration builder —
#      the canonical source of binding values, including user-secrets and
#      env overrides).
#   3. Generate ./<Name>.flow + ./bindings.json (verbatim copy of step 2).
set -euo pipefail

NAME="${1:?usage: convert-to-v1.sh <Name>}"
CSHARP_ROOT="${CSHARP_ROOT:-$HOME/src/flow-v2/csharp}"
FLOW_V2="${FLOW_V2:-$HOME/src/flow-v2}"
NODE_BIN="${NODE_BIN:-$HOME/.asdf/installs/nodejs/22.22.1/bin/node}"
# v2-to-v1 looks up connector defs by typeRef during conversion. Point at
# the shared library cache when present so it doesn't fall back to the
# (gitignored) ~/src/flow-v2/integrations/library/.
LIBRARY_DIR="${FLOW_V2_LIBRARY_JSON:-${LIBRARY_DIR:-$HOME/.cache/coder-eval/flow-v2/library-json}}"

PROJECT_DIR="./$NAME"
WORK_DIR="$PROJECT_DIR/.cs2v1"

if [[ ! -d "$PROJECT_DIR" ]]; then
    echo "error: $PROJECT_DIR not found. Did you run \`dotnet new csflow --name $NAME\`?" >&2
    exit 1
fi

mkdir -p "$WORK_DIR"

# Step 1: build the workflow from C# (analyzer + emit, including the
# action/trigger metadata). A stub bindings.json gets written too;
# step 2 overwrites it with the project's resolved bindings.
if [[ ! -d "$CSHARP_ROOT/cli/dist" ]]; then
    (cd "$CSHARP_ROOT/cli" && npm install >/dev/null 2>&1 && npm run build)
fi

echo "==> Building workflow from C#..."
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

echo "==> Generating .flow file..."
V2V1_ARGS=("$WORK_DIR/$NAME.fil" --bindings "$WORK_DIR/bindings.json" --out "./$NAME.flow")
if [[ -f "$LIBRARY_DIR/index.json" ]]; then
    V2V1_ARGS+=(--library "$LIBRARY_DIR")
fi
"$NODE_BIN" "$FLOW_V2/v2-to-v1/dist/cli.js" "${V2V1_ARGS[@]}"

echo
echo "Wrote ./$NAME.flow"
echo "Wrote ./bindings.json"
