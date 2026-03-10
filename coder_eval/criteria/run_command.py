"""Run command criterion checker."""

import logging
import re
from typing import TYPE_CHECKING

from coder_eval.criteria.base import BaseCriterion, register_criterion
from coder_eval.models import CriterionResult, RunCommandCriterion


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
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
        turn_records: list["TurnRecord"] | None = None,
    ) -> CriterionResult:
        """Execute command and check exit code, with optional stdout matching.

        Returns:
            Result with binary score (1.0 if all checks pass, 0.0 otherwise)
        """
        logger.debug(f"Running command for criterion '{criterion.description}': {criterion.command}")
        exit_code, stdout, stderr = sandbox.run_command(criterion.command, timeout=criterion.timeout)

        exit_ok = exit_code == criterion.expected_exit_code

        details = f"Exit code: {exit_code} (expected: {criterion.expected_exit_code})"
        if stdout:
            details += f"\nStdout: {stdout[:200]}"
        if stderr:
            details += f"\nStderr: {stderr[:200]}"

        # Optional stdout matching
        stdout_ok = True
        if criterion.expected_stdout is not None:
            stdout_ok = self._match_stdout(stdout, criterion.expected_stdout, criterion.stdout_match)
            details += f"\nStdout match ({criterion.stdout_match}): {'pass' if stdout_ok else 'FAIL'}"
            if not stdout_ok:
                details += f"\nExpected: {criterion.expected_stdout[:200]}"

        score = 1.0 if (exit_ok and stdout_ok) else 0.0

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=details,
        )

    @staticmethod
    def _match_stdout(actual: str, expected: str, mode: str) -> bool:
        """Compare stdout against expected value using the given mode."""
        actual_stripped = actual.strip()
        expected_stripped = expected.strip()
        if mode == "exact":
            return actual_stripped == expected_stripped
        if mode == "contains":
            return expected_stripped in actual_stripped
        if mode == "regex":
            try:
                return re.search(expected_stripped, actual_stripped) is not None
            except re.error as e:
                logger.warning("Invalid stdout regex pattern '%s': %s", expected_stripped, e)
                return False
        raise ValueError(f"Unknown stdout_match mode: {mode!r}")
