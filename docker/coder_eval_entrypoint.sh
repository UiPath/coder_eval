#!/usr/bin/env bash
# Entrypoint for coder-eval-agent containers.
#
# The image deliberately bakes NO `ENTRYPOINT`: the host pins this script at run
# time via `docker run --entrypoint /usr/local/bin/coder_eval_entrypoint.sh`, so
# the in-container orchestrator launches regardless of what a task image's own
# Dockerfile declares. The coder-eval-specific filename avoids colliding with a
# base image's own `/usr/local/bin/entrypoint.sh`.
#
# Forwards any args through to `coder-eval _run-task-internal` (the host appends
# `--output`/`--task-dir`). For manual debugging, pass the same flag:
#   docker run --rm --entrypoint /usr/local/bin/coder_eval_entrypoint.sh <image> --input /tmp/foo
#
# User/permission isolation barrier: this entrypoint (and _run-task-internal) runs
# as ROOT — grading needs it. All lock/chown of grading material and the per-agent
# uid drop live in Python (cli/run_task_internal_command.py), NOT here. SSOT for
# the barrier constants (mirrored from src/coder_eval/models/container_paths.py):
#   AGENT_UID = AGENT_GID = 2000, AGENT_USERNAME = "agent"
#   CONTAINER_DROP_SHIM = /usr/local/bin/coder_eval_drop_privilege.sh
set -euo pipefail

exec coder-eval _run-task-internal "$@"
