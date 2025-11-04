"""Tests for evaluator reference code support."""

from coder_eval.evaluator import LLMReviewer, SuccessChecker
from coder_eval.models import (
    LLMReviewerConfig,
    ReferenceComparisonCriterion,
    SandboxConfig,
)
from coder_eval.sandbox import Sandbox


class TestSuccessCheckerReference:
    """Tests for SuccessChecker with reference code."""

    def test_check_all_accepts_reference_code(self, tmp_path):
        """check_all accepts reference_code parameter."""
        config = SandboxConfig(driver="tempdir")
        sandbox = Sandbox(config, "test")
        sandbox.setup()

        checker = SuccessChecker(sandbox)
        reference_code = "def foo(): pass"

        # Should not raise
        results = checker.check_all([], reference_code=reference_code)
        assert results == []

        sandbox.cleanup(preserve=False)

    def test_reference_comparison_without_reference(self, tmp_path):
        """reference_comparison fails without reference code."""
        config = SandboxConfig(driver="tempdir")
        sandbox = Sandbox(config, "test")
        sandbox.setup()

        # Create a dummy file
        (sandbox.sandbox_dir / "solution.py").write_text("def foo(): pass")

        criterion = ReferenceComparisonCriterion(
            description="Compare against reference",
            agent_file="solution.py",
        )

        checker = SuccessChecker(sandbox)
        # Don't provide reference_code
        result = checker.check(criterion)

        assert result.score == 0.0
        assert "No reference code provided" in result.error

        sandbox.cleanup(preserve=False)

    def test_reference_comparison_with_reference(self, tmp_path):
        """reference_comparison succeeds with reference code."""
        config = SandboxConfig(driver="tempdir")
        sandbox = Sandbox(config, "test")
        sandbox.setup()

        # Create agent file with similar code
        reference_code = "def hello():\n    return 'world'"
        agent_code = "def hello():\n    return 'world'"
        (sandbox.sandbox_dir / "solution.py").write_text(agent_code)

        criterion = ReferenceComparisonCriterion(
            description="Compare against reference",
            agent_file="solution.py",
            comparison_method="token",
        )

        checker = SuccessChecker(sandbox)
        # Provide reference_code via check_all
        checker._reference_code = reference_code
        result = checker.check(criterion)

        # Identical code should score 1.0
        assert result.score == 1.0
        assert result.error is None

        sandbox.cleanup(preserve=False)

    def test_reference_comparison_agent_file_missing(self, tmp_path):
        """reference_comparison fails when agent file doesn't exist."""
        config = SandboxConfig(driver="tempdir")
        sandbox = Sandbox(config, "test")
        sandbox.setup()

        reference_code = "def foo(): pass"

        criterion = ReferenceComparisonCriterion(
            description="Compare against reference",
            agent_file="nonexistent.py",
        )

        checker = SuccessChecker(sandbox)
        checker._reference_code = reference_code
        result = checker.check(criterion)

        assert result.score == 0.0
        assert "Agent file not found" in result.error

        sandbox.cleanup(preserve=False)

    def test_reference_comparison_ast_method(self, tmp_path):
        """reference_comparison with ast method."""
        config = SandboxConfig(driver="tempdir")
        sandbox = Sandbox(config, "test")
        sandbox.setup()

        reference_code = "def foo():\n    return 42"
        # Slightly different but structurally similar
        agent_code = "def foo():\n    return 42"
        (sandbox.sandbox_dir / "solution.py").write_text(agent_code)

        criterion = ReferenceComparisonCriterion(
            description="Compare against reference",
            agent_file="solution.py",
            comparison_method="ast",
        )

        checker = SuccessChecker(sandbox)
        checker._reference_code = reference_code
        result = checker.check(criterion)

        # Should succeed with high similarity
        assert result.score > 0.8
        assert "ast" in result.details

        sandbox.cleanup(preserve=False)


class TestLLMReviewerReference:
    """Tests for LLMReviewer with reference solution."""

    def test_review_accepts_reference_solution(self):
        """review accepts reference_solution parameter."""
        config = LLMReviewerConfig(enabled=False)
        reviewer = LLMReviewer(config)

        # Should not raise (disabled reviewer returns None)
        result = reviewer.review(
            task_description="Test task",
            agent_output="Agent output",
            current_iteration=1,
            max_iterations=3,
            reference_solution="def foo(): pass",
        )

        assert result is None

    def test_build_prompt_without_reference(self):
        """_build_review_prompt works without reference."""
        config = LLMReviewerConfig(enabled=False)
        reviewer = LLMReviewer(config)

        prompt = reviewer._build_review_prompt(
            task_description="Test task",
            agent_output="Agent output",
            current_iteration=1,
            max_iterations=3,
            reference_solution=None,
        )

        assert "Test task" in prompt
        assert "Agent output" in prompt
        assert "REFERENCE SOLUTION" not in prompt

    def test_build_prompt_with_reference(self):
        """_build_review_prompt includes reference when provided."""
        config = LLMReviewerConfig(enabled=False)
        reviewer = LLMReviewer(config)

        reference_code = "def foo():\n    return 'bar'"
        prompt = reviewer._build_review_prompt(
            task_description="Test task",
            agent_output="Agent output",
            current_iteration=1,
            max_iterations=3,
            reference_solution=reference_code,
        )

        assert "Test task" in prompt
        assert "Agent output" in prompt
        assert "REFERENCE SOLUTION" in prompt
        assert reference_code in prompt
        assert "NOT visible to the agent" in prompt
