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


# Tokens a task YAML uses to address host directories from a criterion's path
# fields, mirroring the TASK_DIR / REFERENCE_DIR env vars ``run_command`` exposes.
# Defined in this dependency-free leaf so the models layer and judge_context share
# one definition without an import cycle.
# Rationale: .claude/notes/contracts.md § Judge context and untrusted text
TASK_DIR_TOKEN = "$TASK_DIR"
REFERENCE_DIR_TOKEN = "$REFERENCE_DIR"


def path_uses_token(path: str, token: str) -> bool:
    """Whether ``path`` addresses ``token`` — the bare token or token + separator.

    The separator requirement is what keeps an unrelated identifier like
    ``$TASK_DIRECTORY`` from matching ``$TASK_DIR``. Shared by the judge path
    resolver and TaskDefinition's load-time validator: when the two used
    different rules, a ``$REFERENCE_DIRECTORY/x`` entry was a sandbox path to one
    and a reference consumer to the other, hard-failing task load.
    """
    if path == token:
        return True
    return path.startswith(token) and path[len(token)] in "/\\"


def command_uses_token(command: str, token: str) -> bool:
    """Whether a shell ``command`` references ``token`` as a variable.

    The path-shaped sibling of :func:`path_uses_token`, for the one place a
    token appears inside free-form shell rather than as a path prefix. Both
    spellings count, because both are what a task author actually writes::

        diff -r "$REFERENCE_DIR" out/
        diff -r "${REFERENCE_DIR}" out/

    A plain ``token in command`` substring test matched only the first and let
    the brace form load clean — then run with the variable unset, so the command
    silently received an empty argument. It also matched ``$REFERENCE_DIRECTORY``,
    hard-failing load on an unrelated identifier. The trailing-character check
    below is the same separator rule :func:`path_uses_token` uses, widened to
    the shell characters that can legally follow a variable reference.
    """
    name = token.lstrip("$")
    brace = "${" + name + "}"
    if brace in command:
        return True
    idx = 0
    plain = "$" + name
    while (idx := command.find(plain, idx)) != -1:
        after = idx + len(plain)
        # A following identifier character means this is a LONGER variable name
        # ($REFERENCE_DIRECTORY), not our token.
        if after >= len(command) or not (command[after].isalnum() or command[after] == "_"):
            return True
        idx = after
    return False


# HAZARD: the one reliable "am I inside a task container?" signal. Every gate that
# means "in a container" MUST key on this and never on `sandbox.driver`, which the
# in-container entry point has already rewritten to `tempdir`.
# Rationale: .claude/notes/isolation.md § Capability drops and the anti-cheat window
IN_CONTAINER_ENV = "CODER_EVAL_IN_CONTAINER"

CONTAINER_WORK_DIR = "/work"
CONTAINER_INPUT_DIR = "/work/input"
CONTAINER_OUTPUT_DIR = "/work/output"
CONTAINER_TASK_DIR = "/work/task_dir"

# The per-run private copy of ``task.reference.directory``, exposed to criteria as
# ``REFERENCE_DIR``. Kept at mode 000 for the duration of every
# ``agent.communicate`` call (see ``fs_permissions.py``).
CONTAINER_REFERENCE_DIR = "/work/references"

# Where a DETACHED GRADE mounts the already-executed workspace. Separate from
# CONTAINER_OUTPUT_DIR because the two belong to different runs.
# Rationale: .claude/notes/isolation.md § Why the grading container gets a private scratch directory
CONTAINER_GRADE_WORKSPACE = "/work/workspace"

# Paths a task's WORKDIR must never collide with. Consumed by SandboxConfig's
# working_dir validator and re-asserted host-side in docker_runner.
RESERVED_CONTAINER_DIRS = frozenset(
    {
        "/",
        CONTAINER_WORK_DIR,
        CONTAINER_INPUT_DIR,
        CONTAINER_OUTPUT_DIR,
        CONTAINER_TASK_DIR,
        CONTAINER_REFERENCE_DIR,
        CONTAINER_GRADE_WORKSPACE,
    }
)
