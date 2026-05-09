#!/usr/bin/env bash
# setup.sh [--reinstall]
#
# Idempotently registers the local CsFlowTemplate.Pack with `dotnet new`.
# After this, `dotnet new csflow --name <Name>` scaffolds a fresh
# self-contained C# Flow workflow project.
set -euo pipefail

CSHARP_ROOT="${CSHARP_ROOT:-$HOME/src/flow-v2/csharp}"
NUPKG="$CSHARP_ROOT/nuget/CsFlowTemplate.Pack.1.0.0.nupkg"

if [[ ! -f "$NUPKG" ]]; then
    echo "error: $NUPKG not found." >&2
    echo "Build it first with:" >&2
    echo "  dotnet pack \"$CSHARP_ROOT/template/CsFlowTemplate.Pack.csproj\" -c Release" >&2
    exit 1
fi

REINSTALL=0
for a in "$@"; do
    case "$a" in
        --reinstall) REINSTALL=1 ;;
    esac
done

if [[ "$REINSTALL" == "1" ]]; then
    dotnet new uninstall CsFlowTemplate.Pack 2>/dev/null || true
fi

# `dotnet new csflow --help` exits 0 only when the template is registered.
if dotnet new csflow --help >/dev/null 2>&1; then
    echo "csflow template is already registered."
    exit 0
fi

dotnet new install "$NUPKG"
echo "csflow template registered. Scaffold with: dotnet new csflow --name <Name>"
