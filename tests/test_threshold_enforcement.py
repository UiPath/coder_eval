"""Pass-threshold enforcement, driven through the production model methods.

These tests exercise ``EvaluationResult.calculate_weighted_score`` and
``EvaluationResult.all_criteria_passed`` directly — the single source of truth
the orchestrator's success gate now delegates to — rather than re-implementing
the ``all(...)`` / weighted-average formula inline.
"""

from datetime import datetime

import pytest

from coder_eval.models import (
    CriterionResult,
    EvaluationResult,
    FileExistsCriterion,
    FinalStatus,
)


def _make_result(scores: list[float]) -> EvaluationResult:
    """Build a minimal EvaluationResult carrying one file_exists result per score."""
    return EvaluationResult(
        task_id="threshold-test",
        task_description="d",
        agent_type="claude-code",
        started_at=datetime(2026, 1, 1, 12, 0, 0),
        final_status=FinalStatus.FAILURE,
        iteration_count=1,
        success_criteria_results=[
            CriterionResult(criterion_type="file_exists", description=f"crit-{i}", score=s)
            for i, s in enumerate(scores)
        ],
    )


class TestThresholdEnforcement:
    """pass_threshold is enforced per-criterion via the model gate."""

    def test_high_weighted_score_does_not_mask_a_failed_criterion(self):
        """A below-threshold criterion fails the gate even when the weighted score is high."""
        criteria = [
            FileExistsCriterion(path="f1.txt", description="crit-0", weight=1.0, pass_threshold=0.9),
            FileExistsCriterion(path="f2.txt", description="crit-1", weight=1.0, pass_threshold=0.9),
        ]
        result = _make_result([1.0, 0.8])  # second fails (< 0.9)

        result.calculate_weighted_score(criteria)
        assert result.weighted_score == 0.9  # weighted average is still high
        assert not result.all_criteria_passed(criteria)

    def test_all_pass_when_every_criterion_meets_threshold(self):
        criteria = [
            FileExistsCriterion(path="f1.txt", description="crit-0", weight=1.0, pass_threshold=0.9),
            FileExistsCriterion(path="f2.txt", description="crit-1", weight=1.0, pass_threshold=0.9),
        ]
        result = _make_result([0.95, 0.92])

        assert result.all_criteria_passed(criteria)

    def test_thresholds_enforced_independently(self):
        """Each criterion is gated against its own threshold."""
        criteria = [
            FileExistsCriterion(path="f1.txt", description="crit-0", weight=3.0, pass_threshold=1.0),
            FileExistsCriterion(path="f2.txt", description="crit-1", weight=1.0, pass_threshold=0.5),
        ]
        # Critical (threshold 1.0) at 0.99 fails; optional passes.
        assert not _make_result([0.99, 1.0]).all_criteria_passed(criteria)
        # Critical perfect; optional exactly at its threshold (0.5) — boundary passes.
        assert _make_result([1.0, 0.5]).all_criteria_passed(criteria)

    def test_high_weight_does_not_override_low_weight_failure(self):
        """A heavily-weighted pass cannot rescue a low-weight criterion below threshold."""
        criteria = [
            FileExistsCriterion(path="f1.txt", description="crit-0", weight=10.0, pass_threshold=0.9),
            FileExistsCriterion(path="f2.txt", description="crit-1", weight=1.0, pass_threshold=0.9),
        ]
        result = _make_result([1.0, 0.5])  # low-weight criterion fails

        result.calculate_weighted_score(criteria)
        assert result.weighted_score > 0.9
        assert not result.all_criteria_passed(criteria)

    def test_calculate_weighted_score_raises_on_length_mismatch(self):
        """A results/criteria length mismatch is a loud bug signal, not a silent unweighted fallback."""
        criteria = [FileExistsCriterion(path="f1.txt", description="crit-0", pass_threshold=0.9)]
        result = _make_result([1.0, 0.8])  # 2 results vs 1 criterion

        with pytest.raises(ValueError, match="length mismatch"):
            result.calculate_weighted_score(criteria)

    def test_all_criteria_passed_raises_on_length_mismatch(self):
        """The gate refuses to silently truncate a mismatched results/criteria pairing.

        The first result (0.1) is BELOW threshold on purpose: a naive
        ``all(... zip(strict=True))`` would short-circuit to False before the
        length check, so this pins that the explicit len() pre-check raises
        regardless of element ordering.
        """
        criteria = [FileExistsCriterion(path="f1.txt", description="crit-0", pass_threshold=0.9)]
        result = _make_result([0.1, 0.8])  # 2 results vs 1 criterion; first fails the threshold

        with pytest.raises(ValueError, match="length mismatch"):
            result.all_criteria_passed(criteria)

    def test_empty_inputs_score_zero_without_raising(self):
        """Empty results/criteria still yield 0.0 (not a raise) and an empty gate passes."""
        result = _make_result([])
        result.calculate_weighted_score([])
        assert result.weighted_score == 0.0
        assert result.all_criteria_passed([])  # all([]) is True
