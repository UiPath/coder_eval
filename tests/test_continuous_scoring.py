"""Comprehensive tests for continuous scoring feature.

Tests cover:
- Model validation (weight, pass_threshold, score ranges)
- Weighted score calculation
- Fractional scoring behavior
- Pass threshold logic
- Edge cases and boundary conditions
"""

from datetime import datetime

import pytest
from pydantic import ValidationError

from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import (
    AgentKind,
    CriterionResult,
    EvaluationResult,
    FileContainsCriterion,
    FileExistsCriterion,
    SandboxConfig,
)
from coder_eval.sandbox import Sandbox


def create_test_evaluation_result(**kwargs):
    """Helper to create EvaluationResult with defaults for required fields."""
    defaults = {
        "task_id": "test",
        "task_description": "Test task",
        "variant_id": "test-variant",
        "agent_type": AgentKind.CLAUDE_CODE,
        "started_at": datetime.now(),
        "final_status": "SUCCESS",
        "iteration_count": 1,
        "iterations": [],
        "success_criteria_results": [],
    }
    defaults.update(kwargs)
    return EvaluationResult(**defaults)


class TestModelValidation:
    """Test Pydantic validation for continuous scoring fields."""

    def test_weight_zero_is_allowed(self):
        """weight=0 is valid: 'run but don't score' (criterion runs, excluded from weighted avg)."""
        criterion = FileExistsCriterion(
            path="test.txt",
            description="Test",
            weight=0.0,
        )
        assert criterion.weight == 0.0

    def test_weight_cannot_be_negative(self):
        """Test that weight cannot be negative."""
        with pytest.raises(ValidationError) as exc_info:
            FileExistsCriterion(
                path="test.txt",
                description="Test",
                weight=-1.0,  # Invalid: must be >= 0
            )
        assert "greater than or equal to 0" in str(exc_info.value).lower()

    def test_weight_default_is_one(self):
        """Test that weight defaults to 1.0."""
        criterion = FileExistsCriterion(path="test.txt", description="Test")
        assert criterion.weight == 1.0

    def test_pass_threshold_in_valid_range(self):
        """Test that pass_threshold must be between 0 and 1."""
        # Valid: 0.0
        criterion = FileExistsCriterion(path="test.txt", description="Test", pass_threshold=0.0)
        assert criterion.pass_threshold == 0.0

        # Valid: 1.0
        criterion = FileExistsCriterion(path="test.txt", description="Test", pass_threshold=1.0)
        assert criterion.pass_threshold == 1.0

        # Valid: 0.9
        criterion = FileExistsCriterion(path="test.txt", description="Test", pass_threshold=0.9)
        assert criterion.pass_threshold == 0.9

    def test_pass_threshold_cannot_exceed_one(self):
        """Test that pass_threshold cannot be > 1.0."""
        with pytest.raises(ValidationError) as exc_info:
            FileExistsCriterion(
                path="test.txt",
                description="Test",
                pass_threshold=1.1,  # Invalid: must be <= 1.0
            )
        assert "less than or equal to 1" in str(exc_info.value).lower()

    def test_pass_threshold_cannot_be_negative(self):
        """Test that pass_threshold cannot be negative."""
        with pytest.raises(ValidationError) as exc_info:
            FileExistsCriterion(
                path="test.txt",
                description="Test",
                pass_threshold=-0.1,  # Invalid: must be >= 0.0
            )
        assert "greater than or equal to 0" in str(exc_info.value).lower()

    def test_pass_threshold_default_is_ninety_percent(self):
        """Test that pass_threshold defaults to 0.9 (90%)."""
        criterion = FileExistsCriterion(path="test.txt", description="Test")
        assert criterion.pass_threshold == 0.9

    def test_score_in_valid_range(self):
        """Test that CriterionResult score must be between 0 and 1."""
        # Valid: 0.0
        result = CriterionResult(criterion_type="file_exists", description="Test", score=0.0)
        assert result.score == 0.0

        # Valid: 1.0
        result = CriterionResult(criterion_type="file_exists", description="Test", score=1.0)
        assert result.score == 1.0

        # Valid: 0.5
        result = CriterionResult(criterion_type="file_exists", description="Test", score=0.5)
        assert result.score == 0.5

    def test_score_cannot_exceed_one(self):
        """Test that score cannot be > 1.0."""
        with pytest.raises(ValidationError) as exc_info:
            CriterionResult(
                criterion_type="file_exists",
                description="Test",
                score=1.1,  # Invalid
            )
        assert "less than or equal to 1" in str(exc_info.value).lower()

    def test_score_cannot_be_negative(self):
        """Test that score cannot be negative."""
        with pytest.raises(ValidationError) as exc_info:
            CriterionResult(
                criterion_type="file_exists",
                description="Test",
                score=-0.1,  # Invalid
            )
        assert "greater than or equal to 0" in str(exc_info.value).lower()


