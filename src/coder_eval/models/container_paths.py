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
# Agent-readable sanitized skill-bundle copy mount (docs/commands/skills only;
# no grader trees). The agent's plugin discovery reads from here, never the raw
# skills-repo checkout (which is not mounted into the agent container at all).
CONTAINER_SKILL_DOCS_DIR = "/work/skills"

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
