"""File contains criterion checker."""

import logging
from typing import TYPE_CHECKING

from coder_eval.criteria.base import BaseCriterion, CheckContext, register_criterion
from coder_eval.models import CriterionResult, FileContainsCriterion


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
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
        *,
        turn_records: list["TurnRecord"] | None = None,
        context: CheckContext | None = None,
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

        # Combined score: only average when both categories are active
        has_includes = includes_total > 0
        has_excludes = bool(criterion.excludes)
        if has_includes and has_excludes:
            score = (includes_score + excludes_score) / 2.0
        elif has_includes:
            score = includes_score
        elif has_excludes:
            score = excludes_score
        else:
            score = 1.0

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
