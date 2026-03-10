"""Tests for FileMatchesRegexCriterion."""

import re

from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import FileMatchesRegexCriterion, SandboxConfig
from coder_eval.sandbox import Sandbox


class TestFileMatchesRegexCriterion:
    """Tests for FileMatchesRegexCriterion."""

    def test_regex_match_found(self, tmp_path):
        """Test that pattern is found in file."""
        # Setup
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id="test_regex_match")
        sandbox_dir = sandbox.setup()

        # Create test file with specific pattern
        test_file = sandbox_dir / "test.py"
        test_file.write_text("async def my_function():\n    pass\n")

        # Create criterion
        criterion = FileMatchesRegexCriterion(
            description="Check for async function",
            path="test.py",
            pattern=r"async def \w+\(",
        )

        # Execute check
        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        # Verify
        assert result.score == 1.0
        assert "async def my_function(" in result.details
        assert result.error is None

        # Cleanup
        sandbox.cleanup(preserve=False)

    def test_regex_match_not_found(self, tmp_path):
        """Test that pattern is not found in file."""
        # Setup
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id="test_regex_no_match")
        sandbox_dir = sandbox.setup()

        # Create test file without the pattern
        test_file = sandbox_dir / "test.py"
        test_file.write_text("def my_function():\n    pass\n")

        # Create criterion
        criterion = FileMatchesRegexCriterion(
            description="Check for async function",
            path="test.py",
            pattern=r"async def \w+\(",
        )

        # Execute check
        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        # Verify
        assert result.score == 0.0
        assert "not found" in result.details
        assert result.error is None

        # Cleanup
        sandbox.cleanup(preserve=False)

    def test_regex_must_not_match(self, tmp_path):
        """Test must_match=False - pattern should NOT be present."""
        # Setup
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id="test_regex_must_not_match")
        sandbox_dir = sandbox.setup()

        # Create test file without the forbidden pattern
        test_file = sandbox_dir / "test.py"
        test_file.write_text("x = 5\nprint(x)\n")

        # Create criterion - should NOT contain TODO
        criterion = FileMatchesRegexCriterion(
            description="No TODOs allowed",
            path="test.py",
            pattern=r"TODO",
            must_match=False,
        )

        # Execute check
        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        # Verify
        assert result.score == 1.0
        assert "correctly absent" in result.details

        # Cleanup
        sandbox.cleanup(preserve=False)

    def test_regex_must_not_match_fails(self, tmp_path):
        """Test must_match=False fails when pattern is found."""
        # Setup
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id="test_regex_must_not_match_fail")
        sandbox_dir = sandbox.setup()

        # Create test file WITH the forbidden pattern
        test_file = sandbox_dir / "test.py"
        test_file.write_text("# TODO: fix this\nx = 5\n")

        # Create criterion - should NOT contain TODO
        criterion = FileMatchesRegexCriterion(
            description="No TODOs allowed",
            path="test.py",
            pattern=r"TODO",
            must_match=False,
        )

        # Execute check
        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        # Verify
        assert result.score == 0.0
        assert "should not be present" in result.details
        assert "TODO" in result.details

        # Cleanup
        sandbox.cleanup(preserve=False)

    def test_regex_with_flags(self, tmp_path):
        """Test regex with flags (case insensitive)."""
        # Setup
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id="test_regex_flags")
        sandbox_dir = sandbox.setup()

        # Create test file with lowercase
        test_file = sandbox_dir / "test.txt"
        test_file.write_text("hello world\n")

        # Create criterion with IGNORECASE flag
        criterion = FileMatchesRegexCriterion(
            description="Check for HELLO (case insensitive)",
            path="test.txt",
            pattern=r"HELLO",
            flags=re.IGNORECASE,
        )

        # Execute check
        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        # Verify
        assert result.score == 1.0
        assert "hello" in result.details

        # Cleanup
        sandbox.cleanup(preserve=False)

    def test_regex_file_not_found(self, tmp_path):
        """Test that check fails if file doesn't exist."""
        # Setup
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id="test_regex_no_file")
        sandbox.setup()

        # Create criterion for non-existent file
        criterion = FileMatchesRegexCriterion(
            description="Check non-existent file",
            path="nonexistent.py",
            pattern=r"test",
        )

        # Execute check
        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        # Verify
        assert result.score == 0.0
        assert "does not exist" in result.error

        # Cleanup
        sandbox.cleanup(preserve=False)

    def test_regex_invalid_pattern(self, tmp_path):
        """Test that invalid regex pattern is handled."""
        # Setup
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id="test_regex_invalid")
        sandbox_dir = sandbox.setup()

        # Create test file
        test_file = sandbox_dir / "test.txt"
        test_file.write_text("some text\n")

        # Create criterion with invalid regex
        criterion = FileMatchesRegexCriterion(
            description="Invalid regex pattern",
            path="test.txt",
            pattern=r"[invalid(regex",  # Invalid pattern
        )

        # Execute check
        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        # Verify
        assert result.score == 0.0
        assert "Invalid regex pattern" in result.error

        # Cleanup
        sandbox.cleanup(preserve=False)
