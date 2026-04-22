"""End-to-end integration tests for coder-eval CLI.

These tests invoke the complete CLI command and verify the entire
application lifecycle from command parsing through task execution
to report generation.

The tests use a MockAgent that dynamically creates files based on
success criteria, avoiding real API calls while testing the full
orchestration flow.
"""

import json

import pytest
from typer.testing import CliRunner

from coder_eval.cli import app
from coder_eval.orchestrator import Orchestrator
from tests.fixtures.mock_agent import MockAgent


# Create CliRunner instance for all tests
runner = CliRunner()


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def simple_success_task(tmp_path):
    """Create a simple task YAML that should succeed.

    The task requires creating a file named 'test.txt'. The MockAgent
    in success mode will dynamically create this file based on the
    file_exists criterion.

    Args:
        tmp_path: pytest temporary directory fixture

    Returns:
        Path to the created task YAML file
    """
    task_content = """
task_id: "integration_test_success"
description: "Simple integration test - success case"
initial_prompt: "Create a file named 'test.txt'"
max_iterations: 1

agent:
  type: "claude-code"
  permission_mode: "acceptEdits"

sandbox:
  driver: "tempdir"
  python: null

success_criteria:
  - type: "file_exists"
    path: "test.txt"
    description: "Test file must exist"
"""
    task_file = tmp_path / "task.yaml"
    task_file.write_text(task_content)
    return task_file


@pytest.fixture
def simple_failure_task(tmp_path):
    """Create a simple task YAML that will fail.

    The task requires creating a file, but MockAgent in failure mode
    won't create it, causing the task to fail.

    Args:
        tmp_path: pytest temporary directory fixture

    Returns:
        Path to the created task YAML file
    """
    task_content = """
task_id: "integration_test_failure"
description: "Simple integration test - failure case"
initial_prompt: "Create a file named 'test.txt'"
max_iterations: 1

agent:
  type: "claude-code"
  permission_mode: "acceptEdits"

sandbox:
  driver: "tempdir"
  python: null

success_criteria:
  - type: "file_exists"
    path: "test.txt"
    description: "Test file must exist"
"""
    task_file = tmp_path / "task.yaml"
    task_file.write_text(task_content)
    return task_file


@pytest.fixture
def mock_agent_success(monkeypatch):
    """Patch Orchestrator to use mock agent with success scenario.

    This replaces the real agent creation with our MockAgent,
    preventing actual API calls while testing the full flow.

    The MockAgent receives the full task definition so it can
    dynamically create files based on success criteria.

    Args:
        monkeypatch: pytest monkeypatch fixture
    """

    async def _mock_create_agent(self):
        # Return mock agent with full task definition for dynamic behavior
        return MockAgent(self.task, scenario="success")

    # Patch the Orchestrator's agent creation method
    monkeypatch.setattr(Orchestrator, "_create_agent", _mock_create_agent)


@pytest.fixture
def mock_agent_failure(monkeypatch):
    """Patch Orchestrator to use mock agent with failure scenario.

    Args:
        monkeypatch: pytest monkeypatch fixture
    """

    async def _mock_create_agent(self):
        return MockAgent(self.task, scenario="failure")

    monkeypatch.setattr(Orchestrator, "_create_agent", _mock_create_agent)


# ============================================================================
# Phase 2: First E2E Test
# ============================================================================


