"""Pytest criterion checker."""

import logging
import re
import shlex
from typing import TYPE_CHECKING

from coder_eval.criteria.base import BaseCriterion, register_criterion
from coder_eval.models import CriterionResult, PytestCriterion


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)

PYTEST_IMPERFECT_SCORE_CAP = 0.99  # Max score when exit code != 0 (prevents perfect score on failures)


@register_criterion
class PytestChecker(BaseCriterion[PytestCriterion]):
    """Checker for PytestCriterion."""

    criterion_type = "pytest"

    def _check_impl(
        self,
        criterion: PytestCriterion,
        sandbox: "Sandbox",
        reference_code: str | None = None,
        turn_records: list["TurnRecord"] | None = None,
    ) -> CriterionResult:
        """Run pytest and check results.

        Args:
            criterion: Pytest criterion
            sandbox: Sandbox instance for command execution
            reference_code: Not used for this criterion

        Returns:
            Result with fractional score (tests_passed / tests_total)
        """
        # Build pytest command with proper quoting for paths with spaces
        cmd_parts = ["pytest", shlex.quote(criterion.path)]
        cmd_parts.extend(shlex.quote(arg) for arg in criterion.args)
        command = " ".join(cmd_parts)
        logger.debug(f"Running pytest for criterion '{criterion.description}': {command}")

        exit_code, stdout, stderr = sandbox.run_command(command, timeout=criterion.timeout)

        # Combine stdout and stderr (pytest may write to either stream)
        combined_output = (stdout or "") + "\n" + (stderr or "")

        # Helper to extract counts from output
        def _extract_count(pattern: str) -> int:
            match = re.search(pattern, combined_output, re.IGNORECASE)
            return int(match.group(1)) if match else 0

        # Parse all test result categories
        passed = _extract_count(r"(\d+)\s+passed")
        failed = _extract_count(r"(\d+)\s+failed")
        errors = _extract_count(r"(\d+)\s+errors?")
        skipped = _extract_count(r"(\d+)\s+skipped")
        collected = _extract_count(r"collected\s+(\d+)\s+items?")

        # Calculate score
        if collected == 0:
            # No tests collected is a failure (likely wrong path or no test files)
            score = 0.0
            details = f"Exit code: {exit_code}, Pytest: No tests collected (score: 0.00)\n"
        elif passed + failed + errors == 0:
            # Tests collected but none ran (possibly all skipped)
            score = 0.0
            details = (
                f"Exit code: {exit_code}, Pytest: {collected} collected, {skipped} skipped, none ran (score: 0.00)\n"
            )
        else:
            # Normal case: calculate score from passed/failed/errors
            total_run = passed + failed + errors
            score = passed / total_run if total_run > 0 else 0.0

            # Never give perfect score if pytest exited non-zero
            if exit_code != 0 and score == 1.0:
                score = PYTEST_IMPERFECT_SCORE_CAP

            details = f"Exit code: {exit_code}, Pytest: {passed} passed, {failed} failed, {errors} errors"
            if skipped > 0:
                details += f", {skipped} skipped"
            details += f" out of {collected} collected (score: {score:.2f})\n"

        # Extract summary line from output for additional context
        for line in combined_output.split("\n"):
            if "passed" in line or "failed" in line or "error" in line:
                details += line + "\n"
                break

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=details,
        )
