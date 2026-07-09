"""Tests for UiPathEvalCriterion."""

import json

from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import UiPathEvalCriterion


class MockSandbox:
    """Mock sandbox for testing."""

    def __init__(self):
        self.sandbox_dir = None
        self.task_dir = None
        self._files = {}
        self._default_file_content = None  # Default content for any uipath_eval output file

    def run_command(self, cmd: str) -> tuple[int, str, str]:
        """Mock run_command."""
        # Allow injection of command results via _command_result
        if hasattr(self, "_command_result"):
            return self._command_result
        return (0, "", "")

    def get_file_content(self, path: str) -> str:
        """Mock get_file_content."""
        # Check if this is a uipath_eval output file and we have default content set
        if "uipath_eval_output_" in path and self._default_file_content:
            return self._default_file_content

        if path not in self._files:
            raise FileNotFoundError(f"File not found: {path}")
        return self._files[path]

    def set_file_content(self, path: str, content: str) -> None:
        """Helper to set file content.

        If path is 'output.json', store as default for uipath_eval output files.
        """
        if path == "output.json":
            self._default_file_content = content
        else:
            self._files[path] = content

    def set_command_result(self, exit_code: int, stdout: str, stderr: str) -> None:
        """Helper to set command result."""
        self._command_result = (exit_code, stdout, stderr)


