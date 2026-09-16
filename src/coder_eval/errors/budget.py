"""Budget-exceeded exceptions for evaluation lifecycle."""

from __future__ import annotations


class BudgetExceededError(Exception):
    """Raised when a RunLimits budget is exceeded.

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


class BudgetUnenforceableError(Exception):
    """Raised when ``run_limits.max_usd`` is set but the run can price no turn it ran.

    The harness reported no cost and ``agent.model`` has no rate card entry, so the
    budget could not be enforced. An eval-config error: the run finalizes ``ERROR``.
    """
