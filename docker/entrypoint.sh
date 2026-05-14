#!/usr/bin/env bash
# Entrypoint for coder-eval-agent containers.
#
# Forwards any args through to `coder-eval _run-task-internal`. The host
# always invokes us with no args (defaults point at /work/input + /work/output),
# but accepting `$@` keeps the door open for image-level overrides during
# debugging: `docker run --rm coder-eval-agent --input /tmp/foo`.
set -euo pipefail

exec coder-eval _run-task-internal "$@"
