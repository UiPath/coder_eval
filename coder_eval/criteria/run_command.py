"""Run command criterion checker."""

import logging
from typing import TYPE_CHECKING

from coder_eval.criteria.base import BaseCriterion, register_criterion
from coder_eval.models import CriterionResult, RunCommandCriterion


if TYPE_CHECKING:
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)


@register_criterion
class RunCommandChecker(BaseCriterion[RunCommandCriterion]):
    """Checker for RunCommandCriterion."""

    criterion_type = "run_command"

    def _check_impl(
        self,
        criterion: RunCommandCriterion,
        sandbox: "Sandbox",
        reference_code: str | None = None,
    ) -> CriterionResult:
        """Execute command and check exit code.

        Args:
            criterion: Run command criterion
            sandbox: Sandbox instance for command execution
            reference_code: Not used for this criterion

        Returns:
            Result with binary score (1.0 if exit code matches, 0.0 if not)
        """
        logger.debug(f"Running command for criterion '{criterion.description}': {criterion.command}")
        exit_code, stdout, stderr = sandbox.run_command(criterion.command, timeout=criterion.timeout)

        score = 1.0 if exit_code == criterion.expected_exit_code else 0.0

        details = f"Exit code: {exit_code} (expected: {criterion.expected_exit_code})"
        if stdout:
            details += f"\nStdout: {stdout[:200]}"  # Truncate long output
        if stderr:
            details += f"\nStderr: {stderr[:200]}"

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=details,
        )
