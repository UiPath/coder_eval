"""``coder-eval evaluate --format harbor`` — grade a workdir using ATIF trajectory context.

End-to-end: build an ATIF trajectory.json by hand (the shape a Harbor agent's
``coder-eval execute --format harbor`` would produce), drop a file in a plain
workdir (standing in for what the Harbor agent's container left behind), and
grade it with both a workdir-only criterion (``file_exists``) and a
trajectory-dependent one (``command_executed``) to prove hydration actually
feeds the checker.
"""

import json

import click
from typer.testing import CliRunner

from coder_eval.cli import app
from coder_eval.harbor.atif_emit import evaluation_result_to_trajectory
from coder_eval.models import CommandTelemetry, EvaluationResult, FinalStatus, TurnRecord


runner = CliRunner()

_TASK_YAML = """
task_id: "atif_format_harbor_evaluate_test"
description: "evaluate --format harbor smoke test"
initial_prompt: "unused"
agent:
  type: "claude-code"

sandbox:
  driver: "tempdir"
  python: null

success_criteria:
  - type: "file_exists"
    path: "test.txt"
    description: "Test file must exist"
  - type: "command_executed"
    tool_name: "Bash"
    command_pattern: "touch test.txt"
    min_count: 1
    description: "The agent must have run touch"
"""


def _write_trajectory(tmp_path):
    turn = TurnRecord(
        iteration=1,
        user_input="create test.txt",
        agent_output="done",
        commands=[
            CommandTelemetry(
                tool_name="Bash",
                tool_id="toolu_1",
                timestamp="2026-09-10T00:00:00Z",
                parameters={"command": "touch test.txt"},
                result_status="success",
                result_summary="",
                assistant_turn_index=0,
                sequence_number=0,
            )
        ],
    )
    result = EvaluationResult(
        task_id="atif_format_harbor_evaluate_test",
        task_description="d",
        agent_type="harbor-agent",
        started_at="2026-09-10T00:00:00Z",
        final_status=FinalStatus.NOT_GRADED,
        iteration_count=1,
        iterations=[turn],
    )
    trajectory = evaluation_result_to_trajectory(result)
    assert trajectory is not None
    path = tmp_path / "trajectory.json"
    path.write_text(trajectory.model_dump_json(exclude_none=True), encoding="utf-8")
    return path


def test_evaluate_format_harbor_grades_with_hydrated_trajectory(tmp_path):
    task_file = tmp_path / "task.yaml"
    task_file.write_text(_TASK_YAML, encoding="utf-8")

    work_dir = tmp_path / "workdir"
    work_dir.mkdir()
    (work_dir / "test.txt").write_text("hi", encoding="utf-8")

    trajectory_path = _write_trajectory(tmp_path)
    run_dir = tmp_path / "run"

    result = runner.invoke(
        app,
        [
            "evaluate",
            str(task_file),
            str(work_dir),
            "--format",
            "harbor",
            "--trajectory",
            str(trajectory_path),
            "--run-dir",
            str(run_dir),
        ],
    )
    assert result.exit_code == 0, result.output

    task_jsons = sorted(run_dir.glob("**/task.json"))
    assert len(task_jsons) == 1
    graded = json.loads(task_jsons[0].read_text(encoding="utf-8"))
    assert graded["weighted_score"] == 1.0
    scores = {r["criterion_type"]: r["score"] for r in graded["success_criteria_results"]}
    assert scores["file_exists"] == 1.0
    assert scores["command_executed"] == 1.0


def test_evaluate_format_harbor_requires_trajectory(tmp_path):
    task_file = tmp_path / "task.yaml"
    task_file.write_text(_TASK_YAML, encoding="utf-8")
    work_dir = tmp_path / "workdir"
    work_dir.mkdir()

    result = runner.invoke(app, ["evaluate", str(task_file), str(work_dir), "--format", "harbor"])
    assert result.exit_code != 0
    # click.unstyle strips ANSI color codes -- see test_execute_format_harbor.py's
    # equivalent assertion for why a plain substring check is not portable here.
    assert "--trajectory" in click.unstyle(result.output)
