"""File existence criterion checker."""

import logging
from typing import TYPE_CHECKING

from coder_eval.criteria.base import BaseCriterion, CheckContext, register_criterion
from coder_eval.models import CriterionResult, FileExistsCriterion


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)


@register_criterion
class FileExistsChecker(BaseCriterion[FileExistsCriterion]):
    """Checker for FileExistsCriterion."""

    criterion_type = "file_exists"

    def _check_impl(
        self,
        criterion: FileExistsCriterion,
        sandbox: "Sandbox",
        reference_code: str | None = None,
        *,
        turn_records: list["TurnRecord"] | None = None,
        context: CheckContext | None = None,
    ) -> CriterionResult:
        """Check if a file exists in the sandbox.

        Returns:
            CriterionResult with score 1.0 if exists, 0.0 if not
        """
        exists = sandbox.file_exists(criterion.path)
        score = 1.0 if exists else 0.0

        details = f"File '{criterion.path}' {'exists' if exists else 'does not exist'}"
        resolved = sandbox.resolved_path_label(criterion.path)
        if resolved:
            details += f" (resolved: {resolved})"

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=details,
        )
