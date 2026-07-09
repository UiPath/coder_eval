"""Budget-exceeded exceptions for evaluation lifecycle."""

from __future__ import annotations


class BudgetExceededError(Exception):
    """Raised when a RunLimits budget is exceeded between agent turns.

    Carries which budget tripped and the over-budget value so the
    orchestrator can record the status reason without re-computing.
    """

    def __init__(
        self,
        budget_name: str,
        *,
        actual: float,
        limit: float,
        task_id: str | None = None,
        iteration: int | None = None,
    ):
        self.budget_name = budget_name
        self.actual = actual
        self.limit = limit
        self.task_id = task_id
        self.iteration = iteration
        suffix = f" (iteration {iteration})" if iteration is not None else ""
        super().__init__(f"{budget_name} budget exceeded: {actual:g} > {limit:g}{suffix}")
