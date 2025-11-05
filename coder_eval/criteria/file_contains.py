"""File contains criterion checker."""

import logging
from typing import TYPE_CHECKING

from coder_eval.criteria.base import BaseCriterion, register_criterion
from coder_eval.models import CriterionResult, FileContainsCriterion


if TYPE_CHECKING:
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)


@register_criterion
class FileContainsChecker(BaseCriterion[FileContainsCriterion]):
    """Checker for FileContainsCriterion."""

    criterion_type = "file_contains"

    def _check_impl(
        self,
        criterion: FileContainsCriterion,
        sandbox: "Sandbox",
        reference_code: str | None = None,
    ) -> CriterionResult:
        """Check if file contains required strings and excludes forbidden ones.

        Returns:
            CriterionResult with fractional score based on includes/excludes matched
        """
        if not sandbox.file_exists(criterion.path):
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                error=f"File '{criterion.path}' does not exist",
            )

        content = sandbox.get_file_content(criterion.path)

        # Calculate includes score (fraction of includes found)
        includes_found = sum(1 for inc in criterion.includes if inc in content)
        includes_total = len(criterion.includes)
        includes_score = includes_found / includes_total if includes_total > 0 else 1.0

        # Calculate excludes score (fraction of excludes absent)
        if criterion.excludes:
            excludes_found = sum(1 for exc in criterion.excludes if exc in content)
            excludes_total = len(criterion.excludes)
            excludes_score = 1.0 - (excludes_found / excludes_total)
        else:
            excludes_score = 1.0

        # Combined score: average of includes and excludes
        score = (includes_score + excludes_score) / 2.0

        # Build details
        details_parts = []
        details_parts.append(f"Includes: {includes_found}/{includes_total} found")
        if criterion.excludes:
            excludes_absent = len(criterion.excludes) - sum(1 for exc in criterion.excludes if exc in content)
            details_parts.append(f"Excludes: {excludes_absent}/{len(criterion.excludes)} absent")
        details_parts.append(f"Score: {score:.2f}")

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details="; ".join(details_parts),
        )
