#!/usr/bin/env bash
# verify.sh <Name>
#
# Verifies the C# Flow workflow project at ./<Name>/ via csflowrun in
# dry-run mode. csflowrun discovers [WorkflowTest] methods from the built
# assembly's embedded PDB sources and runs each scenario through the C#
# Flow runtime; fixture-pinned tests (those with `History = "fixtures/…"`)
# replay their history files; smoke tests run the workflow once.
#
# Dry-run never makes a real connector call, so empty / placeholder
# binding values in appsettings.json are fine — the runtime substitutes
# a placeholder UUID internally. Credential validation against a real
# tenant is the deploy gate (./convert-to-v1.sh + ./validate.sh).
set -euo pipefail

NAME="${1:?usage: verify.sh <Name>}"
CSHARP_ROOT="${CSHARP_ROOT:-$HOME/src/flow-v2/csharp}"
LIBRARY_DIR="${FLOW_V2_LIBRARY_JSON:-${LIBRARY_DIR:-$HOME/.cache/coder-eval/flow-v2/library-json}}"

if [[ ! -d "./$NAME" ]]; then
    echo "error: ./$NAME not found. Did you run \`dotnet new csflow --name $NAME\`?" >&2
    exit 1
fi

if [[ ! -f "$LIBRARY_DIR/index.json" ]]; then
    echo "error: connector library not found at $LIBRARY_DIR." >&2
    echo "       Set FLOW_V2_LIBRARY_JSON to a populated library dir, or run" >&2
    echo "       coder_eval/templates/flow-v2-shared/setup-library.sh once." >&2
    exit 2
fi

if ! command -v csflowrun >/dev/null 2>&1; then
    echo "error: csflowrun not on PATH. Re-run ./setup.sh." >&2
    exit 3
fi

echo "==> building $NAME..."
dotnet build "./$NAME/$NAME.csproj" --nologo -v minimal

ASSEMBLY=$(find "./$NAME/bin" -maxdepth 4 -name "$NAME.dll" 2>/dev/null | head -1)
if [[ -z "$ASSEMBLY" ]]; then
    echo "error: $NAME.dll not found under ./$NAME/bin — did the build succeed?" >&2
    exit 4
fi

echo "==> csflowrun $(basename "$ASSEMBLY") --dry-run..."
exec csflowrun "$ASSEMBLY" \
    --csharp-root "$CSHARP_ROOT" \
    --library "$LIBRARY_DIR" \
    --dry-run
