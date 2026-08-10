#!/usr/bin/env bash
# Execute the evaluated agent under its dedicated identity. The harness invokes
# this as root; every descendant inherits the UID/GID, empty capability sets,
# and no-new-privileges bit.
set -euo pipefail

if [[ $# -eq 0 ]]; then
    echo "coder_eval_drop_privilege: missing command" >&2
    exit 64
fi

exec setpriv \
    --reuid=agent \
    --regid=agent \
    --clear-groups \
    --inh-caps=-all \
    --ambient-caps=-all \
    --bounding-set=-all \
    --no-new-privs \
    -- "$@"
