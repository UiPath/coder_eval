"""Tests for run_command stdout matching (absorbed program_stdout_equals)."""

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from coder_eval.criteria.run_command import RunCommandChecker
from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import RunCommandCriterion, SandboxConfig, TaskDefinition
from coder_eval.sandbox import Sandbox


def _task_base() -> dict:
    return {
        "task_id": "test",
        "description": "test",
        "initial_prompt": "do something",
        "agent": {"type": "claude-code"},
        "sandbox": {"driver": "tempdir"},
    }


class TestRemovedCriteriaMigrationGuard:
    """Verify that removed criterion types produce helpful errors."""

    def test_program_stdout_equals_rejected(self):
        with pytest.raises(ValidationError, match=r"program_stdout_equals.*has been removed"):
            TaskDefinition(
                **_task_base(),
                success_criteria=[
                    {
                        "type": "program_stdout_equals",
                        "command": "echo hi",
                        "expected_output": "hi",
                        "description": "d",
                    }
                ],
            )

    def test_scored_command_rejected(self):
        with pytest.raises(ValidationError, match=r"scored_command.*has been removed"):
            TaskDefinition(
                **_task_base(),
                success_criteria=[{"type": "scored_command", "command": "echo 1", "description": "d"}],
            )

    def test_code_lints_rejected(self):
        with pytest.raises(ValidationError, match=r"code_lints.*has been removed"):
            TaskDefinition(
                **_task_base(),
                success_criteria=[{"type": "code_lints", "linter": "ruff", "description": "d"}],
            )

    def test_valid_criteria_not_rejected(self):
        task = TaskDefinition(
            **_task_base(),
            success_criteria=[
                {
                    "type": "run_command",
                    "command": "echo hi",
                    "expected_stdout": "hi",
                    "description": "d",
                }
            ],
        )
        assert len(task.success_criteria) == 1


class TestRunCommandStdoutModel:
    """Verify RunCommandCriterion model defaults for new stdout fields."""

    def test_defaults(self):
        c = RunCommandCriterion(command="echo hi", description="d")
        assert c.expected_stdout is None
        assert c.stdout_match == "exact"
        assert c.expected_exit_code == 0
        assert c.timeout == 30

    def test_score_from_stdout_default_false(self):
        c = RunCommandCriterion(command="echo hi", description="d")
        assert c.score_from_stdout is False

    def test_score_from_stdout_alone_valid(self):
        c = RunCommandCriterion(command="echo 0.5", description="d", score_from_stdout=True)
        assert c.score_from_stdout is True

    def test_score_from_stdout_and_expected_stdout_mutually_exclusive(self):
        with pytest.raises(ValidationError, match="mutually exclusive"):
            RunCommandCriterion(
                command="echo 0.5",
                description="d",
                score_from_stdout=True,
                expected_stdout="something",
            )

    def test_with_stdout_fields(self):
        c = RunCommandCriterion(
            command="echo hi",
            description="d",
            expected_stdout="hi",
            stdout_match="contains",
        )
        assert c.expected_stdout == "hi"
        assert c.stdout_match == "contains"


