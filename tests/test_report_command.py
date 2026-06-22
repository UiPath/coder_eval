"""CliRunner tests for the public `coder-eval report` command."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from typer.testing import CliRunner

from coder_eval.cli import app
from coder_eval.models import AgentKind, EvaluationResult, FinalStatus


runner = CliRunner()


def _write_task_json(run_dir: Path, task_id: str = "sample-task") -> Path:
    """Write a minimal valid task.json (serialized EvaluationResult) under run_dir."""
    result = EvaluationResult(
        task_id=task_id,
        task_description="Sample task for report-command test",
        variant_id="default",
        agent_type=AgentKind.CLAUDE_CODE,
        model_used="claude-sonnet-4-6",
        started_at=datetime(2026, 1, 1, 12, 0, 0),
        completed_at=datetime(2026, 1, 1, 12, 1, 0),
        duration_seconds=60.0,
        final_status=FinalStatus.SUCCESS,
        weighted_score=0.9,
        iteration_count=1,
        success_criteria_results=[],
        iterations=[],
    )
    task_dir = run_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    task_json = task_dir / "task.json"
    task_json.write_text(result.model_dump_json(), encoding="utf-8")
    return task_json


# --------------------------------------------------------------------------
# Markdown path
# --------------------------------------------------------------------------


def test_report_markdown_prints_to_stdout(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.md").write_text("# Run Report\n\nAll good.", encoding="utf-8")

    res = runner.invoke(app, ["report", str(run_dir)])
    assert res.exit_code == 0
    assert "Run Report" in res.stdout


def test_report_markdown_output_file_written(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.md").write_text("# Run Report\n\nDetails here.", encoding="utf-8")
    out = tmp_path / "summary.md"

    res = runner.invoke(app, ["report", str(run_dir), "--output", str(out)])
    assert res.exit_code == 0
    assert out.read_text(encoding="utf-8") == "# Run Report\n\nDetails here."


def test_report_markdown_missing_report_exits_1(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()  # no run.md / run.json

    res = runner.invoke(app, ["report", str(run_dir)])
    assert res.exit_code == 1
    assert "No report found" in res.stdout
    assert "Hint" in res.stdout


def test_report_unknown_format_exits_1(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    res = runner.invoke(app, ["report", str(run_dir), "--format", "xml"])
    assert res.exit_code == 1
    assert "unknown --format" in res.stdout


# --------------------------------------------------------------------------
# HTML path (_regenerate_html_reports)
# --------------------------------------------------------------------------


def test_report_html_no_task_json_exits_1(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    res = runner.invoke(app, ["report", str(run_dir), "--format", "html"])
    assert res.exit_code == 1
    assert "no task.json" in res.stdout


def test_report_html_all_valid_writes_reports(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_task_json(run_dir, "task-a")
    _write_task_json(run_dir, "task-b")

    res = runner.invoke(app, ["report", str(run_dir), "--format", "html"])
    assert res.exit_code == 0
    assert "Generated 2 HTML report(s)" in res.stdout
    assert (run_dir / "task-a" / "task.html").exists()
    assert (run_dir / "task-b" / "task.html").exists()


def test_report_html_single_output_writes_to_file(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_task_json(run_dir, "task-only")
    out = tmp_path / "report.html"

    res = runner.invoke(app, ["report", str(run_dir), "--format", "html", "--output", str(out)])
    assert res.exit_code == 0
    assert "Generated 1 HTML report(s)" in res.stdout
    assert out.exists()


def test_report_html_output_collision_exits_1(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_task_json(run_dir, "task-a")
    _write_task_json(run_dir, "task-b")
    out = tmp_path / "single.html"

    res = runner.invoke(app, ["report", str(run_dir), "--format", "html", "--output", str(out)])
    assert res.exit_code == 1
    assert "single file" in res.stdout


def test_report_html_malformed_task_json_skipped_and_fails(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_task_json(run_dir, "task-good")
    bad_dir = run_dir / "task-bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "task.json").write_text("{ not valid json", encoding="utf-8")

    res = runner.invoke(app, ["report", str(run_dir), "--format", "html"])
    # The good one is generated; the malformed one is skipped and counted failed → exit 1.
    assert res.exit_code == 1
    assert "Skipping" in res.stdout
    assert "1 task(s) failed" in res.stdout
    assert (run_dir / "task-good" / "task.html").exists()
