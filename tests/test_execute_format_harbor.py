"""``coder-eval execute --format harbor`` — writes a trajectory.json (ATIF) sibling for task.json."""

import json

import click
import pytest
from typer.testing import CliRunner

from coder_eval.cli import app
from coder_eval.orchestrator import Orchestrator
from tests.fixtures.mock_agent import MockAgent


runner = CliRunner()


@pytest.fixture
def success_task(tmp_path):
    task_content = """
task_id: "atif_format_harbor_test"
description: "execute --format harbor smoke test"
initial_prompt: "Create a file named 'test.txt'"
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
def mock_agent(monkeypatch):
    async def _mock_create_agent(self):
        return MockAgent(self.task, scenario="success")

    monkeypatch.setattr(Orchestrator, "_create_agent", _mock_create_agent)


def test_execute_format_harbor_writes_trajectory_json(tmp_path, success_task, mock_agent):
    run_dir = tmp_path / "run"
    result = runner.invoke(
        app,
        ["execute", str(success_task), "--run-dir", str(run_dir), "--format", "harbor"],
    )
    assert result.exit_code == 0, result.output

    task_jsons = sorted(run_dir.glob("**/task.json"))
    assert len(task_jsons) == 1
    trajectory_path = task_jsons[0].with_name("trajectory.json")
    assert trajectory_path.exists()

    trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
    assert trajectory["schema_version"].startswith("ATIF-v1.")
    assert len(trajectory["steps"]) >= 1


def test_execute_without_format_does_not_write_trajectory_json(tmp_path, success_task, mock_agent):
    run_dir = tmp_path / "run"
    result = runner.invoke(app, ["execute", str(success_task), "--run-dir", str(run_dir)])
    assert result.exit_code == 0, result.output

    task_jsons = sorted(run_dir.glob("**/task.json"))
    assert len(task_jsons) == 1
    assert not task_jsons[0].with_name("trajectory.json").exists()


def test_unknown_format_value_errors_cleanly(tmp_path, success_task):
    run_dir = tmp_path / "run"
    result = runner.invoke(
        app,
        ["execute", str(success_task), "--run-dir", str(run_dir), "--format", "nonsense"],
    )
    assert result.exit_code != 0
    # click.unstyle strips ANSI color codes -- CI renders Click's own error box
    # with color (option names highlighted char-by-char), which would otherwise
    # split "--format" across escape sequences and silently break this check.
    assert "Unsupported --format" in click.unstyle(result.output)
