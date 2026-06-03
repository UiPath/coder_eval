#!/usr/bin/env bash
# convert.sh <BaseName>
#
# Convert + deploy-gate in one step:
#   1. Converts the v2 project (<BaseName>.fil + bindings.json) into the
#      v1 form (<BaseName>.flow) via the v2-to-v1 CLI.
#   2. Runs `uip maestro flow validate` on the v1 .flow.
#   3. Runs `uip maestro flow format` on the v1 .flow.
#
# Bundling (2) and (3) here avoids the Claude Code Bash-tool auto-background
# that fires on chained `validate && format` invocations (each `uip` call
# pays a multi-second plugin-load startup; chained, they routinely exceed
# the 2-minute implicit timeout, get backgrounded, and force the agent to
# wake up later to acknowledge — costing ~3 minutes of wall time per task).
# Running them in this script keeps the whole gate behind a single Bash
# tool call with explicit progress output.
set -euo pipefail

NAME="${1:?usage: convert.sh <BaseName>}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/flow-v2-env.sh"
"$HERE/check-tools.sh"
"$HERE/check-library.sh" --json

# Use the same uip the SKILL.md prompts pin to (works around the
# @uipath/cli@0.1.21 env_packages shadowing on PATH).
UIP="${UIP:-$HOME/.bun/bin/uip}"
if [[ ! -x "$UIP" ]]; then
  UIP="$(command -v uip)" || { echo "convert.sh: no usable 'uip' on PATH or at \$HOME/.bun/bin/uip" >&2; exit 1; }
fi

echo "==> v2 → v1 conversion"
node "$FLOW_V2_V2_TO_V1" "${NAME}.fil" \
  --library "$FLOW_V2_LIBRARY_JSON" \
  --out "${NAME}.flow"

echo "==> uip maestro flow validate ${NAME}.flow"
"$UIP" maestro flow validate "${NAME}.flow"

echo "==> uip maestro flow format ${NAME}.flow"
"$UIP" maestro flow format "${NAME}.flow"

echo "convert.sh: ${NAME}.flow is valid and formatted."
