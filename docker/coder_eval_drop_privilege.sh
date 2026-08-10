#!/usr/bin/env bash
# Execute the evaluated agent under its dedicated identity. The harness invokes
# this as root; every descendant inherits the UID/GID, empty capability sets,
# and no-new-privileges bit.
set -euo pipefail

if [[ $# -eq 0 ]]; then
    echo "coder_eval_drop_privilege: missing command" >&2
    exit 64
fi

GROUP_ARGS=(--clear-groups)
if [[ "${CODER_EVAL_AGENT_ALLOW_RPC:-}" == "1" ]]; then
    GROUP_ARGS=(--groups=uip-rpc)
fi

exec setpriv \
    --reuid=agent \
    --regid=agent \
    "${GROUP_ARGS[@]}" \
    --inh-caps=-all \
    --ambient-caps=-all \
    --bounding-set=-all \
    --no-new-privs \
    -- "$@"
