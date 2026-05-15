#!/usr/bin/env bash
# parse-fil.sh <FilFile>
#
# Compiles a FIL source file to WAT (output discarded) just to verify the
# FIL parses cleanly. Exit code 0 on a clean parse, non-zero with a stderr
# error message otherwise.
set -euo pipefail

FILE="${1:?usage: parse-fil.sh <file.fil>}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/flow-v2-env.sh"
"$HERE/check-tools.sh"

node "$FLOW_V2_FIL" "$FILE" -o /tmp/.fil-parse-check.wat >/dev/null
