"""Commands efficiency criterion checker."""

import logging
from typing import TYPE_CHECKING, ClassVar

from coder_eval.criteria.base import BaseCriterion, CheckContext, register_criterion
from coder_eval.models import CommandsEfficiencyCriterion, CriterionResult


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)


def compute_commands_efficiency(actual: int, expected: int) -> float:
    """Compute commands efficiency score.

    Returns expected / max(actual, expected), or 0.0 if actual is 0.
    Raises ValueError if expected < 1 (callers must validate).
    """
    if expected < 1:
        raise ValueError(f"expected must be >= 1, got {expected}")
    if actual == 0:
        return 0.0
    return expected / max(actual, expected)


@register_criterion
class CommandsEfficiencyChecker(BaseCriterion[CommandsEfficiencyCriterion]):
    """Checker for CommandsEfficiencyCriterion."""

    criterion_type: ClassVar[str] = "commands_efficiency"

    def _check_impl(
        self,
        criterion: CommandsEfficiencyCriterion,
        sandbox: "Sandbox",
        reference_code: str | None = None,
        *,
        turn_records: list["TurnRecord"] | None = None,
        context: CheckContext | None = None,
    ) -> CriterionResult:
        if turn_records is None:
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                error="turn_records not provided to checker",
                details="No turn records available",
            )

        actual = sum(len(t.commands) for t in turn_records)
        if actual == 0:
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                details="No commands recorded (agent may not have run)",
            )

        score = compute_commands_efficiency(actual, criterion.expected_commands)
        over = actual - criterion.expected_commands
        budget_note = " (at or under budget)" if over <= 0 else f" (over budget by {over} commands)"
        details = f"Commands: {actual}, expected: {criterion.expected_commands}, efficiency: {score:.3f}{budget_note}"

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=details,
        )