def test_cli_run_simple_success(tmp_path, simple_success_task, mock_agent_success):
    """Test successful task execution end-to-end.

    This test verifies the complete flow:
    1. CLI command invocation works
    2. Task execution completes successfully
    3. Run directory structure is created correctly
    4. experiment.json is generated with correct content
    5. Task-specific task.json is created
    6. experiment.md is generated

    Args:
        tmp_path: pytest temporary directory
        simple_success_task: fixture providing task YAML path
        mock_agent_success: fixture that patches agent creation
    """
    run_dir = tmp_path / "runs"

    # Invoke the CLI command
    result = runner.invoke(
        app,
        ["run", str(simple_success_task), "--run-dir", str(run_dir)],
    )

    # Verify command succeeded (CLI should always exit 0 unless catastrophic error)
    assert result.exit_code == 0, f"CLI command failed: {result.stdout}\n{result.stderr}"

    # Verify run directory was created (when --run-dir is provided, it's used directly)
    assert run_dir.exists(), "Run directory not created"

    # Verify experiment.json exists at run-level and has correct structure
    summary_file = run_dir / "experiment.json"
    assert summary_file.exists(), "experiment.json not created"

    # Parse and validate experiment result content (ExperimentResult schema)
    summary = json.loads(summary_file.read_text())
    assert "experiment_id" in summary, "Missing experiment_id field"
    assert "variant_aggregates" in summary, "Missing variant_aggregates field"
    assert "task_summaries" in summary, "Missing task_summaries field"
    # Check default variant aggregate shows 1 succeeded
    default_agg = summary["variant_aggregates"]["default"]
    assert default_agg["tasks_succeeded"] == 1, f"Expected 1 succeeded, got {default_agg['tasks_succeeded']}"
    assert default_agg["tasks_failed"] == 0, f"Expected 0 failed, got {default_agg['tasks_failed']}"

    # Verify task-specific directory and results
    # With experiment layer (default experiment), structure is:
    # run_dir / variant_id / {task_id} / NN (replicate index)
    task_dir = run_dir / "default" / "integration_test_success" / "00"
    assert task_dir.exists(), "Task directory not created"

    # Task results are saved as task.json
    report_json_file = task_dir / "task.json"
    assert report_json_file.exists(), "task.json not created"

    # Parse and verify task results
    task_result = json.loads(report_json_file.read_text())
    assert task_result["final_status"] == "SUCCESS", f"Expected SUCCESS status, got {task_result['final_status']}"

    # Verify experiment.md was generated at run-level
    run_report_file = run_dir / "experiment.md"
    assert run_report_file.exists(), "experiment.md not created"

    # Basic content check on report
    report_content = run_report_file.read_text()
    assert "integration_test_success" in report_content, "Task name not in report"
    assert len(report_content) > 100, "Report seems too short (< 100 chars)"


# ============================================================================
# Phase 3: Core Test Scenarios
# ============================================================================


def test_cli_run_simple_failure(tmp_path, simple_failure_task, mock_agent_failure):
    """Test task failure path (CLI exits non-zero on task failure).

    Verifies that:
    1. CLI exits with code 1 when a task fails
    2. Failure is properly recorded in experiment.json
    3. Task status is marked as FAILED
    4. Reports are still generated

    Args:
        tmp_path: pytest temporary directory
        simple_failure_task: fixture providing task YAML path
        mock_agent_failure: fixture that patches agent to fail
    """
    run_dir = tmp_path / "runs"

    result = runner.invoke(
        app,
        ["run", str(simple_failure_task), "--run-dir", str(run_dir)],
    )

    # CLI should exit 1 when a task fails
    assert result.exit_code == 1, f"CLI should exit 1 on task failure: {result.stdout}"

    # Verify run directory created (when --run-dir is provided, it's used directly)
    assert run_dir.exists(), "Run directory not created"

    # Verify experiment.json shows the failure at run-level
    summary_file = run_dir / "experiment.json"
    assert summary_file.exists(), "experiment.json not created"

    summary = json.loads(summary_file.read_text())
    # ExperimentResult schema: check variant aggregate
    default_agg = summary["variant_aggregates"]["default"]
    assert default_agg["tasks_succeeded"] == 0, f"Expected 0 succeeded, got {default_agg['tasks_succeeded']}"
    assert default_agg["tasks_failed"] == 1, f"Expected 1 failed, got {default_agg['tasks_failed']}"

    # Verify task status marked as FAILURE in the task's task.json
    # With experiment layer (default experiment), structure is:
    # run_dir / variant_id / {task_id} / NN (replicate index)
    task_dir = run_dir / "default" / "integration_test_failure" / "00"
    assert task_dir.exists(), "Task directory not created"

    report_json_file = task_dir / "task.json"
    assert report_json_file.exists(), "task.json not created"

    task_result = json.loads(report_json_file.read_text())
    assert task_result["final_status"] == "FAILURE", f"Expected FAILURE status, got {task_result['final_status']}"

    # Verify experiment.md still generated even for failures
    run_report_file = run_dir / "experiment.md"
    assert run_report_file.exists(), "experiment.md not created for failed task"


def test_cli_run_keyboard_interrupt(tmp_path, simple_success_task, monkeypatch):
    """Test that KeyboardInterrupt during execution exits with code 2.

    Verifies that Ctrl+C is caught and translated to exit code 2,
    following the pytest convention for interrupted execution.
    """
    import asyncio

    def _interrupted_run(coro, **kwargs):
        # Close the coroutine to avoid RuntimeWarning
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "run", _interrupted_run)

    result = runner.invoke(
        app,
        ["run", str(simple_success_task), "--run-dir", str(tmp_path / "runs")],
    )

    assert result.exit_code == 2, f"CLI should exit 2 on interrupt: {result.stdout}"
