"""Resolution-time guardrail for docker tasks whose ``pre_run`` cannot run on the host.

Under ``--driver docker`` a task's ``pre_run`` runs on the HOST, before the
container, into a staging dir that seeds the container workspace (the helper
scripts + skills-repo tree live only host-side). That is correct for the vast
majority of setups (seed files, fixture-tree copies).

A small set of setups genuinely need to run INSIDE the container instead: a
``uv sync`` builds a virtualenv whose absolute paths are non-portable off the
host, and ``uip codedagent setup`` performs live-tenant provisioning that must
happen where the venv lives. Running these on the host produces a broken venv
and no in-container provisioning.

Until per-command in-container execution exists, this guard rejects exactly
those cases at resolution time (plan + run) with a clear redirect to
``--driver tempdir``, which runs everything in the one sandbox as before. It is
a temporary, narrow gate — not a broad block on all docker pre/post.
"""

from __future__ import annotations

import re

from ..models import TaskDefinition


# Command fragments that must run inside the container (venv build +
# live-tenant provisioning), not on the host. Matched case-insensitively
# against each resolved pre_run command string.
#
# Anchored to a COMMAND POSITION — the start of the string, or immediately after
# a shell separator (``&&``, ``;``, ``|``, or a newline, optionally followed by
# whitespace) — rather than a ``\b``-anywhere substring, so a quoted/argument
# mention such as ``echo 'run uv sync'`` or ``grep 'uv sync'`` does NOT
# false-positive-gate. A real leading or ``&&``-chained ``uv sync`` /
# ``uip codedagent setup`` still matches.
#
# Interim false-positive risk: the anchor is a heuristic, not a shell parser —
# an exotic construction (e.g. the pattern inside a ``$(...)`` command
# substitution that is itself not the leading command) could still slip past or
# mis-fire. Acceptable for this narrow, temporary guard; retired once
# per-command in-container execution exists.
_COMMAND_POSITION = r"(?:^|[&;|\n])\s*"
_IN_CONTAINER_ONLY_PATTERNS = (
    re.compile(_COMMAND_POSITION + r"uv\s+sync\b", re.IGNORECASE),
    re.compile(_COMMAND_POSITION + r"uip\s+codedagent\s+setup\b", re.IGNORECASE),
)


class DockerPreRunHostUnsafeError(ValueError):
    """Raised when a docker task's ``pre_run`` needs in-container execution.

    Subclasses ``ValueError`` so the run path's resolve -> ``typer.BadParameter``
    conversion covers it transparently, mirroring ``EarlyStopConfigError``.
    """


def validate_docker_pre_run_host_safety(task: TaskDefinition) -> None:
    """Reject a resolved docker task whose ``pre_run`` cannot run host-side.

    Reads the RESOLVED ``sandbox.driver`` (after the 5-layer merge, so a CLI
    ``--driver docker`` is honored) — no-op unless it is ``docker``. Then scans
    each resolved ``pre_run`` command for a fragment that must run inside the
    container (``uv sync`` / ``uip codedagent setup``). On a match, raises
    :class:`DockerPreRunHostUnsafeError` with a message pointing the user at
    ``--driver tempdir``. No-op for tempdir, and for docker tasks whose
    ``pre_run`` is host-safe.
    """
    if task.sandbox is None or task.sandbox.driver != "docker":
        return
    for cmd in task.pre_run:
        for pattern in _IN_CONTAINER_ONLY_PATTERNS:
            if pattern.search(cmd.command):
                raise DockerPreRunHostUnsafeError(
                    f"Task {task.task_id!r} has a pre_run command that must run inside the container "
                    + f"(matches {pattern.pattern!r}): {cmd.command!r}. Under --driver docker, pre_run runs "
                    + "on the host, which would build a non-portable venv / skip in-container provisioning. "
                    + "Run this task with --driver tempdir instead."
                )
