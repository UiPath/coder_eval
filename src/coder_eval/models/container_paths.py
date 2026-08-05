"""Canonical container-side paths (single source of truth).

These absolute paths are framework-owned inside a ``driver: docker`` container.
They are defined here -- a dependency-free leaf module -- so both the
import-light ``models`` layer (``SandboxConfig`` validation) and the heavier
``isolation.docker_runner`` can share one definition instead of each carrying a
copy. ``docker_runner`` re-exports the ``CONTAINER_*`` names, so existing
importers (and tests) that read them from ``docker_runner`` are unaffected.

Also mirrored as a comment in ``docker/coder_eval_entrypoint.sh``.
"""

from __future__ import annotations


CONTAINER_WORK_DIR = "/work"
CONTAINER_INPUT_DIR = "/work/input"
CONTAINER_OUTPUT_DIR = "/work/output"
CONTAINER_TASK_DIR = "/work/task_dir"
# Agent-readable, world-readable skill-DOCS copy mount (docs/commands/skills only;
# no grader trees). The agent's plugin discovery reads from here, not the raw
# skills-repo mount (which is locked root-0700 under the user/permission barrier).
CONTAINER_SKILL_DOCS_DIR = "/work/skills"

# Unprivileged agent uid/gid/username baked into docker/Dockerfile (via ARG) and
# used at runtime to drop the agent's CLI subprocess out of root under the
# user/permission isolation barrier. Single source of truth: the Dockerfile
# `useradd -u/-g` and the coder_eval_entrypoint.sh comment mirror these literals,
# and a drift-guard test asserts the Dockerfile uid matches AGENT_UID.
AGENT_UID = 2000
AGENT_GID = 2000
AGENT_USERNAME = "agent"
# Agent-owned HOME baked into docker/Dockerfile (`useradd -d /home/agent -m`,
# 0755 owned by the agent uid). The in-container spawn seam points the dropped
# CLI's ``HOME`` here so ``~/.claude`` / other dotfile writes land in an
# agent-writable dir instead of root's 0700 ``/root`` (which would EACCES). A
# drift-guard test asserts the Dockerfile home dir matches this literal.
AGENT_HOME = "/home/agent"

# Drop-privilege shim baked into the image. `exec setpriv --reuid=agent ... -- "$@"`
# runs its argv as the agent uid. Mirrored in docker/Dockerfile (COPY dest) and
# reused by the codex + antigravity spawn-seam wiring.
CONTAINER_DROP_SHIM = "/usr/local/bin/coder_eval_drop_privilege.sh"

# Paths a task's WORKDIR must never collide with: the container root and every
# framework-owned mount under /work. Consumed by SandboxConfig's working_dir
# validator (models/sandbox.py) and re-asserted host-side in docker_runner.
RESERVED_CONTAINER_DIRS = frozenset(
    {
        "/",
        CONTAINER_WORK_DIR,
        CONTAINER_INPUT_DIR,
        CONTAINER_OUTPUT_DIR,
        CONTAINER_TASK_DIR,
        CONTAINER_SKILL_DOCS_DIR,
    }
)
