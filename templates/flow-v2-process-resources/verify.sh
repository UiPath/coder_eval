#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${FLOW_V2:-}" ]]; then
  if [[ -d "$HOME/root/flow-v2" ]]; then
    FLOW_V2="$HOME/root/flow-v2"
  else
    FLOW_V2="$HOME/src/flow-v2"
  fi
fi
NODE_BIN="${NODE_BIN:-$(command -v node)}"

if [[ ! -d "$FLOW_V2/flow-run/dist" ]]; then
  echo "error: flow-run dist not built; run \`npm run build\` in $FLOW_V2/flow-run" >&2
  exit 2
fi

ARGS=()
LIVE=0
for a in "$@"; do
  case "$a" in
    --live) LIVE=1 ;;
    --dry-run) LIVE=0 ;;
    *) ARGS+=("$a") ;;
  esac
done

if [[ "$LIVE" == "0" ]]; then
  ARGS+=("--dry-run")
fi

"$NODE_BIN" "$FLOW_V2/flow-run/dist/cli.js" \
  . \
  --library "$FLOW_V2/integrations/library" \
  ${ARGS[@]+"${ARGS[@]}"}
