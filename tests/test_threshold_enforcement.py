"""Pass-threshold enforcement, driven through the production model methods.

These tests exercise ``EvaluationResult.calculate_weighted_score`` and
``EvaluationResult.all_criteria_passed`` directly — the single source of truth
the orchestrator's success gate now delegates to — rather than re-implementing
the ``all(...)`` / weighted-average formula inline.
"""

from datetime import datetime

import pytest
from pydantic import ValidationError

from coder_eval.models import (
    CommandExecutedCriterion,
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

    def test_zero_weight_criterion_is_informational_and_does_not_gate(self):
        """weight=0 excludes a criterion from the score AND from the pass/fail gate."""
        criteria = [
            FileExistsCriterion(path="f1.txt", description="crit-0", weight=1.0, pass_threshold=0.9),
            FileExistsCriterion(path="f2.txt", description="crit-1", weight=0.0, pass_threshold=0.9),
        ]
        result = _make_result([1.0, 0.0])  # the informational criterion scores zero

        result.calculate_weighted_score(criteria)
        assert result.weighted_score == 1.0  # weight-0 contributes to neither term
        assert result.all_criteria_passed(criteria)  # ...and cannot flip the task to FAILURE

    def test_zero_weight_does_not_rescue_a_failing_gating_criterion(self):
        """Only the weight-0 criterion is exempt; real criteria still gate."""
        criteria = [
            FileExistsCriterion(path="f1.txt", description="crit-0", weight=1.0, pass_threshold=0.9),
            FileExistsCriterion(path="f2.txt", description="crit-1", weight=0.0, pass_threshold=0.9),
        ]
        assert not _make_result([0.5, 1.0]).all_criteria_passed(criteria)

    def test_all_zero_weight_criteria_leave_an_empty_gate(self):
        """A task of purely informational criteria has nothing to fail on."""
        criteria = [
            FileExistsCriterion(path="f1.txt", description="crit-0", weight=0.0, pass_threshold=0.9),
            FileExistsCriterion(path="f2.txt", description="crit-1", weight=0.0, pass_threshold=0.9),
        ]
        assert _make_result([0.0, 0.0]).all_criteria_passed(criteria)

    def test_zero_weight_cannot_be_armed_for_early_stop(self):
        """weight=0 + stop_when is incoherent: it would leave the early-stop gate empty."""
        with pytest.raises(ValidationError, match="weight=0"):
            CommandExecutedCriterion(
                description="informational + armed",
                weight=0.0,
                stop_when="pass",
                tool_name="Bash",
                command_pattern="pytest",
            )

    def test_zero_weight_cannot_declare_suite_thresholds(self):
        """weight=0 + suite_thresholds is incoherent: the suite gate drives the run exit code."""
        with pytest.raises(ValidationError, match="weight=0"):
            FileExistsCriterion(
                path="f1.txt",
                description="informational + suite-gated",
                weight=0.0,
                suite_thresholds={"mean": 0.8},
            )

    def test_empty_inputs_score_zero_without_raising(self):
        """Empty results/criteria still yield 0.0 (not a raise) and an empty gate passes."""
        result = _make_result([])
        result.calculate_weighted_score([])
        assert result.weighted_score == 0.0
        assert result.all_criteria_passed([])  # all([]) is True


class TestInformationalDisplayParity:
    """A weight-0 criterion must render as informational everywhere, not as failed.

    ``final_status`` and both exit codes already ignore weight-0 criteria. These
    pin the *display* surfaces to the same story, so a report header can't
    contradict its own row list.
    """

    def test_checker_stamps_gating_from_the_criterion(self, tmp_path):
        """The result mirrors ``is_gating`` so downstream renderers need no criterion."""
        from coder_eval.evaluation.checker import SuccessChecker
        from coder_eval.models import SandboxConfig
        from coder_eval.sandbox import Sandbox

        sandbox = Sandbox(SandboxConfig(driver="tempdir"), task_id="gating-display-test")
        sandbox.sandbox_dir = tmp_path
        checker = SuccessChecker(sandbox)

        gating = checker.check(FileExistsCriterion(path="missing.txt", description="gating", weight=1.0))
        informational = checker.check(FileExistsCriterion(path="missing.txt", description="informational", weight=0.0))

        assert gating.gating is True
        assert informational.gating is False
        # Both genuinely scored zero — un-gating changes the label, never the measurement.
        assert gating.score == 0.0
        assert informational.score == 0.0

    def test_criterion_result_defaults_to_gating(self):
        """Results persisted before the field existed must read back as gating."""
        assert CriterionResult(criterion_type="file_exists", description="d", score=0.0).gating is True
        restored = CriterionResult.model_validate_json(
            '{"criterion_type": "file_exists", "description": "d", "score": 0.0}'
        )
        assert restored.gating is True

    def test_html_report_labels_informational_and_excludes_it_from_the_count(self):
        """The HTML header counts gating criteria only, and the row says why."""
        from coder_eval.reports_html import _render_criteria

        html = _render_criteria(
            [
                CriterionResult(criterion_type="file_exists", description="real", score=1.0, gating=True),
                CriterionResult(criterion_type="file_exists", description="info", score=0.0, gating=False),
            ]
        )

        assert "(1/1 passed" in html  # NOT 1/2 — the informational miss isn't a failure
        assert "informational — not gated (weight: 0)" in html
        assert "1 informational" in html
