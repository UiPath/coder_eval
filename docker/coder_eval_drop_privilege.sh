#!/usr/bin/env bash
# Drop-privilege shim (SSOT for the docker user/permission isolation barrier).
#
# Runs its argv as the unprivileged `agent` uid baked into the image. The
# container entrypoint stays root (grading needs it); only the agent-under-test's
# CLI subprocess is routed through this shim so it (and every tool it spawns)
# executes as `agent:agent` and gets EACCES on the root-0700 grading material.
#
# Reused by the codex (launch_args_override) and antigravity (PATH-shadow) spawn
# wiring. The `agent` user is defined in docker/Dockerfile (useradd -u 2000);
# the path here is mirrored by CONTAINER_DROP_SHIM in
# src/coder_eval/models/container_paths.py.
set -euo pipefail
exec setpriv --reuid=agent --regid=agent --clear-groups -- "$@"