class TestWeightedScoreCalculation:
    """Test weighted score calculation logic."""

    def test_weighted_score_equal_weights(self):
        """Test weighted score with all weights equal to 1.0."""
        result = create_test_evaluation_result(
            task_id="test",
            final_status="SUCCESS",
            success_criteria_results=[
                CriterionResult(criterion_type="file_exists", description="A", score=1.0),
                CriterionResult(criterion_type="file_exists", description="B", score=0.5),
                CriterionResult(criterion_type="file_exists", description="C", score=0.0),
            ],
            turns=[],
        )

        criteria = [
            FileExistsCriterion(path="a.txt", description="A", weight=1.0),
            FileExistsCriterion(path="b.txt", description="B", weight=1.0),
            FileExistsCriterion(path="c.txt", description="C", weight=1.0),
        ]

        result.calculate_weighted_score(criteria)
        # (1.0*1.0 + 0.5*1.0 + 0.0*1.0) / (1.0 + 1.0 + 1.0) = 1.5 / 3.0 = 0.5
        assert result.weighted_score == 0.5

    def test_weight_zero_criterion_excluded_from_score(self):
        """A weight=0 criterion runs but does not affect the weighted score ('run but don't score')."""
        result = create_test_evaluation_result(
            task_id="test",
            final_status="SUCCESS",
            success_criteria_results=[
                CriterionResult(criterion_type="run_command", description="setup", score=0.0),
                CriterionResult(criterion_type="file_exists", description="real", score=1.0),
            ],
            turns=[],
        )
        criteria = [
            FileExistsCriterion(path="setup", description="setup", weight=0.0),  # run-but-don't-score
            FileExistsCriterion(path="real.txt", description="real", weight=1.0),
        ]
        result.calculate_weighted_score(criteria)
        # The weight-0 criterion (score 0.0) is excluded; only the weight-1 criterion counts.
        # (0.0*0.0 + 1.0*1.0) / (0.0 + 1.0) = 1.0
        assert result.weighted_score == 1.0

    def test_all_zero_weights_scores_zero_without_dividing(self):
        """All-zero weights must not divide by zero -- the guard yields 0.0."""
        result = create_test_evaluation_result(
            task_id="test",
            final_status="SUCCESS",
            success_criteria_results=[
                CriterionResult(criterion_type="file_exists", description="A", score=1.0),
            ],
            turns=[],
        )
        criteria = [FileExistsCriterion(path="a.txt", description="A", weight=0.0)]
        result.calculate_weighted_score(criteria)
        assert result.weighted_score == 0.0

    def test_weighted_score_different_weights(self):
        """Test weighted score with different weights."""
        result = create_test_evaluation_result(
            task_id="test",
            final_status="SUCCESS",
            success_criteria_results=[
                CriterionResult(criterion_type="file_exists", description="A", score=1.0),
                CriterionResult(criterion_type="file_exists", description="B", score=0.0),
            ],
            turns=[],
        )

        criteria = [
            FileExistsCriterion(path="a.txt", description="A", weight=3.0),  # More important
            FileExistsCriterion(path="b.txt", description="B", weight=1.0),
        ]

        result.calculate_weighted_score(criteria)
        # (1.0*3.0 + 0.0*1.0) / (3.0 + 1.0) = 3.0 / 4.0 = 0.75
        assert result.weighted_score == 0.75

    def test_weighted_score_all_perfect(self):
        """Test weighted score when all criteria score 1.0."""
        result = create_test_evaluation_result(
            task_id="test",
            final_status="SUCCESS",
            success_criteria_results=[
                CriterionResult(criterion_type="file_exists", description="A", score=1.0),
                CriterionResult(criterion_type="file_exists", description="B", score=1.0),
                CriterionResult(criterion_type="file_exists", description="C", score=1.0),
            ],
            turns=[],
        )

        criteria = [
            FileExistsCriterion(path="a.txt", description="A", weight=2.0),
            FileExistsCriterion(path="b.txt", description="B", weight=1.0),
            FileExistsCriterion(path="c.txt", description="C", weight=3.0),
        ]

        result.calculate_weighted_score(criteria)
        assert result.weighted_score == 1.0

    def test_weighted_score_all_zero(self):
        """Test weighted score when all criteria score 0.0."""
        result = create_test_evaluation_result(
            task_id="test",
            final_status="FAILURE",
            success_criteria_results=[
                CriterionResult(criterion_type="file_exists", description="A", score=0.0),
                CriterionResult(criterion_type="file_exists", description="B", score=0.0),
            ],
            turns=[],
        )

        criteria = [
            FileExistsCriterion(path="a.txt", description="A", weight=1.0),
            FileExistsCriterion(path="b.txt", description="B", weight=1.0),
        ]

        result.calculate_weighted_score(criteria)
        assert result.weighted_score == 0.0

    def test_weighted_score_empty_results(self):
        """Test weighted score calculation with no results."""
        result = create_test_evaluation_result(
            task_id="test",
            final_status="ERROR",
            success_criteria_results=[],
            turns=[],
        )

        result.calculate_weighted_score([])
        assert result.weighted_score == 0.0

    def test_weighted_score_mismatch_raises(self):
        """A results/criteria length mismatch fails loud instead of fabricating a score."""
        result = create_test_evaluation_result(
            task_id="my-task",
            final_status="SUCCESS",
            success_criteria_results=[
                CriterionResult(criterion_type="file_exists", description="A", score=1.0),
                CriterionResult(criterion_type="file_exists", description="B", score=0.5),
            ],
            turns=[],
        )

        # Only provide 1 criterion (mismatch with 2 results)
        criteria = [
            FileExistsCriterion(path="a.txt", description="A", weight=1.0),
        ]

        with pytest.raises(ValueError, match="my-task"):
            result.calculate_weighted_score(criteria)
        # No weight-ignoring score was fabricated.
        assert result.weighted_score is None

    def test_weighted_score_empty_criteria_does_not_raise(self):
        """Empty results/criteria still yields 0.0 without raising (ERROR rows rely on this)."""
        result = create_test_evaluation_result(
            task_id="test",
            final_status="ERROR",
            success_criteria_results=[],
            turns=[],
        )

        # Non-empty criteria but empty results -> empty guard, not the mismatch raise.
        criteria = [
            FileExistsCriterion(path="a.txt", description="A", weight=1.0),
        ]
        result.calculate_weighted_score(criteria)
        assert result.weighted_score == 0.0

    def test_weighted_score_single_criterion(self):
        """Test weighted score with a single criterion."""
        result = create_test_evaluation_result(
            task_id="test",
            final_status="SUCCESS",
            success_criteria_results=[
                CriterionResult(criterion_type="file_exists", description="A", score=0.7),
            ],
            turns=[],
        )

        criteria = [
            FileExistsCriterion(path="a.txt", description="A", weight=5.0),
        ]

        result.calculate_weighted_score(criteria)
        # (0.7 * 5.0) / 5.0 = 0.7
        assert result.weighted_score == 0.7