class TestRunCommandStdoutMatching:
    """Unit tests for stdout matching with mocked sandbox."""

    def _sandbox(self, exit_code: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
        s = MagicMock(spec=Sandbox)
        s.run_command.return_value = (exit_code, stdout, stderr)
        return s

    def test_no_stdout_check(self):
        """Without expected_stdout, only exit code matters."""
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="echo hi", description="d")
        result = checker._check_impl(c, self._sandbox(0, "hi"))
        assert result.score == 1.0

    def test_exact_match_pass(self):
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="echo hi", description="d", expected_stdout="hi")
        result = checker._check_impl(c, self._sandbox(0, "hi\n"))
        assert result.score == 1.0

    def test_exact_match_fail(self):
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="echo hi", description="d", expected_stdout="hello")
        result = checker._check_impl(c, self._sandbox(0, "hi\n"))
        assert result.score == 0.0
        assert "FAIL" in result.details

    def test_contains_match_pass(self):
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="cmd", description="d", expected_stdout="world", stdout_match="contains")
        result = checker._check_impl(c, self._sandbox(0, "hello world foo"))
        assert result.score == 1.0

    def test_contains_match_fail(self):
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="cmd", description="d", expected_stdout="bar", stdout_match="contains")
        result = checker._check_impl(c, self._sandbox(0, "hello world foo"))
        assert result.score == 0.0

    def test_regex_match_pass(self):
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="cmd", description="d", expected_stdout=r"\d{3}", stdout_match="regex")
        result = checker._check_impl(c, self._sandbox(0, "code 200 ok"))
        assert result.score == 1.0

    def test_regex_match_fail(self):
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="cmd", description="d", expected_stdout=r"\d{5}", stdout_match="regex")
        result = checker._check_impl(c, self._sandbox(0, "code 200 ok"))
        assert result.score == 0.0

    def test_exit_code_fail_with_stdout_match(self):
        """Both exit code and stdout must pass."""
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="cmd", description="d", expected_stdout="hi")
        result = checker._check_impl(c, self._sandbox(1, "hi"))
        assert result.score == 0.0

    def test_exit_code_pass_stdout_fail(self):
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="cmd", description="d", expected_stdout="bye")
        result = checker._check_impl(c, self._sandbox(0, "hi"))
        assert result.score == 0.0

    def test_invalid_regex_returns_zero(self):
        """Invalid regex pattern should score 0, not raise an exception."""
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="cmd", description="d", expected_stdout="[invalid(", stdout_match="regex")
        result = checker._check_impl(c, self._sandbox(0, "anything"))
        assert result.score == 0.0

    def test_match_stdout_regex_preserves_whitespace_in_pattern(self):
        """Regex mode must not strip the authored pattern — whitespace is meaningful."""
        assert RunCommandChecker._match_stdout("  hello  ", r"^\s*hello\s*$", "regex") is True
        # actual does NOT have leading spaces, so this pattern that requires
        # them must fail (it would pass today if actual and expected were stripped).
        assert RunCommandChecker._match_stdout("hello", r"^   hello$", "regex") is False

    def test_match_stdout_exact_still_strips_both_sides(self):
        """exact still strips — preserving today's forgiving behaviour."""
        assert RunCommandChecker._match_stdout("  hi\n", "hi", "exact") is True

    def test_match_stdout_contains_still_strips_both_sides(self):
        """contains still strips — preserving today's forgiving behaviour."""
        assert RunCommandChecker._match_stdout("  hello world  ", "hello", "contains") is True


class TestRunCommandDetailsSurfacing:
    """Details must surface command, exit code, and both streams — even when
    stdout or stderr is empty — so reviewers can debug failures from the HTML
    report without diving back into raw logs."""

    def _sandbox(self, exit_code: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
        s = MagicMock(spec=Sandbox)
        s.run_command.return_value = (exit_code, stdout, stderr)
        return s

    def test_failure_details_include_command_exit_and_both_streams(self):
        """A non-zero exit must always include the command + labeled streams."""
        checker = RunCommandChecker()
        c = RunCommandCriterion(
            command="uip rpa get-errors --project-dir .",
            description="get-errors",
            expected_exit_code=0,
        )
        result = checker._check_impl(c, self._sandbox(1, "", "Project not found: .\n"))
        assert result.score == 0.0
        details = result.details or ""
        assert "Command: uip rpa get-errors --project-dir ." in details
        assert "Exit code: 1 (expected: 0)" in details
        assert "Stdout: (empty)" in details
        assert "Stderr:" in details
        assert "Project not found" in details

    def test_success_details_include_command_and_both_streams(self):
        """On success we still emit the full exec block so the report shows
        what was actually run."""
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="echo hello", description="d")
        result = checker._check_impl(c, self._sandbox(0, "hello\n"))
        assert result.score == 1.0
        details = result.details or ""
        assert "Command: echo hello" in details
        assert "Exit code: 0 (expected: 0)" in details
        assert "Stdout:\nhello" in details
        assert "Stderr: (empty)" in details

    def test_long_stream_is_truncated_with_marker(self):
        """Output longer than the per-stream budget is truncated with a
        visible marker so the HTML stays bounded."""
        checker = RunCommandChecker()
        big = "x" * 9000
        c = RunCommandCriterion(command="cmd", description="d")
        result = checker._check_impl(c, self._sandbox(1, big, ""))
        details = result.details or ""
        # Budget is 4000 chars — the full 9000 must not appear verbatim.
        assert "x" * 9000 not in details
        assert "more chars truncated" in details

    def test_score_from_stdout_failure_includes_stdout_and_stderr(self):
        """The score_from_stdout failure path also surfaces captured output."""
        checker = RunCommandChecker()
        c = RunCommandCriterion(
            command="pytest --tb=short -q",
            description="tests",
            score_from_stdout=True,
        )
        result = checker._check_impl(c, self._sandbox(2, "", "ImportError: no module named foo"))
        assert result.score == 0.0
        details = result.details or ""
        assert "Command: pytest --tb=short -q" in details
        assert "Exit code: 2" in details
        assert "ImportError" in details


