"""Tests for reference comparison scoring: complexity baseline and division safety."""

from unittest.mock import MagicMock

import pytest

from coder_eval.sandbox import Sandbox


class TestComplexityBaseline:
    """Verify complexity comparison derives baseline from reference code."""

    def test_complexity_comparison_uses_reference_code(self, tmp_path):
        """Complexity comparison should derive baseline from reference code, not use empty dict."""
        pytest.importorskip("radon")
        from coder_eval.criteria.reference_comparison import ReferenceComparisonChecker
        from coder_eval.models import ReferenceComparisonCriterion

        agent_code = "def add(a, b):\n    return a + b\n"
        reference_code = "def add(a, b):\n    return a + b\n"

        sandbox = MagicMock(spec=Sandbox)
        sandbox.sandbox_dir = tmp_path
        agent_file = tmp_path / "solution.py"
        agent_file.write_text(agent_code)
        sandbox.file_exists.return_value = True
        # agent_file resolves through the shared path seam (glob expansion,
        # ignore filtering, exactly-one), not a direct sandbox_dir read.
        sandbox.get_file_content.return_value = agent_code

        criterion = ReferenceComparisonCriterion(
            description="Compare complexity",
            agent_file="solution.py",
            comparison_method="complexity",
        )

        checker = ReferenceComparisonChecker()
        result = checker._check_impl(criterion, sandbox, reference_code=reference_code)

        assert result.score > 0.0, "Score should be non-zero for valid code"
        assert result.score >= 0.3, f"Complexity score {result.score} seems wrong for identical code."


class TestComplexityDivisionByZero:
    """Verify ComplexityScorer handles zero baseline values without crashing."""

    def test_zero_cyclomatic_baseline_no_crash(self):
        """score_complexity should not crash when cyclomatic baseline is 0."""
        pytest.importorskip("radon")
        from coder_eval.scoring.complexity import ComplexityScorer

        scorer = ComplexityScorer()
        agent_code = "x = 1\n"
        baseline = {"cyclomatic": 0, "lines_of_code": 10, "function_count": 1}

        try:
            result = scorer.score_complexity(agent_code, "", baseline)
            assert 0.0 <= result["scores"]["cyclomatic_score"] <= 1.0
        except ZeroDivisionError:
            pytest.fail("ZeroDivisionError when cyclomatic baseline is 0")

    def test_zero_loc_baseline_no_crash(self):
        """score_complexity should not crash when lines_of_code baseline is 0."""
        pytest.importorskip("radon")
        from coder_eval.scoring.complexity import ComplexityScorer

        scorer = ComplexityScorer()
        agent_code = "x = 1\n"
        baseline = {"cyclomatic": 10, "lines_of_code": 0, "function_count": 1}

        try:
            result = scorer.score_complexity(agent_code, "", baseline)
            assert 0.0 <= result["scores"]["loc_score"] <= 1.0
        except ZeroDivisionError:
            pytest.fail("ZeroDivisionError when lines_of_code baseline is 0")
