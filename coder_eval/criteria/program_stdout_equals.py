"""Program stdout equals criterion checker."""

import logging
from typing import TYPE_CHECKING

from coder_eval.criteria.base import BaseCriterion, register_criterion
from coder_eval.models import CriterionResult, ProgramStdoutEqualsCriterion


if TYPE_CHECKING:
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)


@register_criterion
class ProgramStdoutEqualsChecker(BaseCriterion[ProgramStdoutEqualsCriterion]):
    """Checker for ProgramStdoutEqualsCriterion."""

    criterion_type = "program_stdout_equals"

    def _check_impl(
        self,
        criterion: ProgramStdoutEqualsCriterion,
        sandbox: "Sandbox",
        reference_code: str | None = None,
    ) -> CriterionResult:
        """Execute command and compare output.

        Args:
            criterion: Program stdout criterion
            sandbox: Sandbox instance for command execution
            reference_code: Not used for this criterion

        Returns:
            Result with binary score (1.0 if exact match and exit 0, 0.0 otherwise)
        """
        logger.debug(f"Running command for criterion '{criterion.description}': {criterion.command}")
        exit_code, stdout, _stderr = sandbox.run_command(criterion.command, timeout=criterion.timeout)

        stdout_stripped = stdout.strip()
        expected_stripped = criterion.expected_output.strip()

        score = 1.0 if (stdout_stripped == expected_stripped and exit_code == 0) else 0.0

        details = f"Exit code: {exit_code}\n"
        details += f"Expected: {expected_stripped}\n"
        details += f"Got: {stdout_stripped}"

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=details,
        )