class TestScoreFromStdout:
    """Unit tests for the score_from_stdout path."""

    def _sandbox(self, exit_code: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
        s = MagicMock(spec=Sandbox)
        s.run_command.return_value = (exit_code, stdout, stderr)
        return s

    def test_valid_score_1_0(self):
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="echo 1.0", description="d", score_from_stdout=True)
        result = checker._check_impl(c, self._sandbox(0, "1.0\n"))
        assert result.score == 1.0

    def test_valid_score_0_75(self):
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="echo 0.75", description="d", score_from_stdout=True)
        result = checker._check_impl(c, self._sandbox(0, "0.75\n"))
        assert result.score == 0.75

    def test_valid_score_0_0(self):
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="echo 0.0", description="d", score_from_stdout=True)
        result = checker._check_impl(c, self._sandbox(0, "0.0\n"))
        assert result.score == 0.0

    def test_nonzero_exit_returns_0(self):
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="fail", description="d", score_from_stdout=True)
        result = checker._check_impl(c, self._sandbox(1, "0.9\n", "error msg"))
        assert result.score == 0.0
        assert result.error is not None
        assert "exit" in result.error.lower() or "code" in result.error.lower()

    def test_custom_expected_exit_code_respected(self):
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="cmd", description="d", score_from_stdout=True, expected_exit_code=1)
        result = checker._check_impl(c, self._sandbox(1, "0.8\n"))
        assert result.score == 0.8

    def test_empty_stdout_returns_0(self):
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="cmd", description="d", score_from_stdout=True)
        result = checker._check_impl(c, self._sandbox(0, ""))
        assert result.score == 0.0
        assert result.error is not None

    def test_non_numeric_returns_0(self):
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="cmd", description="d", score_from_stdout=True)
        result = checker._check_impl(c, self._sandbox(0, "not a number\n"))
        assert result.score == 0.0
        assert result.error is not None

    def test_nan_returns_0(self):
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="cmd", description="d", score_from_stdout=True)
        result = checker._check_impl(c, self._sandbox(0, "nan\n"))
        assert result.score == 0.0
        assert result.error is not None

    def test_inf_returns_0(self):
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="cmd", description="d", score_from_stdout=True)
        result = checker._check_impl(c, self._sandbox(0, "inf\n"))
        assert result.score == 0.0
        assert result.error is not None

    def test_score_above_1_clamped(self):
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="cmd", description="d", score_from_stdout=True)
        result = checker._check_impl(c, self._sandbox(0, "1.5\n"))
        assert result.score == 1.0
        assert "clamped" in result.details

    def test_score_below_0_clamped(self):
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="cmd", description="d", score_from_stdout=True)
        result = checker._check_impl(c, self._sandbox(0, "-0.3\n"))
        assert result.score == 0.0
        assert "clamped" in result.details

    def test_remaining_lines_in_details(self):
        checker = RunCommandChecker()
        c = RunCommandCriterion(command="cmd", description="d", score_from_stdout=True)
        result = checker._check_impl(c, self._sandbox(0, "0.8\nline2\nline3\n"))
        assert result.score == 0.8
        assert "line2" in result.details
        assert "line3" in result.details


class TestRunCommandStdoutIntegration:
    """Integration tests with real sandbox."""

    def test_exact_stdout_match(self):
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id="test_rc_stdout")
        sandbox.setup()

        criterion = RunCommandCriterion(
            command="python -c \"print('Hello, World!')\"",
            expected_stdout="Hello, World!",
            stdout_match="exact",
            description="exact match",
        )
        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 1.0
        sandbox.cleanup(preserve=False)

    def test_contains_stdout_match(self):
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id="test_rc_contains")
        sandbox.setup()

        criterion = RunCommandCriterion(
            command="python -c \"print('hello world')\"",
            expected_stdout="world",
            stdout_match="contains",
            description="contains match",
        )
        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 1.0
        sandbox.cleanup(preserve=False)

    def test_score_from_stdout(self):
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id="test_rc_score")
        sandbox.setup()

        criterion = RunCommandCriterion(
            command='python -c "print(0.75)"',
            score_from_stdout=True,
            description="score from stdout",
        )
        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 0.75
        sandbox.cleanup(preserve=False)

    def test_regex_stdout_match(self):
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id="test_rc_regex")
        sandbox.setup()

        criterion = RunCommandCriterion(
            command="python -c \"print('version 3.13.0')\"",
            expected_stdout=r"version \d+\.\d+\.\d+",
            stdout_match="regex",
            description="regex match",
        )
        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 1.0
        sandbox.cleanup(preserve=False)
