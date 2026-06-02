"""Pylint score criterion checker."""

import logging
import re
import shlex
from typing import TYPE_CHECKING

from coder_eval.criteria.base import BaseCriterion, CheckContext, register_criterion
from coder_eval.models import CriterionResult, PylintScoreCriterion


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)


@register_criterion
class PylintScoreChecker(BaseCriterion[PylintScoreCriterion]):
    """Checker for PylintScoreCriterion."""

    criterion_type = "pylint_score"

    def _check_impl(
        self,
        criterion: PylintScoreCriterion,
        sandbox: "Sandbox",
        reference_code: str | None = None,
        *,
        turn_records: list["TurnRecord"] | None = None,
        context: CheckContext | None = None,
    ) -> CriterionResult:
        """Run pylint and extract quality score.

        Pylint output format:
            -------------------------------------------------------------------
            Your code has been rated at 8.75/10 (previous run: 8.50/10, +0.25)

        Args:
            criterion: Pylint score criterion
            sandbox: Sandbox instance for command execution
            reference_code: Not used for this criterion

        Returns:
            Result with continuous score (0.0-1.0) normalized from pylint's 0-10 scale
        """
        # Build pylint command with proper quoting for paths with spaces
        cmd_parts = ["pylint", shlex.quote(criterion.path)]

        # Add optional rcfile
        if criterion.rcfile:
            cmd_parts.extend(["--rcfile", shlex.quote(criterion.rcfile)])

        # Add optional fail-under (makes pylint exit non-zero below threshold)
        if criterion.fail_under is not None:
            cmd_parts.extend(["--fail-under", str(criterion.fail_under)])

        # Add any additional arguments (quote each)
        cmd_parts.extend(shlex.quote(arg) for arg in criterion.args)

        command = " ".join(cmd_parts)
        logger.debug(f"Running pylint for criterion '{criterion.description}': {command}")

        # Execute pylint in sandbox
        exit_code, stdout, stderr = sandbox.run_command(command, timeout=criterion.timeout)

        # Parse pylint output for score
        score_result = self._parse_pylint_output(stdout, stderr)

        if score_result is None:
            # Pylint ran but no score found - treat as error
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                error="Could not parse pylint score from output",
            )

        pylint_score, details_text = score_result

        # Normalize score to 0.0-1.0 (clamp to 0.0 minimum for negative scores)
        normalized_score = max(0.0, pylint_score / 10.0)

        # Determine threshold (min_score takes precedence for clarity)
        if criterion.min_score is not None:
            threshold_text = f"min_score={criterion.min_score}/10"
        else:
            threshold_text = f"pass_threshold={criterion.pass_threshold}"

        # Build comprehensive details
        details = f"Pylint score: {pylint_score:.2f}/10 (normalized: {normalized_score:.3f})\n"
        details += f"Threshold: {threshold_text}\n"
        details += f"Exit code: {exit_code}\n"
        details += f"\n{details_text}"

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=normalized_score,
            details=details,
        )

    def _parse_pylint_output(self, stdout: str, stderr: str) -> tuple[float, str] | None:
        """Parse pylint output to extract score and details.

        Args:
            stdout: Standard output from pylint
            stderr: Standard error from pylint

        Returns:
            Tuple of (score, details_text) or None if parsing fails
        """
        # Combine output (pylint may write to stderr)
        output = stdout + "\n" + stderr

        # Pylint always outputs scores in the format "Your code has been rated at X.XX/10"
        # This is the standard pylint output format and is unlikely to change
        # Pattern breakdown (Issue 3 fix - supports negative scores):
        #   (-?\d+(?:\.\d+)?) - Captures score with optional minus and decimal
        #     -?              - Optional minus sign for negative scores
        #     \d+             - One or more digits (integer part)
        #     (?:\.\d+)?      - Optional decimal part (non-capturing group)
        #   /10               - Literal "/10" suffix from pylint output
        # Matches: "-1.50/10", "0.00/10", "8/10", "9.75/10"
        score_pattern = r"Your code has been rated at (-?\d+(?:\.\d+)?)/10"

        match = re.search(score_pattern, output)

        if not match:
            return None

        score = float(match.group(1))

        # Extract summary section (everything after the score line)
        score_line_idx = output.find(match.group(0))
        summary = output[score_line_idx:].strip()

        # Truncate if too long (keep first 500 chars)
        if len(summary) > 500:
            summary = summary[:500] + "\n... (truncated)"

        return score, summary