class TestFractionalScoring:
    """Test fractional scoring behavior in SuccessChecker."""

    def test_file_contains_partial_includes_no_excludes(self, tmp_path):
        """Test file_contains with partial matches on includes."""
        sandbox_dir = tmp_path / "sandbox"
        sandbox_dir.mkdir()
        test_file = sandbox_dir / "test.txt"
        test_file.write_text("Hello World")

        config = SandboxConfig(driver="tempdir")
        sandbox = Sandbox(config=config, task_id="test")
        sandbox.sandbox_dir = sandbox_dir  # Set sandbox_dir, not sandbox_path
        checker = SuccessChecker(sandbox)

        criterion = FileContainsCriterion(
            path="test.txt",
            includes=["Hello", "Goodbye"],  # Only 1/2 found
            description="Test",
        )

        result = checker.check(criterion)
        # includes: 1/2 = 0.5, no excludes so score = includes_score only
        assert result.score == 0.5

    def test_file_contains_all_includes_no_excludes(self, tmp_path):
        """Test file_contains with all includes found."""
        sandbox_dir = tmp_path / "sandbox"
        sandbox_dir.mkdir()
        test_file = sandbox_dir / "test.txt"
        test_file.write_text("Hello World")

        config = SandboxConfig(driver="tempdir")
        sandbox = Sandbox(config=config, task_id="test")
        sandbox.sandbox_dir = sandbox_dir
        checker = SuccessChecker(sandbox)

        criterion = FileContainsCriterion(
            path="test.txt",
            includes=["Hello", "World"],  # Both found
            description="Test",
        )

        result = checker.check(criterion)
        # includes: 2/2 = 1.0, excludes: 1.0 (none), avg = 1.0
        assert result.score == 1.0

    def test_file_contains_includes_with_present_excludes(self, tmp_path):
        """Test file_contains when excludes are present (should be absent)."""
        sandbox_dir = tmp_path / "sandbox"
        sandbox_dir.mkdir()
        test_file = sandbox_dir / "test.txt"
        test_file.write_text("Hello World Forbidden")

        config = SandboxConfig(driver="tempdir")
        sandbox = Sandbox(config=config, task_id="test")
        sandbox.sandbox_dir = sandbox_dir
        checker = SuccessChecker(sandbox)

        criterion = FileContainsCriterion(
            path="test.txt",
            includes=["Hello"],  # Found
            excludes=["Forbidden"],  # Present but shouldn't be
            description="Test",
        )

        result = checker.check(criterion)
        # includes: 1/1 = 1.0, excludes: 1 found out of 1 = 0.0, avg = 0.5
        assert result.score == 0.5

    def test_file_contains_includes_with_absent_excludes(self, tmp_path):
        """Test file_contains when excludes are properly absent."""
        sandbox_dir = tmp_path / "sandbox"
        sandbox_dir.mkdir()
        test_file = sandbox_dir / "test.txt"
        test_file.write_text("Hello World")

        config = SandboxConfig(driver="tempdir")
        sandbox = Sandbox(config=config, task_id="test")
        sandbox.sandbox_dir = sandbox_dir
        checker = SuccessChecker(sandbox)

        criterion = FileContainsCriterion(
            path="test.txt",
            includes=["Hello"],  # Found
            excludes=["Forbidden", "BadWord"],  # None present (good)
            description="Test",
        )

        result = checker.check(criterion)
        # includes: 1/1 = 1.0, excludes: 0 found out of 2 = 1.0, avg = 1.0
        assert result.score == 1.0


