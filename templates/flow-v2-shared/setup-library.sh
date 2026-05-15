#!/usr/bin/env bash
# setup-library.sh [--force]
#
# Builds both the JSON connector library (consumed by flow-run, v1-to-v2,
# v2-to-v1) and the markdown library (consumed by the coding agent in the
# flow-v2-library template) into the shared cache.
#
# Both libraries land under FLOW_V2_LIBRARY_CACHE_DIR (default
# $HOME/.cache/coder-eval/flow-v2). Already-built libraries are left alone
# unless --force is passed.
#
# Building the JSON library calls `uip flow registry get` ~1200 times, so
# the script is meant to run once before any flow-v2 evaluation task.
# Requirements: `uip` CLI authenticated to a tenant, `python3`.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/flow-v2-env.sh"

FORCE=0
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    -h|--help)
      sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 not on PATH" >&2; exit 1
fi
if ! command -v uip >/dev/null 2>&1; then
  echo "error: uip CLI not on PATH (needed to call flow registry / is resources)" >&2; exit 1
fi

mkdir -p "$FLOW_V2_LIBRARY_CACHE_DIR"

json_ready=0
md_ready=0
[[ -f "$FLOW_V2_LIBRARY_JSON/index.json" ]] && json_ready=1
[[ -f "$FLOW_V2_LIBRARY_MD/index.json" ]] && md_ready=1

if [[ "$FORCE" == "0" && "$json_ready" == "1" && "$md_ready" == "1" ]]; then
  echo "library cache already present:"
  echo "  JSON: $FLOW_V2_LIBRARY_JSON"
  echo "  MD:   $FLOW_V2_LIBRARY_MD"
  echo "pass --force to rebuild."
  exit 0
fi

# Step 1 — JSON library via the canonical generator.
if [[ "$FORCE" == "1" || "$json_ready" == "0" ]]; then
  echo "==> building JSON library at $FLOW_V2_LIBRARY_JSON"
  python3 "$HERE/generate_connectors.py" \
    --output-dir "$FLOW_V2_LIBRARY_JSON" \
    --cache-dir "$FLOW_V2_LIBRARY_CACHE_DIR/.registry-cache" \
    --is-cache-dir "$FLOW_V2_LIBRARY_CACHE_DIR/.is-cache" \
    --connectors-cache-dir "$FLOW_V2_LIBRARY_CACHE_DIR/.is-connectors-cache" \
    --keep-temp
else
  echo "==> JSON library already present at $FLOW_V2_LIBRARY_JSON (skip)"
fi

# Step 2 — markdown library derived from the JSON library.
if [[ "$FORCE" == "1" || "$md_ready" == "0" ]]; then
  echo "==> building markdown library at $FLOW_V2_LIBRARY_MD"
  python3 "$HERE/convert_library_to_md.py" \
    --source "$FLOW_V2_LIBRARY_JSON" \
    --output "$FLOW_V2_LIBRARY_MD"
else
  echo "==> markdown library already present at $FLOW_V2_LIBRARY_MD (skip)"
fi

echo ""
echo "done. cache root: $FLOW_V2_LIBRARY_CACHE_DIR"
