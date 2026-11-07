"""Typed error for judge-side infrastructure failures."""

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from coder_eval.models import GradingErrorInfo


class GradingError(RuntimeError):
    """A failed grading operation, with its original diagnostic."""

    def __init__(self, cause: Exception, *, component: str) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.component = component
        self.incident: GradingErrorInfo | None = None


class JudgeInfrastructureError(RuntimeError):
    """Judge-side infrastructure failure (transport error, throttle, 5xx, auth)
    after bounded retries.

    Deliberately NOT converted to a scored-0.0 ``CriterionResult``:
    ``handle_criterion_errors`` and ``SuccessChecker._check_single`` re-raise it
    so the orchestrator lands the row as ``FinalStatus.ERROR`` — a judge outage
    must be distinguishable from genuine agent failure in pass-rate metrics.
    """
