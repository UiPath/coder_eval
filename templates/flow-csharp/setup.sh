#!/usr/bin/env bash
# setup.sh [--reinstall]
#
# Idempotently registers the local CsFlowTemplate.Pack with `dotnet new`
# and installs the csflowrun .NET global tool. After this:
#   - `dotnet new csflow --name <Name>` scaffolds a fresh self-contained
#     C# Flow workflow project.
#   - `csflowrun <Name>.dll` (called by ./verify.sh) compiles Workflow.cs
#     → FIL and runs the [WorkflowTest] cases through flow-run.
set -euo pipefail

CSHARP_ROOT="${CSHARP_ROOT:-$HOME/src/flow-v2/csharp}"
NUGET_DIR="$CSHARP_ROOT/nuget"
TEMPLATE_NUPKG="$NUGET_DIR/CsFlowTemplate.Pack.1.0.0.nupkg"

# csflowrun nupkg — versioned, so glob and pick the highest.
CSFLOWRUN_NUPKG="$(ls -1 "$NUGET_DIR"/UiPath.CsFlowRun.*.nupkg 2>/dev/null | sort -V | tail -1 || true)"

if [[ ! -f "$TEMPLATE_NUPKG" ]]; then
    echo "error: $TEMPLATE_NUPKG not found." >&2
    echo "Build it first with:" >&2
    echo "  dotnet pack \"$CSHARP_ROOT/template/CsFlowTemplate.Pack.csproj\" -c Release" >&2
    exit 1
fi

if [[ -z "$CSFLOWRUN_NUPKG" || ! -f "$CSFLOWRUN_NUPKG" ]]; then
    echo "error: no UiPath.CsFlowRun.*.nupkg under $NUGET_DIR." >&2
    echo "Build it first with:" >&2
    echo "  dotnet build \"$CSHARP_ROOT/src/CsFlowRun/CsFlowRun.csproj\" -c Release" >&2
    exit 1
fi

REINSTALL=0
for a in "$@"; do
    case "$a" in
        --reinstall) REINSTALL=1 ;;
    esac
done

# ─── csflow dotnet new template ───────────────────────────────────────────
if [[ "$REINSTALL" == "1" ]]; then
    dotnet new uninstall CsFlowTemplate.Pack 2>/dev/null || true
fi

# `dotnet new csflow --help` exits 0 only when the template is registered.
if dotnet new csflow --help >/dev/null 2>&1; then
    echo "csflow template is already registered."
else
    dotnet new install "$TEMPLATE_NUPKG"
    echo "csflow template registered. Scaffold with: dotnet new csflow --name <Name>"
fi

# ─── csflowrun global tool ────────────────────────────────────────────────
if [[ "$REINSTALL" == "1" ]]; then
    dotnet tool uninstall -g UiPath.CsFlowRun 2>/dev/null || true
fi

if command -v csflowrun >/dev/null 2>&1; then
    echo "csflowrun global tool is already installed ($(csflowrun --help 2>&1 | head -1))."
else
    dotnet tool install -g UiPath.CsFlowRun --add-source "$NUGET_DIR"
    echo "csflowrun global tool installed. Invoke via: csflowrun <project-dir | dll>"
fi
