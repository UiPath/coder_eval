"""File matches regex criterion checker."""

import logging
import re
from typing import TYPE_CHECKING

from coder_eval.criteria.base import BaseCriterion, register_criterion
from coder_eval.models import CriterionResult, FileMatchesRegexCriterion


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)


@register_criterion
class FileMatchesRegexChecker(BaseCriterion[FileMatchesRegexCriterion]):
    """Checker for FileMatchesRegexCriterion."""

    criterion_type = "file_matches_regex"

    def _check_impl(
        self,
        criterion: FileMatchesRegexCriterion,
        sandbox: "Sandbox",
        reference_code: str | None = None,
        turn_records: list["TurnRecord"] | None = None,
    ) -> CriterionResult:
        """Check if file content matches the regex pattern.

        Args:
            criterion: File matches regex criterion
            sandbox: Sandbox instance for file access
            reference_code: Not used for this criterion

        Returns:
            Result with binary score (1.0 if pattern matches as expected, 0.0 otherwise)
        """
        if not sandbox.file_exists(criterion.path):
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                error=f"File '{criterion.path}' does not exist",
            )

        content = sandbox.get_file_content(criterion.path)

        # Compile and search for pattern
        try:
            regex = re.compile(criterion.pattern, criterion.flags)
            match = regex.search(content)
        except re.error as e:
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                error=f"Invalid regex pattern: {e}",
            )

        # Check based on must_match flag
        if criterion.must_match:
            score = 1.0 if match is not None else 0.0
            if match:
                matched_text = match.group()[:100]
                details = f"Pattern '{criterion.pattern}' found in file (matched: '{matched_text}')"
            else:
                details = f"Pattern '{criterion.pattern}' not found in file"
        else:
            score = 1.0 if match is None else 0.0
            if match is None:
                details = f"Pattern '{criterion.pattern}' correctly absent from file"
            else:
                matched_text = match.group()[:100]
                details = f"Pattern '{criterion.pattern}' found but should not be present (matched: '{matched_text}')"

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=details,
        )
