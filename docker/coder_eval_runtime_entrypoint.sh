#!/usr/bin/env bash
# Entrypoint for INJECTED coder-eval runtime images (runtime-mode: inject).
#
# Unlike the framework image (where coder-eval is installed system-wide), an
# injected image keeps the task's own base and only has the self-contained kit
# under /opt/coder-eval. So this entrypoint prepends the kit's Python venv and
# Node bin dirs to PATH before launching the orchestrator — making `coder-eval`,
# `claude`, and `node` resolve from the kit regardless of what the task's base
# image provides (or doesn't).
#
# Same filename/location as the framework entrypoint, so the host's fixed
# `docker run --entrypoint /usr/local/bin/coder_eval_entrypoint.sh` works for
# both. Args are forwarded to `coder-eval _run-task-internal` (the host appends
# --output/--task-dir).
set -euo pipefail

export PATH="/opt/coder-eval/venv/bin:/opt/coder-eval/node/bin:${PATH}"

exec coder-eval _run-task-internal "$@"
