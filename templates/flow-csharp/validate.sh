#!/usr/bin/env bash
# validate.sh <Name>
#
# Runs `uip maestro flow validate` and `uip maestro flow format` against
# ./<Name>.flow. Both must succeed before the flow is shippable.
set -euo pipefail

NAME="${1:?usage: validate.sh <Name>}"

if [[ ! -f "./$NAME.flow" ]]; then
    echo "error: ./${NAME}.flow not found. Run convert-to-v1.sh first." >&2
    exit 1
fi

uip maestro flow validate "./$NAME.flow"
uip maestro flow format "./$NAME.flow"
