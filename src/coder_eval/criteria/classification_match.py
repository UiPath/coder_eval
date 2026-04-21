"""Classification-match criterion checker."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from coder_eval.criteria._classification_aggregate import overlay_classification_metrics
from coder_eval.criteria.base import BaseCriterion, register_criterion
from coder_eval.models import (
    ClassificationCriterionResult,
    ClassificationMatchCriterion,
    CriterionAggregate,
    CriterionResult,
)


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)

# Sentinels for the observed label — surfaced as their own classes in the
# suite rollup's confusion matrix so "didn't write anything" and "wrote
# something unrecognisable" are visible failure modes rather than vanishing.
_SENTINEL_NONE = "(none)"
_SENTINEL_OTHER = "(other)"


@register_criterion
class ClassificationMatchChecker(BaseCriterion[ClassificationMatchCriterion]):
    """Checker for ClassificationMatchCriterion.

    Reads a file written by the agent, extracts its single-line label, and
    compares it to the ground-truth label. Populates ``observed_label`` and
    ``expected_label`` on the result so the suite rollup can compute per-class
    precision / recall / F1 and a confusion matrix.
    """

    criterion_type = "classification_match"

    def _check_impl(
        self,
        criterion: ClassificationMatchCriterion,
        sandbox: Sandbox,
        reference_code: str | None = None,
        turn_records: list[TurnRecord] | None = None,
    ) -> CriterionResult:
        expected = self._canonicalize(criterion.expected_label, criterion)

        try:
            raw = sandbox.get_file_content(criterion.path)
        except FileNotFoundError:
            observed = _SENTINEL_NONE
        else:
            content = raw.strip()
            observed = self._canonicalize(content, criterion) if content else _SENTINEL_NONE
            if observed not in {*criterion.allowed_labels, _SENTINEL_NONE}:
                # Content didn't canonicalize to a known label.
                observed = _SENTINEL_OTHER

        score = 1.0 if observed == expected else 0.0
        details = f"observed={observed!r}, expected={expected!r}"

        return ClassificationCriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=details,
            observed_label=observed,
            expected_label=expected,
        )

    @staticmethod
    def _canonicalize(value: str, criterion: ClassificationMatchCriterion) -> str:
        """Map a raw string to its canonical form in ``allowed_labels``.

        When ``case_sensitive=False`` and the value matches an allowed label
        up to case, return the canonical casing from ``allowed_labels``.
        Otherwise return the value as-is; callers decide whether to replace
        it with a sentinel.
        """
        value = value.strip()
        if criterion.case_sensitive:
            return value
        lower_map = {label.lower(): label for label in criterion.allowed_labels}
        return lower_map.get(value.lower(), value)

    def aggregate(
        self,
        criterion: ClassificationMatchCriterion,
        per_row_results: list[CriterionResult],
    ) -> CriterionAggregate | None:
        """Baseline stats (from super) + sklearn-style classification metrics.

        Delegates the classification math to
        ``overlay_classification_metrics`` so skill_triggered and any future
        label-emitting criterion share one implementation.
        """
        base = super().aggregate(criterion, per_row_results)
        if base is None:
            return None
        return overlay_classification_metrics(base, per_row_results)
