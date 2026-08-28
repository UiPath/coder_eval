"""Post-merge validation for cross-field run-limit semantics."""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from coder_eval.models import TaskDefinition


INEFFECTIVE_TASK_TIMEOUT_WARNING = (
    "A larger task_timeout cannot extend the agent's single iteration; the agent budget is turn_timeout."
)


def validate_run_limits(task: TaskDefinition) -> tuple[str, ...]:
    """Return non-blocking warnings for the fully resolved run limits.

    The comparison belongs after config merge because either timeout may come
    from any of the five layers. The warning is about one agent call: even when
    dialog simulation makes several calls, a larger task-wide timeout cannot
    extend any call beyond its turn timeout.
    """
    limits = task.run_limits
    if limits is None or limits.task_timeout is None or limits.turn_timeout is None:
        return ()
    if limits.task_timeout <= limits.turn_timeout:
        return ()
    return (
        f"run_limits.task_timeout ({limits.task_timeout}s) exceeds "
        + f"run_limits.turn_timeout ({limits.turn_timeout}s). "
        + INEFFECTIVE_TASK_TIMEOUT_WARNING,
    )