class TestPassThresholdBehavior:
    """Test pass threshold logic in evaluation."""

    def test_score_meets_threshold(self):
        """Test criterion passes when score meets threshold."""
        criterion = FileExistsCriterion(path="test.txt", description="Test", pass_threshold=0.9)
        result = CriterionResult(
            criterion_type="file_exists",
            description="Test",
            score=0.95,  # Exceeds threshold
        )

        assert result.score >= criterion.pass_threshold

    def test_score_equals_threshold(self):
        """Test criterion passes when score equals threshold."""
        criterion = FileExistsCriterion(path="test.txt", description="Test", pass_threshold=0.9)
        result = CriterionResult(
            criterion_type="file_exists",
            description="Test",
            score=0.9,  # Equals threshold
        )

        assert result.score >= criterion.pass_threshold

    def test_score_below_threshold(self):
        """Test criterion fails when score below threshold."""
        criterion = FileExistsCriterion(path="test.txt", description="Test", pass_threshold=0.9)
        result = CriterionResult(
            criterion_type="file_exists",
            description="Test",
            score=0.89,  # Below threshold
        )

        assert result.score < criterion.pass_threshold

    def test_low_threshold_allows_partial_success(self):
        """Test that low threshold can pass with partial scores."""
        criterion = FileContainsCriterion(
            path="test.txt",
            includes=["A", "B"],
            description="Test",
            pass_threshold=0.5,  # Only need 50%
        )

        # Simulated result: only 1/2 includes found
        # includes: 0.5, excludes: 1.0, avg: 0.75
        result = CriterionResult(criterion_type="file_contains", description="Test", score=0.75)

        assert result.score >= criterion.pass_threshold

    def test_high_threshold_requires_near_perfect(self):
        """Test that high threshold requires near-perfect scores."""
        criterion = FileExistsCriterion(
            path="test.txt",
            description="Test",
            pass_threshold=0.99,  # Need 99%
        )

        # Good score but not enough
        result = CriterionResult(criterion_type="file_exists", description="Test", score=0.95)

        assert result.score < criterion.pass_threshold


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_zero_weight_total_handled(self):
        """Test weighted score when total weight is zero (shouldn't happen)."""
        result = create_test_evaluation_result(
            task_id="test",
            final_status="SUCCESS",
            success_criteria_results=[
                CriterionResult(criterion_type="file_exists", description="A", score=1.0),
            ],
            turns=[],
        )

        # This shouldn't happen in practice due to validation, but test defensive code
        # If we could somehow have zero weights, should return 0.0
        result.weighted_score = None
        result.calculate_weighted_score([])
        assert result.weighted_score == 0.0

    def test_criterion_with_minimum_valid_weight(self):
        """Test criterion with smallest valid positive weight."""
        criterion = FileExistsCriterion(
            path="test.txt",
            description="Test",
            weight=0.0001,  # Very small but valid
        )
        assert criterion.weight == 0.0001

    def test_criterion_with_large_weight(self):
        """Test criterion with very large weight."""
        criterion = FileExistsCriterion(
            path="test.txt",
            description="Test",
            weight=1000.0,  # Very large but valid
        )
        assert criterion.weight == 1000.0

    def test_pass_threshold_zero_always_passes(self):
        """Test that pass_threshold=0.0 allows any score to pass."""
        criterion = FileExistsCriterion(path="test.txt", description="Test", pass_threshold=0.0)

        # Even score of 0.0 should pass
        result = CriterionResult(criterion_type="file_exists", description="Test", score=0.0)

        assert result.score >= criterion.pass_threshold

    def test_pass_threshold_one_requires_perfect(self):
        """Test that pass_threshold=1.0 requires perfect score."""
        criterion = FileExistsCriterion(path="test.txt", description="Test", pass_threshold=1.0)

        # Only 1.0 passes
        result_perfect = CriterionResult(criterion_type="file_exists", description="Test", score=1.0)
        assert result_perfect.score >= criterion.pass_threshold

        # 0.9999 fails
        result_almost = CriterionResult(criterion_type="file_exists", description="Test", score=0.9999)
        assert result_almost.score < criterion.pass_threshold
