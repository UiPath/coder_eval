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


class TestRunCommandStdoutIntegration:
    """Integration tests with real sandbox."""

    def test_exact_stdout_match(self):
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id="test_rc_stdout")
        sandbox.setup()

        criterion = RunCommandCriterion(
            command="echo 'Hello, World!'",
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
            command="echo 'hello world'",
            expected_stdout="world",
            stdout_match="contains",
            description="contains match",
        )
        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 1.0
        sandbox.cleanup(preserve=False)

    def test_regex_stdout_match(self):
        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = Sandbox(config, task_id="test_rc_regex")
        sandbox.setup()

        criterion = RunCommandCriterion(
            command="echo 'version 3.13.0'",
            expected_stdout=r"version \d+\.\d+\.\d+",
            stdout_match="regex",
            description="regex match",
        )
        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 1.0
        sandbox.cleanup(preserve=False)
