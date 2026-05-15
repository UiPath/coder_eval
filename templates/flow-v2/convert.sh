#!/usr/bin/env bash
# convert.sh <BaseName>
#
# Converts a v2 project (<BaseName>.fil + bindings.json) into the v1 form
# (<BaseName>.flow) via the v2-to-v1 CLI.
set -euo pipefail

NAME="${1:?usage: convert.sh <BaseName>}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/flow-v2-env.sh"
"$HERE/check-tools.sh"
"$HERE/check-library.sh" --json

node "$FLOW_V2_V2_TO_V1" "${NAME}.fil" \
  --library "$FLOW_V2_LIBRARY_JSON" \
  --out "${NAME}.flow"
