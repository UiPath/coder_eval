#!/usr/bin/env bash
# flow-v2-env.sh — sourced by other scripts to set library and tool paths.
#
# Library cache (built by setup-library.sh):
#   FLOW_V2_LIBRARY_CACHE_DIR  Root cache dir.
#                              Default: $HOME/.cache/coder-eval/flow-v2
#   FLOW_V2_LIBRARY_JSON       Canonical JSON library (consumed by flow-run,
#                              v1-to-v2, v2-to-v1 via --library).
#   FLOW_V2_LIBRARY_MD         Markdown library (consumed by the coding agent
#                              for connector discovery in flow-v2-library).
#
# Vendored toolchain (populated by sync-tools.sh, lives in tools/ next
# to this script):
#   FLOW_V2_TOOLS_DIR          tools/ root.
#   FLOW_V2_FIL                node-callable entry for the fil compiler.
#   FLOW_V2_FLOW_RUN           node-callable entry for flow-run.
#   FLOW_V2_V2_TO_V1           node-callable entry for v2tov1.
#
# Override the library root with FLOW_V2_LIBRARY_CACHE_DIR; the two sub-paths
# follow. Tool paths anchor on `$(dirname this-file)/tools/` and are not
# meant to be overridden — re-run sync-tools.sh to refresh them.

: "${FLOW_V2_LIBRARY_CACHE_DIR:=$HOME/.cache/coder-eval/flow-v2}"
: "${FLOW_V2_LIBRARY_JSON:=$FLOW_V2_LIBRARY_CACHE_DIR/library-json}"
: "${FLOW_V2_LIBRARY_MD:=$FLOW_V2_LIBRARY_CACHE_DIR/library-md}"

_flow_v2_env_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLOW_V2_TOOLS_DIR="$_flow_v2_env_dir/tools"
FLOW_V2_FIL="$FLOW_V2_TOOLS_DIR/fil/dist/index.js"
FLOW_V2_FLOW_RUN="$FLOW_V2_TOOLS_DIR/flow-run/dist/cli.js"
FLOW_V2_V2_TO_V1="$FLOW_V2_TOOLS_DIR/v2-to-v1/dist/cli.js"
unset _flow_v2_env_dir

export FLOW_V2_LIBRARY_CACHE_DIR FLOW_V2_LIBRARY_JSON FLOW_V2_LIBRARY_MD
export FLOW_V2_TOOLS_DIR FLOW_V2_FIL FLOW_V2_FLOW_RUN FLOW_V2_V2_TO_V1
