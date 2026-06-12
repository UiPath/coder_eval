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
set -euo pipefail

exec coder-eval _run-task-internal "$@"