class TestUiPathEvalCriterion:
    """Test suite for UiPathEvalCriterion."""

    def test_all_thresholds_met(self):
        """Test when all metric thresholds are met."""
        sandbox = MockSandbox()
        sandbox.set_command_result(0, "", "")
        sandbox.set_file_content(
            "output.json",
            json.dumps(
                {
                    "success_rate": 0.95,
                    "avg_time": 3.0,
                    "completion_count": 100,
                }
            ),
        )

        criterion = UiPathEvalCriterion(
            description="Verify UiPath eval metrics",
            agent_name="test_agent",
            eval_set="test_eval",
            thresholds={
                "success_rate": 0.90,
                "avg_time": 2.5,
                "completion_count": 50,
            },
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 1.0
        assert "All thresholds met" in result.details
        assert result.error is None

    def test_some_thresholds_failed(self):
        """Test when some metrics fail thresholds."""
        sandbox = MockSandbox()
        sandbox.set_command_result(0, "", "")
        sandbox.set_file_content(
            "output.json",
            json.dumps(
                {
                    "success_rate": 0.85,
                    "avg_time": 5.0,
                    "completion_count": 100,
                }
            ),
        )

        criterion = UiPathEvalCriterion(
            description="Verify UiPath eval metrics",
            agent_name="test_agent",
            eval_set="test_eval",
            thresholds={
                "success_rate": 0.90,
                "avg_time": 3.0,
                "completion_count": 50,
            },
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        # 2 out of 3: avg_time 5.0 >= 3.0 (pass), completion_count 100 >= 50 (pass), success_rate 0.85 < 0.9 (fail)
        assert result.score == 2.0 / 3.0
        assert "Failed metrics" in result.details
        assert "success_rate: 0.85 < 0.9" in result.details

    def test_all_thresholds_failed(self):
        """Test when all metrics fail thresholds."""
        sandbox = MockSandbox()
        sandbox.set_command_result(0, "", "")
        sandbox.set_file_content(
            "output.json",
            json.dumps(
                {
                    "success_rate": 0.5,
                    "accuracy": 0.3,
                    "completion_count": 10,
                }
            ),
        )

        criterion = UiPathEvalCriterion(
            description="Verify UiPath eval metrics",
            agent_name="test_agent",
            eval_set="test_eval",
            thresholds={
                "success_rate": 0.90,
                "accuracy": 0.80,
                "completion_count": 100,
            },
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 0.0
        assert "Failed metrics" in result.details

    def test_command_failure(self):
        """Test when eval command fails."""
        sandbox = MockSandbox()
        sandbox.set_command_result(1, "", "Command failed: Agent not found")

        criterion = UiPathEvalCriterion(
            description="Verify UiPath eval metrics",
            agent_name="nonexistent_agent",
            eval_set="test_eval",
            thresholds={"success_rate": 0.90},
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 0.0
        assert "Command failed with exit code 1" in result.details

    def test_command_failure_uipath_cli_missing_appends_hint(self):
        """When stderr signals a missing in-sandbox `uipath` CLI, details append the hint."""
        sandbox = MockSandbox()
        sandbox.set_command_result(127, "", "/bin/sh: uipath: command not found")

        criterion = UiPathEvalCriterion(
            description="Verify UiPath eval metrics",
            agent_name="any",
            eval_set="test_eval",
            thresholds={"success_rate": 0.90},
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 0.0
        assert "could not resolve the `uipath` CLI" in result.details
        # The hint mentions the in-sandbox source of truth, not the host extra.
        assert "in-sandbox dependency" in result.details

    def test_command_failure_module_not_found_appends_hint(self):
        """A 'No module named uipath' error also triggers the hint."""
        sandbox = MockSandbox()
        sandbox.set_command_result(1, "", "Traceback: ModuleNotFoundError: No module named 'uipath'")

        criterion = UiPathEvalCriterion(
            description="Verify UiPath eval metrics",
            agent_name="any",
            eval_set="test_eval",
            thresholds={"success_rate": 0.90},
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 0.0
        assert "could not resolve the `uipath` CLI" in result.details

    def test_command_failure_exit_code_127_appends_hint_even_with_empty_stderr(self):
        """Exit code 127 alone is enough — some shells produce empty stderr on command-not-found."""
        sandbox = MockSandbox()
        sandbox.set_command_result(127, "", "")

        criterion = UiPathEvalCriterion(
            description="Verify UiPath eval metrics",
            agent_name="any",
            eval_set="test_eval",
            thresholds={"success_rate": 0.90},
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 0.0
        assert "could not resolve the `uipath` CLI" in result.details

    def test_command_failure_unrelated_stderr_does_not_append_hint(self):
        """Arbitrary stderr (no CLI-missing signature, non-trigger exit code) must NOT get the hint.

        Regression guard for the prior substring-only heuristic, which would have
        matched if a task's stderr happened to contain the trigger phrases for
        unrelated reasons.
        """
        sandbox = MockSandbox()
        sandbox.set_command_result(1, "", "TypeError: cannot compare 'int' and 'str'")

        criterion = UiPathEvalCriterion(
            description="Verify UiPath eval metrics",
            agent_name="any",
            eval_set="test_eval",
            thresholds={"success_rate": 0.90},
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 0.0
        assert "could not resolve the `uipath` CLI" not in result.details

    def test_invalid_json_output(self):
        """Test when output.json contains invalid JSON."""
        sandbox = MockSandbox()
        sandbox.set_command_result(0, "", "")
        sandbox.set_file_content("output.json", "{ invalid json")

        criterion = UiPathEvalCriterion(
            description="Verify UiPath eval metrics",
            agent_name="test_agent",
            eval_set="test_eval",
            thresholds={"success_rate": 0.90},
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 0.0
        assert "Failed to parse" in result.details
        assert ".json" in result.details

    def test_missing_output_file(self):
        """Test when output.json does not exist."""
        sandbox = MockSandbox()
        sandbox.set_command_result(0, "", "")

        criterion = UiPathEvalCriterion(
            description="Verify UiPath eval metrics",
            agent_name="test_agent",
            eval_set="test_eval",
            thresholds={"success_rate": 0.90},
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 0.0
        assert "Failed to parse" in result.details
        assert ".json" in result.details

    def test_missing_metric_in_output(self):
        """Test when expected metric is missing from output."""
        sandbox = MockSandbox()
        sandbox.set_command_result(0, "", "")
        sandbox.set_file_content(
            "output.json",
            json.dumps(
                {
                    "success_rate": 0.95,
                    "completion_count": 100,
                }
            ),
        )

        criterion = UiPathEvalCriterion(
            description="Verify UiPath eval metrics",
            agent_name="test_agent",
            eval_set="test_eval",
            thresholds={
                "success_rate": 0.90,
                "avg_time": 3.0,  # Missing from output
                "completion_count": 50,
            },
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 2.0 / 3.0
        assert "Missing metric: avg_time" in result.details

    def test_empty_thresholds(self):
        """Test when no thresholds are specified."""
        sandbox = MockSandbox()
        sandbox.set_command_result(0, "", "")
        sandbox.set_file_content("output.json", json.dumps({"any_metric": 0.5}))

        criterion = UiPathEvalCriterion(
            description="Verify UiPath eval metrics",
            agent_name="test_agent",
            eval_set="test_eval",
            thresholds={},
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 1.0
        assert "All thresholds met" in result.details

    def test_non_numeric_threshold_value(self):
        """Test when output contains non-numeric metric value."""
        sandbox = MockSandbox()
        sandbox.set_command_result(0, "", "")
        sandbox.set_file_content(
            "output.json",
            json.dumps(
                {
                    "success_rate": "N/A",
                    "completion_count": 100,
                }
            ),
        )

        criterion = UiPathEvalCriterion(
            description="Verify UiPath eval metrics",
            agent_name="test_agent",
            eval_set="test_eval",
            thresholds={
                "success_rate": 0.90,
                "completion_count": 50,
            },
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        # Non-numeric value should fail explicitly (not silently pass)
        # 1 out of 2 metrics passed (only completion_count)
        assert result.score == 0.5
        assert "Invalid type" in result.details
        assert "success_rate" in result.details

    def test_integer_threshold_comparison(self):
        """Test threshold comparison with integer values."""
        sandbox = MockSandbox()
        sandbox.set_command_result(0, "", "")
        sandbox.set_file_content(
            "output.json",
            json.dumps(
                {
                    "tests_passed": 95,
                    "tests_total": 100,
                }
            ),
        )

        criterion = UiPathEvalCriterion(
            description="Verify test results",
            agent_name="test_agent",
            eval_set="test_eval",
            thresholds={
                "tests_passed": 90,
                "tests_total": 100,
            },
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 1.0
        assert "All thresholds met" in result.details

    def test_command_with_agent_and_eval_set_args(self):
        """Test that command is built correctly with agent_name and eval_set."""
        import re

        sandbox = MockSandbox()

        # Mock run_command to capture the command string
        original_run = sandbox.run_command
        captured_cmd = []

        def capture_cmd(cmd: str):
            captured_cmd.append(cmd)
            # Extract output filename from command
            match = re.search(r"--output-file\s+(\S+)", cmd)
            if match:
                output_filename = match.group(1)
                sandbox.set_file_content(output_filename, json.dumps({"metric": 0.95}))
            return original_run(cmd)

        sandbox.run_command = capture_cmd

        criterion = UiPathEvalCriterion(
            description="Verify UiPath eval metrics",
            agent_name="my_agent",
            eval_set="production",
            thresholds={"metric": 0.90},
        )

        checker = SuccessChecker(sandbox)
        checker.check(criterion)

        assert len(captured_cmd) == 1
        assert "my_agent" in captured_cmd[0]
        assert "production" in captured_cmd[0]
        assert "--no-report" in captured_cmd[0]
        assert "--output-file" in captured_cmd[0]
        assert "uipath_eval_output_" in captured_cmd[0]
        assert ".json" in captured_cmd[0]

    def test_partial_threshold_passing(self):
        """Test scoring when multiple thresholds are checked."""
        sandbox = MockSandbox()
        sandbox.set_command_result(0, "", "")
        sandbox.set_file_content(
            "output.json",
            json.dumps(
                {
                    "metric1": 0.95,
                    "metric2": 0.5,
                    "metric3": 0.85,
                    "metric4": 0.92,
                    "metric5": 0.45,
                }
            ),
        )

        criterion = UiPathEvalCriterion(
            description="Verify multiple metrics",
            agent_name="test_agent",
            eval_set="test_eval",
            thresholds={
                "metric1": 0.90,  # Pass
                "metric2": 0.80,  # Fail
                "metric3": 0.80,  # Pass
                "metric4": 0.90,  # Pass
                "metric5": 0.80,  # Fail
            },
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        # 3 out of 5 metrics passed
        assert abs(result.score - 0.6) < 0.01
        assert "metric2: 0.5 < 0.8" in result.details
        assert "metric5: 0.45 < 0.8" in result.details

    def test_non_dict_json_output(self):
        """Test when output.json contains a JSON array instead of an object."""
        sandbox = MockSandbox()
        sandbox.set_command_result(0, "", "")
        sandbox.set_file_content("output.json", "[1, 2, 3]")

        criterion = UiPathEvalCriterion(
            description="Verify UiPath eval metrics",
            agent_name="test_agent",
            eval_set="test_eval",
            thresholds={"success_rate": 0.90},
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        assert result.score == 0.0
        assert "Expected JSON object" in result.details
        assert "list" in result.details

    def test_string_score_in_evaluations_treated_as_zero(self):
        """Test that non-numeric scores in evaluationSetResults are treated as 0.0."""
        sandbox = MockSandbox()
        sandbox.set_command_result(0, "", "")
        sandbox.set_file_content(
            "output.json",
            json.dumps(
                {
                    "evaluationSetResults": [
                        {
                            "evaluationRunResults": [
                                {"evaluatorId": "accuracy", "result": {"score": "excellent"}},
                                {"evaluatorId": "accuracy", "result": {"score": 1.0}},
                            ]
                        }
                    ]
                }
            ),
        )

        criterion = UiPathEvalCriterion(
            description="Verify UiPath eval metrics",
            agent_name="test_agent",
            eval_set="test_eval",
            thresholds={"accuracy": 0.4},
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion)

        # "excellent" -> 0.0, 1.0 -> 1.0; avg = 0.5 >= 0.4 threshold
        assert result.score == 1.0
        assert "All thresholds met" in result.details
