#!/usr/bin/env bash
set -euo pipefail

if [[ $# -eq 0 ]]; then
    echo "coder_eval_mockd: missing command" >&2
    exit 64
fi

exec setpriv \
    --reuid=mockd \
    --regid=mockd \
    --groups=uip-rpc \
    --inh-caps=-all \
    --ambient-caps=-all \
    --bounding-set=-all \
    --no-new-privs \
    -- "$@"
