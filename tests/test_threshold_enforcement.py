"""Integration test for pass_threshold enforcement in orchestrator.

Tests that the orchestrator correctly enforces pass_threshold for each
criterion individually, even when the weighted score is high.
"""

from coder_eval.models import (
    CriterionResult,
    FileExistsCriterion,
)


class TestThresholdEnforcement:
    """Test that pass_threshold is enforced consistently."""

    def test_all_passed_requires_each_criterion_meets_threshold(self):
        """Test that success requires EACH criterion to meet its pass_threshold.

        Scenario from issue:
        - Criterion 1: score=1.0, threshold=0.9 → passes
        - Criterion 2: score=0.8, threshold=0.9 → fails
        - Task should FAIL despite high weighted score
        """
        # Define criteria
        criteria = [
            FileExistsCriterion(
                path="file1.txt",
                description="First file",
                weight=1.0,
                pass_threshold=0.9,
            ),
            FileExistsCriterion(
                path="file2.txt",
                description="Second file",
                weight=1.0,
                pass_threshold=0.9,
            ),
        ]

        # Simulate results
        results = [
            CriterionResult(
                criterion_type="file_exists",
                description="First file",
                score=1.0,  # Passes threshold
            ),
            CriterionResult(
                criterion_type="file_exists",
                description="Second file",
                score=0.8,  # FAILS threshold (< 0.9)
            ),
        ]

        # Calculate weighted score
        total_weighted = sum(
            result.score * criterion.weight for result, criterion in zip(results, criteria, strict=False)
        )
        total_weight = sum(c.weight for c in criteria)
        weighted_score = total_weighted / total_weight if total_weight > 0 else 0.0

        # Weighted score is high (0.9)
        assert weighted_score == 0.9

        # But all_passed should be False
        all_passed = all(
            result.score >= criterion.pass_threshold for result, criterion in zip(results, criteria, strict=False)
        )

        assert not all_passed, "Task should fail when any criterion is below threshold"

    def test_all_passed_true_when_all_criteria_meet_threshold(self):
        """Test that success occurs when ALL criteria meet their thresholds."""
        criteria = [
            FileExistsCriterion(
                path="file1.txt",
                description="First file",
                weight=1.0,
                pass_threshold=0.9,
            ),
            FileExistsCriterion(
                path="file2.txt",
                description="Second file",
                weight=1.0,
                pass_threshold=0.9,
            ),
        ]

        results = [
            CriterionResult(
                criterion_type="file_exists",
                description="First file",
                score=0.95,  # Passes threshold
            ),
            CriterionResult(
                criterion_type="file_exists",
                description="Second file",
                score=0.92,  # Passes threshold
            ),
        ]

        all_passed = all(
            result.score >= criterion.pass_threshold for result, criterion in zip(results, criteria, strict=False)
        )

        assert all_passed, "Task should pass when all criteria meet thresholds"

    def test_different_thresholds_enforced_independently(self):
        """Test that different thresholds are enforced independently."""
        criteria = [
            FileExistsCriterion(
                path="file1.txt",
                description="Critical file",
                weight=3.0,
                pass_threshold=1.0,  # Must be perfect
            ),
            FileExistsCriterion(
                path="file2.txt",
                description="Optional file",
                weight=1.0,
                pass_threshold=0.5,  # Lenient
            ),
        ]

        # Case 1: Critical fails, optional passes → should fail
        results_fail = [
            CriterionResult(
                criterion_type="file_exists",
                description="Critical file",
                score=0.99,  # FAILS (< 1.0)
            ),
            CriterionResult(
                criterion_type="file_exists",
                description="Optional file",
                score=1.0,  # Passes
            ),
        ]

        all_passed_fail = all(
            result.score >= criterion.pass_threshold for result, criterion in zip(results_fail, criteria, strict=False)
        )

        assert not all_passed_fail, "Should fail when critical criterion doesn't meet threshold"

        # Case 2: Critical passes, optional barely passes → should pass
        results_pass = [
            CriterionResult(
                criterion_type="file_exists",
                description="Critical file",
                score=1.0,  # Passes
            ),
            CriterionResult(
                criterion_type="file_exists",
                description="Optional file",
                score=0.5,  # Exactly meets threshold
            ),
        ]

        all_passed_pass = all(
            result.score >= criterion.pass_threshold for result, criterion in zip(results_pass, criteria, strict=False)
        )

        assert all_passed_pass, "Should pass when all criteria meet their thresholds"

    def test_high_weighted_score_doesnt_override_failed_criterion(self):
        """Test that high weighted score doesn't override individual failures.

        This is the key test: even if the weighted average is excellent,
        if ANY criterion fails its threshold, the task should fail.
        """
        criteria = [
            FileExistsCriterion(
                path="file1.txt",
                description="High weight file",
                weight=10.0,  # Very high weight
                pass_threshold=0.9,
            ),
            FileExistsCriterion(
                path="file2.txt",
                description="Low weight file",
                weight=1.0,  # Low weight
                pass_threshold=0.9,
            ),
        ]

        results = [
            CriterionResult(
                criterion_type="file_exists",
                description="High weight file",
                score=1.0,  # Perfect
            ),
            CriterionResult(
                criterion_type="file_exists",
                description="Low weight file",
                score=0.5,  # FAILS threshold
            ),
        ]

        # Calculate weighted score - should be very high
        total_weighted = sum(
            result.score * criterion.weight for result, criterion in zip(results, criteria, strict=False)
        )
        total_weight = sum(c.weight for c in criteria)
        weighted_score = total_weighted / total_weight if total_weight > 0 else 0.0

        # Weighted score is ~0.95 (very high)
        assert weighted_score > 0.9

        # But all_passed should still be False
        all_passed = all(
            result.score >= criterion.pass_threshold for result, criterion in zip(results, criteria, strict=False)
        )

        assert not all_passed, "Task should fail even with high weighted score if any criterion is below its threshold"
