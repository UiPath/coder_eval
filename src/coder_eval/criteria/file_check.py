"""Unified file check criterion checker (existence + contents + regex)."""

import logging
import re
from typing import TYPE_CHECKING

from coder_eval.criteria.base import BaseCriterion, CheckContext, register_criterion
from coder_eval.models import CriterionResult, FileCheckCriterion


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)


@register_criterion
class FileCheckChecker(BaseCriterion[FileCheckCriterion]):
    """Checker for FileCheckCriterion."""

    criterion_type = "file_check"

    def _check_impl(
        self,
        criterion: FileCheckCriterion,
        sandbox: "Sandbox",
        reference_code: str | None = None,
        *,
        turn_records: list["TurnRecord"] | None = None,
        context: CheckContext | None = None,
    ) -> CriterionResult:
        """Unified file check: existence, string includes/excludes, regex patterns.

        Returns:
            CriterionResult with fractional score based on active sub-checks
        """
        # 1. File existence (implicit)
        if not sandbox.file_exists(criterion.path):
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                error=f"File '{criterion.path}' does not exist",
            )

        has_includes = len(criterion.includes) > 0
        has_excludes = len(criterion.excludes) > 0
        has_patterns = len(criterion.patterns) > 0
        resolved = sandbox.resolved_path_label(criterion.path)

        # 2. Pure existence check (no sub-checks specified)
        if not has_includes and not has_excludes and not has_patterns:
            details = f"File '{criterion.path}' exists"
            if resolved:
                details += f" (resolved: {resolved})"
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=1.0,
                details=details,
            )

        # 3. Read file content
        content = sandbox.get_file_content(criterion.path)

        scores: list[float] = []
        details_parts: list[str] = []
        if resolved:
            details_parts.append(f"Resolved: {resolved}")

        # 4a. Includes score
        if has_includes:
            found = sum(1 for inc in criterion.includes if inc in content)
            total = len(criterion.includes)
            includes_score = found / total
            scores.append(includes_score)
            details_parts.append(f"Includes: {found}/{total} found")

        # 4b. Excludes score
        if has_excludes:
            present = sum(1 for exc in criterion.excludes if exc in content)
            total = len(criterion.excludes)
            excludes_score = 1.0 - (present / total)
            scores.append(excludes_score)
            absent = total - present
            details_parts.append(f"Excludes: {absent}/{total} absent")

        # 4c. Pattern scores
        if has_patterns:
            pattern_scores: list[float] = []
            pattern_details: list[str] = []
            for p in criterion.patterns:
                try:
                    regex = re.compile(p.pattern, p.flags)
                    match = regex.search(content)
                except re.error as e:
                    logger.warning("Invalid regex pattern '%s': %s", p.pattern, e)
                    pattern_scores.append(0.0)
                    pattern_details.append(f"Invalid regex '{p.pattern}': {e}")
                    continue

                if p.must_match:
                    pattern_scores.append(1.0 if match else 0.0)
                else:
                    pattern_scores.append(1.0 if match is None else 0.0)

            matched = sum(1 for s in pattern_scores if s == 1.0)
            patterns_score = sum(pattern_scores) / len(pattern_scores)
            scores.append(patterns_score)
            detail = f"Patterns: {matched}/{len(pattern_scores)} matched"
            if pattern_details:
                detail += f" ({'; '.join(pattern_details)})"
            details_parts.append(detail)

        # 5. Combine active category scores
        score = sum(scores) / len(scores)
        details_parts.append(f"Score: {score:.2f}")

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details="; ".join(details_parts),
        )
