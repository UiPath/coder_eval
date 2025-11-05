"""Tests for PylintScoreCriterion."""

from unittest.mock import Mock

import pytest

from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import PylintScoreCriterion


class MockSandbox:
    """Mock sandbox for testing without actual command execution."""

    def __init__(self, command_results=None):
        """Initialize mock sandbox.

        Args:
            command_results: Dict mapping command strings to (exit_code, stdout, stderr) tuples
        """
        self.command_results = command_results or {}
        self.sandbox_dir = None

    def run_command(self, command, timeout=None):
        """Mock run_command that returns pre-configured results."""
        # Try exact match first
        if command in self.command_results:
            return self.command_results[command]

        # Try to find a match based on command prefix
        for cmd_key, result in self.command_results.items():
            if command.startswith(cmd_key):
                return result

        # Default: command not found
        return (127, "", "command not found")


class TestPylintScoreCriterion:
    """Test suite for PylintScoreCriterion evaluation."""

    def test_pylint_score_perfect(self):
        """Test pylint with perfect 10/10 score."""
        sandbox = MockSandbox(command_results={"pylint": (0, "Your code has been rated at 10.00/10", "")})

        criterion = PylintScoreCriterion(path="perfect.py", pass_threshold=0.90, description="Perfect code")

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 1.0  # 10.0/10 normalized
        assert "10.00/10" in result.details
        assert result.error is None

    def test_pylint_score_passing(self):
        """Test pylint with score above threshold."""
        sandbox = MockSandbox(
            command_results={"pylint": (0, "Your code has been rated at 8.75/10 (previous run: 8.50/10, +0.25)", "")}
        )

        criterion = PylintScoreCriterion(path="src/", pass_threshold=0.85, description="Code quality check")

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 0.875  # 8.75/10
        assert "8.75/10" in result.details
        assert result.error is None

    def test_pylint_score_failing(self):
        """Test pylint with score below threshold."""
        sandbox = MockSandbox(
            command_results={"pylint": (16, "Your code has been rated at 6.25/10", "")}  # Pylint exit code for issues
        )

        criterion = PylintScoreCriterion(path="src/", pass_threshold=0.75, description="Minimum quality gate")

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 0.625  # 6.25/10
        assert "6.25/10" in result.details
        # Check against threshold happens in orchestrator, not here

    def test_pylint_score_with_min_score(self):
        """Test min_score parameter takes precedence over pass_threshold."""
        sandbox = MockSandbox(command_results={"pylint": (0, "Your code has been rated at 8.0/10", "")})

        criterion = PylintScoreCriterion(
            path="src/",
            pass_threshold=0.70,  # Should be ignored in details
            min_score=8.5,  # This should appear in details
            description="Test min_score",
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 0.80  # 8.0/10
        assert "min_score=8.5/10" in result.details

    def test_pylint_score_not_installed(self):
        """Test error when pylint not installed."""
        sandbox = MockSandbox(
            command_results={"pylint": (127, "", "bash: pylint: command not found")}  # Command not found
        )

        criterion = PylintScoreCriterion(path="src/", pass_threshold=0.80, description="Should fail gracefully")

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 0.0
        assert result.error is not None
        assert "Could not parse" in result.error or "failed" in result.error.lower()

    def test_pylint_score_timeout(self):
        """Test pylint timeout handling."""

        def timeout_command(command, timeout):
            raise TimeoutError(f"Command timed out after {timeout}s")

        sandbox = Mock()
        sandbox.run_command = timeout_command

        criterion = PylintScoreCriterion(
            path="huge_project/", timeout=5, pass_threshold=0.80, description="Large codebase"
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 0.0
        assert result.error is not None
        assert "timeout" in result.error.lower() or "timed out" in result.error.lower()

    def test_pylint_score_with_args(self):
        """Test pylint with additional arguments."""
        sandbox = MockSandbox(
            command_results={
                "pylint src/ --disable=C0111 --max-line-length=120": (
                    0,
                    "Your code has been rated at 9.50/10",
                    "",
                )
            }
        )

        criterion = PylintScoreCriterion(
            path="src/",
            args=["--disable=C0111", "--max-line-length=120"],
            pass_threshold=0.90,
            description="Custom pylint config",
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 0.95
        assert "9.50/10" in result.details

    def test_pylint_score_with_rcfile(self):
        """Test pylint with custom rcfile."""
        sandbox = MockSandbox(
            command_results={"pylint src/ --rcfile .pylintrc": (0, "Your code has been rated at 8.00/10", "")}
        )

        criterion = PylintScoreCriterion(
            path="src/", rcfile=".pylintrc", pass_threshold=0.75, description="Project-specific config"
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 0.80
        assert "8.00/10" in result.details

    def test_pylint_score_with_fail_under(self):
        """Test pylint with --fail-under flag."""
        sandbox = MockSandbox(
            command_results={"pylint src/ --fail-under 8.0": (0, "Your code has been rated at 8.50/10", "")}
        )

        criterion = PylintScoreCriterion(
            path="src/", fail_under=8.0, pass_threshold=0.75, description="Test fail-under"
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 0.85
        assert "8.50/10" in result.details

    def test_pylint_score_parsing_variants(self):
        """Test parsing of different pylint output formats."""
        test_cases = [
            # (output, expected_score)
            ("Your code has been rated at 7.50/10", 0.75),
            ("Your code has been rated at 10.00/10 (previous run: 9.50/10, +0.50)", 1.0),
            ("Your code has been rated at 0.00/10", 0.0),
            ("Your code has been rated at 5.23/10", 0.523),
            ("Your code has been rated at 9.99/10", 0.999),
        ]

        for output, expected in test_cases:
            sandbox = MockSandbox(command_results={"pylint": (0, output, "")})

            criterion = PylintScoreCriterion(path="src/", pass_threshold=0.50, description="Test parsing")

            checker = SuccessChecker(sandbox)
            result = checker.check(criterion)

            assert abs(result.score - expected) < 0.001, f"Failed for: {output}"

    def test_pylint_score_zero_score(self):
        """Test pylint with 0/10 score (syntax errors)."""
        sandbox = MockSandbox(
            command_results={
                "pylint": (
                    32,  # Pylint error exit code
                    "Your code has been rated at 0.00/10",
                    "",
                )
            }
        )

        criterion = PylintScoreCriterion(path="broken.py", pass_threshold=0.50, description="Broken code")

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 0.0
        assert result.error is None  # Not an error, just a low score

    def test_pylint_negative_score_clamped(self):
        """Test that negative scores are parsed and clamped to 0.0."""
        # Note: Negative pylint scores are extremely rare but theoretically possible
        # Issue 3 fix: regex now supports negative scores with (-?\d+(?:\.\d+)?)
        sandbox = MockSandbox(command_results={"pylint": (32, "Your code has been rated at -2.50/10", "")})

        criterion = PylintScoreCriterion(path="terrible.py", pass_threshold=0.50, description="Very bad code")

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        # Negative scores are parsed correctly and clamped to 0.0 in normalized score
        assert result.score == 0.0
        assert result.error is None  # Not an error, just parsed and clamped
        assert "-2.50/10" in result.details

    def test_pylint_unparseable_output(self):
        """Test error when pylint output cannot be parsed."""
        sandbox = MockSandbox(command_results={"pylint": (1, "Some random output without a score", "Error messages")})

        criterion = PylintScoreCriterion(path="src/", pass_threshold=0.80, description="Should error")

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 0.0
        assert result.error is not None
        assert "Could not parse" in result.error

    def test_pylint_score_with_truncated_output(self):
        """Test that long output is truncated."""
        # Create output longer than 500 chars
        long_details = "X" * 600
        output = f"Your code has been rated at 7.5/10\n{long_details}"

        sandbox = MockSandbox(command_results={"pylint": (0, output, "")})

        criterion = PylintScoreCriterion(path="src/", pass_threshold=0.70, description="Long output")

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 0.75
        assert "truncated" in result.details.lower()

    def test_pylint_score_combined_stdout_stderr(self):
        """Test that both stdout and stderr are checked for score."""
        # Sometimes pylint writes to stderr
        sandbox = MockSandbox(command_results={"pylint": (0, "", "Your code has been rated at 8.5/10")})

        criterion = PylintScoreCriterion(path="src/", pass_threshold=0.80, description="Stderr output")

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 0.85
        assert "8.5/10" in result.details

    def test_pylint_score_details_format(self):
        """Test that details contain all expected information."""
        sandbox = MockSandbox(command_results={"pylint": (0, "Your code has been rated at 8.75/10", "")})

        criterion = PylintScoreCriterion(path="src/", pass_threshold=0.85, description="Check details format")

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        # Check all expected components are in details
        assert "Pylint score: 8.75/10" in result.details
        assert "normalized: 0.875" in result.details
        assert "Threshold: pass_threshold=0.85" in result.details
        assert "Exit code: 0" in result.details
        assert "Your code has been rated" in result.details

    def test_pylint_score_model_validation(self):
        """Test Pydantic validation of PylintScoreCriterion fields."""
        from pydantic import ValidationError

        # Valid criterion - Issue 8 fix: min_score overrides pass_threshold via validator
        criterion = PylintScoreCriterion(path="src/", min_score=8.5, pass_threshold=0.90, description="Valid")
        assert criterion.min_score == 8.5
        assert criterion.pass_threshold == 0.85  # Normalized from min_score (8.5/10)

        # Invalid min_score (> 10.0)
        with pytest.raises(ValidationError):
            PylintScoreCriterion(path="src/", min_score=11.0, description="Invalid min_score")

        # Note: Negative min_score is now ALLOWED (Issue 8 fix) - no validation error

        # Invalid fail_under (> 10.0)
        with pytest.raises(ValidationError):
            PylintScoreCriterion(path="src/", fail_under=10.5, description="Invalid fail_under")
