"""Timeout exceptions for evaluation lifecycle."""


class EvaluationTimeoutError(Exception):
    """Base timeout error for evaluation lifecycle.

    Wraps asyncio.TimeoutError with structured context about
    what timed out and where.
    """

    def __init__(
        self,
        message: str,
        *,
        timeout_seconds: float,
        layer: str,  # "turn" | "task"
        task_id: str | None = None,
        iteration: int | None = None,
        elapsed_seconds: float | None = None,
    ):
        super().__init__(message)
        self.timeout_seconds = timeout_seconds
        self.layer = layer
        self.task_id = task_id
        self.iteration = iteration
        self.elapsed_seconds = elapsed_seconds


class TurnTimeoutError(EvaluationTimeoutError):
    """Agent turn (communicate) exceeded its time limit."""

    def __init__(self, timeout_seconds: float, *, task_id: str | None = None, iteration: int | None = None):
        super().__init__(
            f"Agent turn timed out after {timeout_seconds}s (iteration {iteration})",
            timeout_seconds=timeout_seconds,
            layer="turn",
            task_id=task_id,
            iteration=iteration,
        )


class TaskTimeoutError(EvaluationTimeoutError):
    """Overall task evaluation loop exceeded its time limit."""

    def __init__(self, timeout_seconds: float, *, task_id: str | None = None, elapsed_seconds: float | None = None):
        super().__init__(
            f"Task timed out after {timeout_seconds}s",
            timeout_seconds=timeout_seconds,
            layer="task",
            task_id=task_id,
            elapsed_seconds=elapsed_seconds,
        )
