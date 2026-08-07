"""Resolution-time guardrail for docker tasks whose ``pre_run`` is mis-marked.

Under ``--driver docker`` a task's ``pre_run`` is split by each command's
``runs_in``: the ``runs_in: host`` default runs on the HOST, before the
container, into a staging dir that seeds the container workspace (the helper
scripts + skills-repo tree live only host-side). That is correct for the vast
majority of setups (seed files, fixture-tree copies).

A small set of setups genuinely need to run INSIDE the container instead: a
``uv sync`` builds a virtualenv whose absolute paths are non-portable off the
host, and ``uip codedagent setup`` performs live-tenant provisioning that must
happen where the venv lives. Those are what ``runs_in: agent`` is for — it runs
the command in the coder_eval container, in the seeded workspace, before the
agent.

This guard catches the author mistake of leaving such a command at the
``runs_in: host`` default (or explicitly ``host``) under docker: run host-side,
a ``uv sync`` builds a venv the in-container agent cannot use, silently. It
rejects that at resolution time (plan + run) with a clear instruction to mark
the command ``runs_in: agent``. A command already correctly marked
``runs_in: agent`` PASSES. It is a narrow gate — not a broad block on all docker
pre/post — and it never fires under ``--driver tempdir`` (no container; ``agent``
and ``host`` are equivalent there).
"""

from __future__ import annotations

import re

from ..models import TaskDefinition


# Command fragments that must run inside the container (venv build +
# live-tenant provisioning), not on the host. Matched case-insensitively
# against each resolved pre_run command string that is still `runs_in: host`.
#
# Anchored to a COMMAND POSITION — the start of the string, or immediately after
# a shell separator (``&&``, ``;``, ``|``, or a newline, optionally followed by
# whitespace) — rather than a ``\b``-anywhere substring, so a quoted/argument
# mention such as ``echo 'run uv sync'`` or ``grep 'uv sync'`` does NOT
# false-positive-gate. A real leading or ``&&``-chained ``uv sync`` /
# ``uip codedagent setup`` still matches.
#
# False-positive risk: the anchor is a heuristic, not a shell parser — an exotic
# construction (e.g. the pattern inside a ``$(...)`` command substitution that is
# itself not the leading command) could still slip past or mis-fire. Acceptable
# for this narrow guard; the fix for a genuine mis-fire is to mark the command
# ``runs_in: agent`` (which the guard then passes).
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
    """Reject a resolved docker task whose ``pre_run`` is mis-marked ``runs_in: host``.

    Reads the RESOLVED ``sandbox.driver`` (after the 5-layer merge, so a CLI
    ``--driver docker`` is honored) — no-op unless it is ``docker``. Then scans
    each resolved ``runs_in: host`` ``pre_run`` command for a fragment that must
    run inside the container (``uv sync`` / ``uip codedagent setup``). On a
    match, raises :class:`DockerPreRunHostUnsafeError` instructing the author to
    mark the command ``runs_in: agent``. A command already marked
    ``runs_in: agent`` is SKIPPED (it runs in-container, exactly as needed).
    No-op for tempdir (no container; ``agent`` == ``host`` there), and for docker
    tasks whose ``pre_run`` is host-safe.
    """
    if task.sandbox is None or task.sandbox.driver != "docker":
        return
    for cmd in task.pre_run:
        # A command correctly marked runs_in: agent runs inside the container —
        # exactly where uv sync / uip codedagent setup must run. Not a mistake.
        if cmd.runs_in == "agent":
            continue
        for pattern in _IN_CONTAINER_ONLY_PATTERNS:
            if pattern.search(cmd.command):
                raise DockerPreRunHostUnsafeError(
                    f"Task {task.task_id!r} has a pre_run command that must run inside the container "
                    + f"(matches {pattern.pattern!r}): {cmd.command!r}. It is marked runs_in: host (the "
                    + "default), so under --driver docker it would run on the host — building a non-portable "
                    + "venv / skipping in-container provisioning. Mark this pre_run command `runs_in: agent` "
                    + "so it runs inside the container."
                )
