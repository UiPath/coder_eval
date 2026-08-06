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
CONTAINER_AGENT_WORK_DIR = "/work/agent"

# The evaluated agent shares the container with the trusted harness, but cannot
# traverse this root-owned directory. Hidden task, grader, reference, fixture,
# and result material is mounted below it rather than at agent-readable /work
# paths.
CONTAINER_GRADER_DIR = "/opt/coder-eval/grader"
CONTAINER_INPUT_DIR = f"{CONTAINER_GRADER_DIR}/input"
CONTAINER_OUTPUT_DIR = f"{CONTAINER_GRADER_DIR}/output"
CONTAINER_TASK_DIR = f"{CONTAINER_GRADER_DIR}/task_dir"
CONTAINER_PRIVATE_PLUGIN_DIR = f"{CONTAINER_GRADER_DIR}/plugins"

# Manifest-verified skill projections are the only plugin trees exposed to the
# evaluated agent.
CONTAINER_AGENT_SKILLS_DIR = "/opt/coder-eval/agent-skills"

AGENT_UID = 2000
AGENT_GID = 2000
AGENT_USERNAME = "agent"
AGENT_HOME = "/home/agent"
MOCKD_UID = 2100
MOCKD_GID = 2100
MOCKD_USERNAME = "mockd"
MOCK_RPC_GID = 2200
MOCK_RPC_GROUP = "uip-rpc"

CONTAINER_DROP_SHIM = "/usr/local/bin/coder_eval_drop_privilege.sh"
CONTAINER_CLAUDE_SHIM = "/usr/local/bin/coder_eval_claude_agent.sh"

# Paths a task's WORKDIR must never collide with: the container root and every
# framework-owned public or private mount. Consumed by SandboxConfig's
# working_dir validator and re-asserted host-side in docker_runner.
RESERVED_CONTAINER_DIRS = frozenset(
    {
        "/",
        CONTAINER_WORK_DIR,
        CONTAINER_AGENT_WORK_DIR,
        CONTAINER_GRADER_DIR,
        CONTAINER_INPUT_DIR,
        CONTAINER_OUTPUT_DIR,
        CONTAINER_TASK_DIR,
        CONTAINER_PRIVATE_PLUGIN_DIR,
        CONTAINER_AGENT_SKILLS_DIR,
    }
)
