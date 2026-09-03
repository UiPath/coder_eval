"""Shape detection for ``coder-eval evaluate``'s positional arguments.

``evaluate`` accepts two shapes that look alike on the command line:

    coder-eval evaluate tasks/hello.yaml ./my_solution   # grade a directory
    coder-eval evaluate runs/latest/default/hello/00     # re-grade a finished run

Both are "a task and a place", but the second carries its own task config and
trajectory inside ``task.json``, so nothing needs to be supplied twice. Rather
than adding a ``--run-dir-mode`` flag the caller has to remember, the shape is
detected from the target: a directory holding ``task.json`` is a run directory.

The logic lives here, apart from the Typer command, because it is pure — it does
one ``is_file`` probe and otherwise just maps arguments to a decision — so it can
be tested exhaustively without building sandboxes or invoking a CLI runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


TASK_JSON = "task.json"


class EvaluateMode(StrEnum):
    """Which of the two shapes the caller asked for."""

    WORK_DIR = "work_dir"
    """Grade a plain directory against a task file. The original behavior."""

    RUN_DIR = "run_dir"
    """Re-grade a finished run: its task.json supplies config and trajectory."""


@dataclass(frozen=True)
class EvaluateTarget:
    """The resolved intent behind ``evaluate``'s positional arguments."""

    mode: EvaluateMode
    target: Path
    """The run directory (RUN_DIR) or the directory to grade (WORK_DIR)."""

    task_file: Path | None
    """Explicit task YAML. Required in WORK_DIR mode; an optional override in RUN_DIR mode."""


class EvaluateTargetError(ValueError):
    """The two positionals do not describe either supported shape."""


def is_run_dir(path: Path) -> bool:
    """Whether ``path`` is a finished task run directory (it holds ``task.json``)."""
    return (path / TASK_JSON).is_file()


def resolve_evaluate_target(first: Path, second: Path | None) -> EvaluateTarget:
    """Map ``evaluate``'s one-or-two positionals onto a mode + target.

    Args:
        first: The first positional — a task file, or a run directory when it is
            the only argument.
        second: The second positional (the directory to grade), or None.

    Returns:
        The resolved target.

    Raises:
        EvaluateTargetError: If the arguments match neither shape. The message
            always names what was passed and what to pass instead: a caller who
            gets this wrong is one keystroke from the right command, and a bare
            "invalid arguments" would not tell them which one.
    """
    if second is None:
        # One argument: only the run-dir shape is unambiguous. A lone task file
        # names no place to grade, and a lone plain directory names no criteria.
        if not first.is_dir():
            raise EvaluateTargetError(
                f"{first} is not a directory. With a single argument, pass a finished run "
                + f"directory (one containing {TASK_JSON}). To grade a directory against a "
                + "task, pass both: coder-eval evaluate <task.yaml> <directory>"
            )
        if not is_run_dir(first):
            raise EvaluateTargetError(
                f"{first} holds no {TASK_JSON}, so it is not a run directory. Pass the task "
                + f"file too: coder-eval evaluate <task.yaml> {first}"
            )
        return EvaluateTarget(mode=EvaluateMode.RUN_DIR, target=first, task_file=None)

    # Two arguments. The second is the place; the first is the task file. When
    # that place turns out to be a run directory the caller is re-grading it with
    # a DIFFERENT task file than the one it ran with — the "iterate on my
    # criteria against an expensive run I already paid for" case, which is the
    # main reason to keep `execute` and `evaluate` separate at all. Allow it, and
    # let the caller be told which config won.
    mode = EvaluateMode.RUN_DIR if second.is_dir() and is_run_dir(second) else EvaluateMode.WORK_DIR
    return EvaluateTarget(mode=mode, target=second, task_file=first)
