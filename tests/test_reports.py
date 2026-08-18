"""Tests for report generation module."""

import os
from datetime import datetime

import pytest

from coder_eval.models import RunSummary
from coder_eval.reports import ReportGenerator
from tests._fixtures.report_snapshots import assert_matches_snapshot


_SKIP_NO_SYMLINK = pytest.mark.skipif(
    os.name == "nt",
    reason="Symlink creation on Windows requires admin or Developer Mode; not asserted in CI.",
)


def _make_task_result(
    task_id: str,
    status: str,
    weighted_score: float | None,
    duration: float,
    iteration_count: int | None = None,
    turns: list[dict] | None = None,
    reference_similarity: float | None = None,
    model_used: str | None = None,
    agent_config: dict | None = None,
    sdk_options: dict | None = None,
    installed_tools: dict[str, str] | None = None,
) -> dict:
    """Helper to create a task_result dict with all expected fields."""
    return {
        "task_id": task_id,
        "status": status,
        "weighted_score": weighted_score,
        "duration": duration,
        "iteration_count": iteration_count,
        "iterations": turns or [],
        "reference_similarity": reference_similarity,
        "model_used": model_used,
        "agent_config": agent_config,
        "sdk_options": sdk_options,
        "installed_tools": installed_tools,
    }


def test_generate_markdown_snapshot_full():
    """Byte-identical characterization snapshot exercising EVERY dynamic column and
    optional section of generate_markdown — the safety net for its decomposition.

    Triggers: multiple distinct models (Models line), all P0 metrics (scores,
    latency, assistant turns, crashed partials, ground-truth similarity), all four
    dynamic table columns (model/tags/similarity/cmd-efficiency incl. expected/actual),
    run-time notes (max_turns_exhausted + expected_turns_overage), generation metrics,
    token usage (incl. cache + cost), agent settings, installed tools, and a non-empty
    environment block. All inputs are fixed values so the output is deterministic.
    """
    summary = RunSummary(
        run_id="2025-10-11_12-00-00",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 5, 30),
        total_duration_seconds=330.0,
        tasks_run=2,
        tasks_succeeded=1,
        tasks_failed=1,
        tasks_error=0,
        tasks_token_budget_exceeded=1,
        tasks_cost_budget_exceeded=0,
        task_results=[
            {
                "task_id": "alpha",
                "status": "success",
                "weighted_score": 0.95,
                "duration": 12.5,
                "model_used": "claude-haiku-4-5",
                "tags": ["smoke", "fast"],
                "reference_similarity": 0.88,
                "commands_efficiency": 0.75,
                "expected_commands": 4,
                "actual_commands": 6,
                "max_turns_exhausted": True,
                "iterations": [
                    {"iteration": 1, "crashed": False, "assistant_turn_count": 3, "duration_seconds": 4.2},
                ],
                "total_tokens": 1500,
                "input_tokens": 1000,
                "output_tokens": 500,
                "cache_creation_input_tokens": 200,
                "cache_read_input_tokens": 300,
                "total_cost_usd": 0.0123,
                "installed_tools": {"node": "20.1.0"},
                "sdk_options": {"model": "claude-haiku-4-5", "max_turns": 30},
                "agent_config": None,
            },
            {
                "task_id": "beta",
                "status": "failure",
                "weighted_score": 0.30,
                "duration": 8.0,
                "model_used": "claude-sonnet-4-6",
                "tags": ["regression"],
                "reference_similarity": 0.55,
                "commands_efficiency": 1.0,
                "expected_commands": 3,
                "actual_commands": 3,
                "expected_turns_overage": [10, 5],
                "iterations": [
                    {"iteration": 1, "crashed": True, "assistant_turn_count": 1, "duration_seconds": 2.0},
                ],
                "total_tokens": 800,
                "input_tokens": 600,
                "output_tokens": 200,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "total_cost_usd": 0.0077,
                "installed_tools": None,
                "sdk_options": None,
                "agent_config": None,
            },
        ],
        framework_version="0.1.0",
        environment_info={"python": "3.13.11", "platform": "darwin"},
    )

    assert_matches_snapshot(ReportGenerator.generate_markdown(summary), "run_full.md")


def test_generate_markdown_snapshot_minimal():
    """Zero-task summary: header + empty summary + table header/separator with no rows,
    no notes, no optional sections, environment block. Guards the empty-input path."""
    summary = RunSummary(
        run_id="2025-10-11_00-00-00",
        start_time=datetime(2025, 10, 11, 0, 0, 0),
        end_time=datetime(2025, 10, 11, 0, 0, 0),
        total_duration_seconds=0.0,
        tasks_run=0,
        tasks_succeeded=0,
        tasks_failed=0,
        tasks_error=0,
        task_results=[],
        framework_version="0.1.0",
        environment_info={"python": "3.13.11"},
    )

    assert_matches_snapshot(ReportGenerator.generate_markdown(summary), "run_minimal.md")


def test_generate_markdown_basic():
    """Test basic markdown generation with P0 metrics."""
    summary = RunSummary(
        run_id="2025-10-11_12-00-00",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 5, 30),
        total_duration_seconds=330.0,
        tasks_run=3,
        tasks_succeeded=2,
        tasks_failed=1,
        tasks_error=0,
        task_results=[
            _make_task_result(
                "task1",
                "SUCCESS",
                1.0,
                120.5,
                iteration_count=1,
                turns=[{"iteration": 1, "duration_seconds": 120.5, "command_count": 5}],
                reference_similarity=0.85,
            ),
            _make_task_result(
                "task2",
                "SUCCESS",
                0.95,
                100.3,
                iteration_count=2,
                turns=[
                    {"iteration": 1, "duration_seconds": 60.0, "command_count": 3},
                    {"iteration": 2, "duration_seconds": 40.3, "command_count": 4},
                ],
                reference_similarity=0.72,
            ),
            _make_task_result(
                "task3",
                "FAILED",
                0.5,
                109.2,
                iteration_count=3,
                turns=[
                    {"iteration": 1, "duration_seconds": 35.0, "command_count": 2},
                    {"iteration": 2, "duration_seconds": 38.0, "command_count": 3},
                    {"iteration": 3, "duration_seconds": 36.2, "command_count": 3},
                ],
            ),
        ],
        framework_version="0.1.0",
        environment_info={
            "coder_eval": "0.1.0",
            "python": "3.13.3",
            "uv": "0.8.17",
        },
    )

    report_md = ReportGenerator.generate_markdown(summary)

    # Check report structure
    assert "# Evaluation Run Report" in report_md
    assert "2025-10-11_12-00-00" in report_md
    assert "330.00s" in report_md

    # Check summary section
    assert "Total Tasks**: 3" in report_md
    assert "Succeeded**: 2" in report_md
    assert "Failed**: 1" in report_md
    assert "Errors**: 0" in report_md
    assert "Pass Rate**: 66.7% (2/3)" in report_md
    # No errors in this run, so no error-share line to explain the denominator.
    assert "Error Share" not in report_md

    # Check P0 aggregate metrics
    assert "Avg Reliability Score**:" in report_md
    assert "Avg Generation Latency**:" in report_md
    assert "Avg Ground Truth Similarity**:" in report_md

    # Check task details table has new columns
    assert "Reliability Score" in report_md
    assert "Similarity" in report_md  # Column header since task1/task2 have similarity

    # Check task rows
    assert "task1" in report_md
    assert "task2" in report_md
    assert "task3" in report_md
    assert "1.000" in report_md  # weighted_score for task1
    assert "0.950" in report_md  # weighted_score for task2
    assert "0.500" in report_md  # weighted_score for task3

    # Check Generation Metrics section
    assert "## Generation Metrics" in report_md

    # Check environment section
    assert "coder_eval**: 0.1.0" in report_md
    assert "python**: 3.13.3" in report_md
    assert "uv**: 0.8.17" in report_md


def test_generate_markdown_empty_tasks():
    """Test markdown generation with no tasks."""
    summary = RunSummary(
        run_id="2025-10-11_12-00-00",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 0, 5),
        total_duration_seconds=5.0,
        tasks_run=0,
        tasks_succeeded=0,
        tasks_failed=0,
        tasks_error=0,
        task_results=[],
        framework_version="0.1.0",
        environment_info={},
    )

    report_md = ReportGenerator.generate_markdown(summary)

    assert "Total Tasks**: 0" in report_md
    # A run with no tasks has no pass rate; 0.0% would read as "everything failed".
    assert "Pass Rate**: n/a (0/0)" in report_md
    # No aggregate metrics when there are no tasks
    assert "Avg Reliability Score" not in report_md
    assert "Generation Metrics" not in report_md


def test_generate_markdown_with_null_scores():
    """Test markdown generation with tasks that have null weighted_score."""
    summary = RunSummary(
        run_id="2025-10-11_12-00-00",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 1, 0),
        total_duration_seconds=60.0,
        tasks_run=1,
        tasks_succeeded=0,
        tasks_failed=0,
        tasks_error=1,
        task_results=[
            _make_task_result("task_error", "ERROR", None, 60.0),
        ],
        framework_version="0.1.0",
        environment_info={},
    )

    report_md = ReportGenerator.generate_markdown(summary)

    assert "task_error" in report_md
    assert "ERROR" in report_md
    assert "N/A" in report_md  # Should show N/A for null weighted_score


def test_generate_markdown_no_similarity_column():
    """Test that Similarity column is omitted when no tasks have reference_similarity."""
    summary = RunSummary(
        run_id="test-run",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 1, 0),
        total_duration_seconds=60.0,
        tasks_run=1,
        tasks_succeeded=1,
        tasks_failed=0,
        tasks_error=0,
        task_results=[
            _make_task_result("task1", "SUCCESS", 0.9, 30.0, iteration_count=1),
        ],
        framework_version="0.1.0",
        environment_info={},
    )

    report_md = ReportGenerator.generate_markdown(summary)

    # Table should not have Similarity column
    # Check the table header line doesn't include Similarity
    lines = report_md.split("\n")
    header_lines = [line for line in lines if line.startswith("| Task ID")]
    assert len(header_lines) == 1
    assert "Similarity" not in header_lines[0]

    # Summary should not show Avg Ground Truth Similarity
    assert "Avg Ground Truth Similarity" not in report_md


def test_generate_markdown_with_similarity_column():
    """Test that Similarity column appears when at least one task has reference_similarity."""
    summary = RunSummary(
        run_id="test-run",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 1, 0),
        total_duration_seconds=60.0,
        tasks_run=2,
        tasks_succeeded=2,
        tasks_failed=0,
        tasks_error=0,
        task_results=[
            _make_task_result("task1", "SUCCESS", 0.9, 30.0, iteration_count=1, reference_similarity=0.85),
            _make_task_result("task2", "SUCCESS", 0.8, 25.0, iteration_count=2),
        ],
        framework_version="0.1.0",
        environment_info={},
    )

    report_md = ReportGenerator.generate_markdown(summary)

    # Table should have Similarity column
    lines = report_md.split("\n")
    header_lines = [line for line in lines if line.startswith("| Task ID")]
    assert len(header_lines) == 1
    assert "Similarity" in header_lines[0]

    # task2 should show N/A for similarity
    assert "0.850" in report_md
    assert "Avg Ground Truth Similarity**: 0.850" in report_md


def test_generate_markdown_generation_metrics_section():
    """Test Generation Metrics section shows per-task breakdown."""
    summary = RunSummary(
        run_id="test-run",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 2, 0),
        total_duration_seconds=120.0,
        tasks_run=2,
        tasks_succeeded=2,
        tasks_failed=0,
        tasks_error=0,
        task_results=[
            _make_task_result(
                "task1",
                "SUCCESS",
                1.0,
                50.0,
                iteration_count=1,
                turns=[{"iteration": 1, "duration_seconds": 50.0, "command_count": 5}],
            ),
            _make_task_result(
                "task2",
                "SUCCESS",
                0.9,
                70.0,
                iteration_count=3,
                turns=[
                    {"iteration": 1, "duration_seconds": 25.0, "command_count": 3},
                    {"iteration": 2, "duration_seconds": 22.0, "command_count": 4},
                    {"iteration": 3, "duration_seconds": 23.0, "command_count": 3},
                ],
            ),
        ],
        framework_version="0.1.0",
        environment_info={},
    )

    report_md = ReportGenerator.generate_markdown(summary)

    assert "## Generation Metrics" in report_md
    # task1: 1 turn, 0 asst turns (no assistant_turn_count in test data)
    assert "| task1 | 50.0s | 1 | 0 | 50.0s |" in report_md
    # task2: 3 turns, avg turn = (25+22+23)/3 = 23.3s
    assert "| task2 | 70.0s | 3 | 0 | 23.3s |" in report_md


def test_generate_markdown_no_generation_metrics_without_turns():
    """Test that Generation Metrics section is omitted when no tasks have turns."""
    summary = RunSummary(
        run_id="test-run",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 1, 0),
        total_duration_seconds=60.0,
        tasks_run=1,
        tasks_succeeded=0,
        tasks_failed=0,
        tasks_error=1,
        task_results=[
            _make_task_result("task_error", "ERROR", None, 0.0),
        ],
        framework_version="0.1.0",
        environment_info={},
    )

    report_md = ReportGenerator.generate_markdown(summary)
    assert "Generation Metrics" not in report_md


def test_generate_markdown_aggregate_metrics():
    """Test that aggregate P0 metrics are calculated correctly."""
    summary = RunSummary(
        run_id="test-run",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 2, 0),
        total_duration_seconds=120.0,
        tasks_run=2,
        tasks_succeeded=2,
        tasks_failed=0,
        tasks_error=0,
        task_results=[
            _make_task_result("task1", "SUCCESS", 0.8, 40.0, iteration_count=2),
            _make_task_result("task2", "SUCCESS", 0.6, 60.0, iteration_count=4),
        ],
        framework_version="0.1.0",
        environment_info={},
    )

    report_md = ReportGenerator.generate_markdown(summary)

    # Avg reliability = (0.8 + 0.6) / 2 = 0.7
    assert "Avg Reliability Score**: 0.700" in report_md
    # Avg latency = (40 + 60) / 2 = 50.0s
    assert "Avg Generation Latency**: 50.0s" in report_md


def test_generate_markdown_backward_compatible_task_results():
    """Test that old-format task_results (without new fields) still render correctly."""
    summary = RunSummary(
        run_id="old-run",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 1, 0),
        total_duration_seconds=60.0,
        tasks_run=1,
        tasks_succeeded=1,
        tasks_failed=0,
        tasks_error=0,
        task_results=[
            {
                "task_id": "task1",
                "status": "SUCCESS",
                "weighted_score": 0.9,
                "duration": 30.0,
            }
        ],
        framework_version="0.1.0",
        environment_info={},
    )

    report_md = ReportGenerator.generate_markdown(summary)

    # Should render without errors, just showing N/A for missing fields
    assert "task1" in report_md
    assert "SUCCESS" in report_md
    assert "0.900" in report_md


def test_load_from_run_dir_with_markdown(tmp_path):
    """Test loading pre-generated markdown report."""
    run_dir = tmp_path / "runs" / "test-run"
    run_dir.mkdir(parents=True)

    # Create a pre-generated markdown report
    report_content = "# Test Report\n\nThis is a test report."
    report_path = run_dir / "experiment.md"
    report_path.write_text(report_content)

    # Load report
    loaded_report, source_path = ReportGenerator.load_from_run_dir(run_dir)

    assert loaded_report == report_content
    assert source_path == report_path


def test_load_from_run_dir_with_json_fallback(tmp_path):
    """Test loading from JSON summary when markdown missing."""
    run_dir = tmp_path / "runs" / "test-run"
    run_dir.mkdir(parents=True)

    # Create summary JSON (no markdown)
    summary = RunSummary(
        run_id="test-run",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 0, 10),
        total_duration_seconds=10.0,
        tasks_run=1,
        tasks_succeeded=1,
        tasks_failed=0,
        tasks_error=0,
        task_results=[
            {
                "task_id": "task1",
                "status": "SUCCESS",
                "weighted_score": 1.0,
                "duration": 10.0,
            }
        ],
        framework_version="0.1.0",
        environment_info={},
    )

    summary_path = run_dir / "experiment.json"
    summary_path.write_text(summary.model_dump_json(indent=2))

    # Load report (should regenerate from JSON)
    loaded_report, source_path = ReportGenerator.load_from_run_dir(run_dir)

    assert "# Evaluation Run Report" in loaded_report
    assert "test-run" in loaded_report
    assert "task1" in loaded_report
    assert source_path == summary_path


def test_load_from_run_dir_missing_files(tmp_path):
    """Test error handling when no report files exist."""
    run_dir = tmp_path / "runs" / "empty-run"
    run_dir.mkdir(parents=True)

    # Try to load from empty directory
    with pytest.raises(FileNotFoundError, match="No report found"):
        ReportGenerator.load_from_run_dir(run_dir)


@_SKIP_NO_SYMLINK
def test_load_from_run_dir_resolves_symlink(tmp_path):
    """Test that symlinks are resolved when loading reports."""
    # Create actual run directory
    actual_run_dir = tmp_path / "runs" / "2025-10-11_12-00-00"
    actual_run_dir.mkdir(parents=True)

    report_content = "# Symlink Test Report"
    report_path = actual_run_dir / "experiment.md"
    report_path.write_text(report_content)

    # Create symlink
    symlink_path = tmp_path / "runs" / "latest"
    symlink_path.symlink_to(actual_run_dir)

    # Load via symlink
    loaded_report, source_path = ReportGenerator.load_from_run_dir(symlink_path)

    assert loaded_report == report_content
    # Source path should be from resolved directory, not symlink
    assert "2025-10-11_12-00-00" in str(source_path)


def test_generate_markdown_with_model_info():
    """Test that model info appears in header and task details table."""
    summary = RunSummary(
        run_id="test-run",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 1, 0),
        total_duration_seconds=60.0,
        tasks_run=2,
        tasks_succeeded=2,
        tasks_failed=0,
        tasks_error=0,
        task_results=[
            _make_task_result(
                "task1", "SUCCESS", 1.0, 30.0, iteration_count=1, model_used="claude-sonnet-4-5-20250514"
            ),
            _make_task_result(
                "task2", "SUCCESS", 0.9, 30.0, iteration_count=1, model_used="claude-sonnet-4-5-20250514"
            ),
        ],
        framework_version="0.1.0",
        environment_info={},
    )

    report_md = ReportGenerator.generate_markdown(summary)

    # Header should show single model
    assert "**Model**: `claude-sonnet-4-5-20250514`" in report_md

    # Task details table should have Model column
    lines = report_md.split("\n")
    header_lines = [line for line in lines if line.startswith("| Task ID")]
    assert len(header_lines) == 1
    assert "Model" in header_lines[0]

    # Task rows should include model
    assert "claude-sonnet-4-5-20250514" in report_md


def test_generate_markdown_no_model_info():
    """Test that Model column is omitted when no tasks have model_used."""
    summary = RunSummary(
        run_id="test-run",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 1, 0),
        total_duration_seconds=60.0,
        tasks_run=1,
        tasks_succeeded=1,
        tasks_failed=0,
        tasks_error=0,
        task_results=[
            _make_task_result("task1", "SUCCESS", 0.9, 30.0, iteration_count=1),
        ],
        framework_version="0.1.0",
        environment_info={},
    )

    report_md = ReportGenerator.generate_markdown(summary)

    # Header should NOT show model line
    assert "**Model**:" not in report_md
    assert "**Models**:" not in report_md

    # Task details table should NOT have Model column
    lines = report_md.split("\n")
    header_lines = [line for line in lines if line.startswith("| Task ID")]
    assert len(header_lines) == 1
    assert "Model" not in header_lines[0]


def test_generate_markdown_multiple_models():
    """Test that header shows multiple models when tasks use different models."""
    summary = RunSummary(
        run_id="test-run",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 2, 0),
        total_duration_seconds=120.0,
        tasks_run=2,
        tasks_succeeded=2,
        tasks_failed=0,
        tasks_error=0,
        task_results=[
            _make_task_result(
                "task1", "SUCCESS", 1.0, 60.0, iteration_count=1, model_used="claude-sonnet-4-5-20250514"
            ),
            _make_task_result("task2", "SUCCESS", 0.9, 60.0, iteration_count=1, model_used="claude-opus-4-20250514"),
        ],
        framework_version="0.1.0",
        environment_info={},
    )

    report_md = ReportGenerator.generate_markdown(summary)

    # Header should show multiple models
    assert "**Models**:" in report_md
    assert "`claude-opus-4-20250514`" in report_md
    assert "`claude-sonnet-4-5-20250514`" in report_md

    # Task details table should have Model column with per-task values
    lines = report_md.split("\n")
    header_lines = [line for line in lines if line.startswith("| Task ID")]
    assert len(header_lines) == 1
    assert "Model" in header_lines[0]


def test_generate_markdown_with_agent_settings():
    """Test that Agent Settings section appears when agent_config is present."""
    summary = RunSummary(
        run_id="test-run",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 1, 0),
        total_duration_seconds=60.0,
        tasks_run=1,
        tasks_succeeded=1,
        tasks_failed=0,
        tasks_error=0,
        task_results=[
            _make_task_result(
                "task1",
                "SUCCESS",
                1.0,
                30.0,
                iteration_count=1,
                agent_config={
                    "type": "claude-code",
                    "permission_mode": "acceptEdits",
                    "allowed_tools": ["Read", "Write", "Bash"],
                    "model": "claude-sonnet-4-5-20250514",
                },
            ),
        ],
        framework_version="0.1.0",
        environment_info={},
    )

    report_md = ReportGenerator.generate_markdown(summary)

    assert "## Agent Settings" in report_md
    assert "**Permission Mode**: acceptEdits" in report_md
    assert "**Allowed Tools**: Read, Write, Bash" in report_md
    assert "**Model**: claude-sonnet-4-5-20250514" in report_md
    # Agent Settings should appear before Environment
    agent_idx = report_md.index("## Agent Settings")
    env_idx = report_md.index("## Environment")
    assert agent_idx < env_idx


def test_generate_markdown_agent_settings_all_tools():
    """Test that Agent Settings shows (all) when allowed_tools is None."""
    summary = RunSummary(
        run_id="test-run",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 1, 0),
        total_duration_seconds=60.0,
        tasks_run=1,
        tasks_succeeded=1,
        tasks_failed=0,
        tasks_error=0,
        task_results=[
            _make_task_result(
                "task1",
                "SUCCESS",
                1.0,
                30.0,
                iteration_count=1,
                agent_config={
                    "type": "claude-code",
                    "permission_mode": "bypassPermissions",
                    "allowed_tools": None,
                    "model": None,
                },
            ),
        ],
        framework_version="0.1.0",
        environment_info={},
    )

    report_md = ReportGenerator.generate_markdown(summary)

    assert "## Agent Settings" in report_md
    assert "**Permission Mode**: bypassPermissions" in report_md
    assert "**Allowed Tools**: (all)" in report_md
    # Model line should not appear when model is None
    agent_section_start = report_md.index("## Agent Settings")
    env_section_start = report_md.index("## Environment")
    agent_section = report_md[agent_section_start:env_section_start]
    assert "**Model**:" not in agent_section


def test_generate_markdown_with_sdk_options():
    """Test that Agent Settings section renders from sdk_options when available."""
    summary = RunSummary(
        run_id="test-run",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 1, 0),
        total_duration_seconds=60.0,
        tasks_run=1,
        tasks_succeeded=1,
        tasks_failed=0,
        tasks_error=0,
        task_results=[
            _make_task_result(
                "task1",
                "SUCCESS",
                1.0,
                30.0,
                iteration_count=1,
                sdk_options={
                    "permission_mode": "bypassPermissions",
                    "allowed_tools": ["Read", "Write"],
                    "model": "claude-sonnet-4-5-20250514",
                    "max_turns": 50,
                    "thinking": {"type": "enabled", "budget_tokens": 10000},
                    "effort": "high",
                    "mcp_servers": {"my-server": {"command": "node", "args": ["server.js"]}},
                    "betas": ["context-1m-2025-08-07"],
                    "max_budget_usd": 5.0,
                },
            ),
        ],
        framework_version="0.1.0",
        environment_info={},
    )

    report_md = ReportGenerator.generate_markdown(summary)

    assert "## Agent Settings" in report_md
    assert "**Permission Mode**: bypassPermissions" in report_md
    assert "**Allowed Tools**: Read, Write" in report_md
    assert "**Model**: claude-sonnet-4-5-20250514" in report_md
    assert "**Max Turns**: 50" in report_md
    assert "**Max Budget (USD)**: 5.0" in report_md
    assert "**Thinking**:" in report_md
    assert "**Effort**: high" in report_md
    assert "**MCP Servers**: my-server" in report_md
    assert "**Betas**: context-1m-2025-08-07" in report_md


def test_generate_markdown_sdk_options_preferred_over_agent_config():
    """Test that sdk_options is preferred over agent_config when both are present."""
    summary = RunSummary(
        run_id="test-run",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 1, 0),
        total_duration_seconds=60.0,
        tasks_run=1,
        tasks_succeeded=1,
        tasks_failed=0,
        tasks_error=0,
        task_results=[
            _make_task_result(
                "task1",
                "SUCCESS",
                1.0,
                30.0,
                iteration_count=1,
                agent_config={
                    "type": "claude-code",
                    "permission_mode": "acceptEdits",
                    "allowed_tools": None,
                    "model": None,
                },
                sdk_options={
                    "permission_mode": "bypassPermissions",
                    "allowed_tools": ["Read"],
                    "model": "claude-opus-4-20250514",
                    "max_turns": 100,
                },
            ),
        ],
        framework_version="0.1.0",
        environment_info={},
    )

    report_md = ReportGenerator.generate_markdown(summary)

    # Should use sdk_options values, not agent_config
    assert "**Permission Mode**: bypassPermissions" in report_md
    assert "**Allowed Tools**: Read" in report_md
    assert "**Model**: claude-opus-4-20250514" in report_md
    assert "**Max Turns**: 100" in report_md


def test_generate_markdown_sdk_options_defaults_hidden():
    """Test that Agent Settings hides SDK fields that are None/empty (defaults)."""
    summary = RunSummary(
        run_id="test-run",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 1, 0),
        total_duration_seconds=60.0,
        tasks_run=1,
        tasks_succeeded=1,
        tasks_failed=0,
        tasks_error=0,
        task_results=[
            _make_task_result(
                "task1",
                "SUCCESS",
                1.0,
                30.0,
                iteration_count=1,
                sdk_options={
                    "permission_mode": "bypassPermissions",
                    "allowed_tools": [],
                    "model": None,
                    "max_turns": None,
                    "thinking": None,
                    "effort": None,
                    "mcp_servers": {},
                    "betas": [],
                    "max_budget_usd": None,
                    "system_prompt": None,
                },
            ),
        ],
        framework_version="0.1.0",
        environment_info={},
    )

    report_md = ReportGenerator.generate_markdown(summary)

    assert "## Agent Settings" in report_md
    assert "**Permission Mode**: bypassPermissions" in report_md
    assert "**Allowed Tools**: (all)" in report_md
    # None/empty defaults should not appear
    assert "**Max Turns**" not in report_md
    assert "**Max Budget" not in report_md
    assert "**Thinking**" not in report_md
    assert "**Effort**" not in report_md
    assert "**MCP Servers**" not in report_md
    assert "**Betas**" not in report_md
    assert "**System Prompt**" not in report_md


def test_agent_settings_rows_system_prompt_preset_shapes():
    """Feed a REAL dump_dataclass(ClaudeAgentOptions) — the shape a Claude Code run
    actually persists into sdk_options — not a hand-built dict: append-mode runs
    carry a SystemPromptPreset dict, and the report must render the appended
    prompt text (never the dict repr), omit the row for a bare preset, and
    surface the regime for a replace-mode plain string."""
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk.types import SystemPromptPreset

    from coder_eval.reports import collect_agent_settings_rows
    from coder_eval.utils import dump_dataclass

    appended = SystemPromptPreset(
        type="preset", preset="claude_code", exclude_dynamic_sections=True, append="Be terse."
    )
    rows = dict(collect_agent_settings_rows(dump_dataclass(ClaudeAgentOptions(system_prompt=appended)), is_sdk=True))
    assert rows["System Prompt"] == "Be terse."
    assert "System Prompt Mode" not in rows

    bare = SystemPromptPreset(type="preset", preset="claude_code", exclude_dynamic_sections=True)
    rows = dict(collect_agent_settings_rows(dump_dataclass(ClaudeAgentOptions(system_prompt=bare)), is_sdk=True))
    assert "System Prompt" not in rows

    rows = dict(collect_agent_settings_rows(dump_dataclass(ClaudeAgentOptions(system_prompt="Grader.")), is_sdk=True))
    assert rows["System Prompt"] == "Grader."
    assert rows["System Prompt Mode"] == "replace"


def test_agent_settings_rows_prefer_the_recorded_semantics_marker():
    """The regime comes from run.json's system_prompt_semantics when the run has it,
    so an append run is visibly an append run instead of being indistinguishable from
    a pre-marker one. Wire-shape sniffing stays only as the fallback for older runs."""
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk.types import SystemPromptPreset

    from coder_eval.reports import collect_agent_settings_rows
    from coder_eval.utils import dump_dataclass

    appended = dump_dataclass(
        ClaudeAgentOptions(
            system_prompt=SystemPromptPreset(
                type="preset", preset="claude_code", exclude_dynamic_sections=True, append="Be terse."
            )
        )
    )
    rows = dict(collect_agent_settings_rows(appended, is_sdk=True, system_prompt_semantics="append"))
    assert rows["System Prompt Mode"] == "append"

    # An agent that persists a plain string but recorded 'append' (Codex-style
    # developer_instructions) must not be mislabelled 'replace' by the fallback.
    plain = dump_dataclass(ClaudeAgentOptions(system_prompt="Be terse."))
    rows = dict(collect_agent_settings_rows(plain, is_sdk=True, system_prompt_semantics="append"))
    assert rows["System Prompt Mode"] == "append"

    # Pre-marker run: no marker to read, so the shape is all there is.
    rows = dict(collect_agent_settings_rows(plain, is_sdk=True))
    assert rows["System Prompt Mode"] == "replace"


def test_generate_markdown_system_prompt_preset_not_dict_repr():
    """End-to-end: a preset-shaped sdk_options.system_prompt renders as prompt text
    in the Markdown report, and a bare preset (no configured prompt) keeps the
    System Prompt row absent — the pre-preset behavior for unset prompts."""
    summary = RunSummary(
        run_id="test-run",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 1, 0),
        total_duration_seconds=60.0,
        tasks_run=1,
        tasks_succeeded=1,
        tasks_failed=0,
        tasks_error=0,
        task_results=[
            _make_task_result(
                "task1",
                "SUCCESS",
                1.0,
                30.0,
                iteration_count=1,
                sdk_options={
                    "permission_mode": "bypassPermissions",
                    "allowed_tools": [],
                    "system_prompt": {
                        "type": "preset",
                        "preset": "claude_code",
                        "exclude_dynamic_sections": True,
                        "append": "You are a careful engineer.",
                    },
                },
            ),
        ],
        framework_version="0.1.0",
        environment_info={},
    )

    report_md = ReportGenerator.generate_markdown(summary)
    assert "**System Prompt**: You are a careful engineer." in report_md
    assert "{'type': 'preset'" not in report_md

    # Bare preset == no configured prompt: the row stays absent.
    summary.task_results[0]["sdk_options"]["system_prompt"] = {
        "type": "preset",
        "preset": "claude_code",
        "exclude_dynamic_sections": True,
    }
    report_md = ReportGenerator.generate_markdown(summary)
    assert "**System Prompt**" not in report_md


def test_generate_markdown_no_agent_settings():
    """Test that Agent Settings section is omitted when no task has agent_config."""
    summary = RunSummary(
        run_id="test-run",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 1, 0),
        total_duration_seconds=60.0,
        tasks_run=1,
        tasks_succeeded=1,
        tasks_failed=0,
        tasks_error=0,
        task_results=[
            _make_task_result("task1", "SUCCESS", 1.0, 30.0, iteration_count=1),
        ],
        framework_version="0.1.0",
        environment_info={},
    )

    report_md = ReportGenerator.generate_markdown(summary)

    assert "## Agent Settings" not in report_md


def test_generate_markdown_with_installed_tools():
    """Test that Installed Tools section appears when tasks have installed tools."""
    summary = RunSummary(
        run_id="test-run",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 2, 0),
        total_duration_seconds=120.0,
        tasks_run=2,
        tasks_succeeded=2,
        tasks_failed=0,
        tasks_error=0,
        task_results=[
            _make_task_result(
                "task1",
                "SUCCESS",
                1.0,
                60.0,
                iteration_count=1,
                installed_tools={"@uipath/cli": "0.1.5"},
            ),
            _make_task_result(
                "task2",
                "SUCCESS",
                0.9,
                60.0,
                iteration_count=1,
                installed_tools={"@uipath/cli": "0.1.5"},
            ),
        ],
        framework_version="0.1.0",
        environment_info={},
    )

    report_md = ReportGenerator.generate_markdown(summary)

    assert "## Installed Tools" in report_md
    assert "| Task ID | Tool | Version |" in report_md
    assert "| task1 | @uipath/cli | 0.1.5 |" in report_md
    assert "| task2 | @uipath/cli | 0.1.5 |" in report_md
    # Should appear before Environment
    tools_idx = report_md.index("## Installed Tools")
    env_idx = report_md.index("## Environment")
    assert tools_idx < env_idx


def test_generate_markdown_installed_tools_multiple_per_task():
    """Test that multiple tools per task each get their own row."""
    summary = RunSummary(
        run_id="test-run",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 1, 0),
        total_duration_seconds=60.0,
        tasks_run=1,
        tasks_succeeded=1,
        tasks_failed=0,
        tasks_error=0,
        task_results=[
            _make_task_result(
                "task1",
                "SUCCESS",
                1.0,
                60.0,
                iteration_count=1,
                installed_tools={"@uipath/cli": "0.1.5", "@uipath/sdk": "2.0.0"},
            ),
        ],
        framework_version="0.1.0",
        environment_info={},
    )

    report_md = ReportGenerator.generate_markdown(summary)

    assert "## Installed Tools" in report_md
    assert "| task1 | @uipath/sdk | 2.0.0 |" in report_md
    assert "| task1 | @uipath/cli | 0.1.5 |" in report_md


def test_generate_markdown_no_installed_tools():
    """Test that Installed Tools section is omitted when no tasks have installed tools."""
    summary = RunSummary(
        run_id="test-run",
        start_time=datetime(2025, 10, 11, 12, 0, 0),
        end_time=datetime(2025, 10, 11, 12, 1, 0),
        total_duration_seconds=60.0,
        tasks_run=1,
        tasks_succeeded=1,
        tasks_failed=0,
        tasks_error=0,
        task_results=[
            _make_task_result("task1", "SUCCESS", 1.0, 30.0, iteration_count=1),
        ],
        framework_version="0.1.0",
        environment_info={},
    )

    report_md = ReportGenerator.generate_markdown(summary)

    assert "## Installed Tools" not in report_md


def test_aggregate_command_statistics_nested_layout(tmp_path):
    """_aggregate_command_statistics should find task.json in nested experiment layout."""
    import json

    # Create nested experiment layout: run_dir/variant_id/task_id/NN/task.json
    task_dir = tmp_path / "opus" / "my-task" / "00"
    task_dir.mkdir(parents=True)

    eval_result = {
        "task_id": "my-task",
        "task_description": "test",
        "variant_id": "opus",
        "agent_type": "claude-code",
        "started_at": "2025-01-01T00:00:00",
        "final_status": "SUCCESS",
        "iteration_count": 1,
        "environment_info": {},
        "iterations": [
            {
                "iteration": 1,
                "user_input": "test prompt",
                "agent_output": "test response",
                "duration_seconds": 5.0,
                "commands": [
                    {
                        "tool_name": "Bash",
                        "tool_id": "tool_001",
                        "timestamp": "2025-01-01T00:00:01",
                        "parameters": {"command": "echo hi"},
                        "duration_ms": 100,
                    }
                ],
            }
        ],
    }
    (task_dir / "task.json").write_text(json.dumps(eval_result))

    stats = ReportGenerator._aggregate_command_statistics(tmp_path)
    assert stats is not None, "Should find task.json in nested layout"
    assert stats.total_commands == 1


def test_report_generator_private_methods_used_by_experiment_reports():
    """Verify all private methods called by reports_experiment.py exist on ReportGenerator."""
    required_methods = [
        "_generate_generation_metrics_section",
        "_generate_token_usage_section",
        "_aggregate_command_statistics",
        "_generate_command_statistics_section",
        "_generate_installed_tools_section",
        "_generate_agent_settings_section",
    ]
    for method_name in required_methods:
        assert hasattr(ReportGenerator, method_name), f"ReportGenerator.{method_name} is missing"
        assert callable(getattr(ReportGenerator, method_name)), f"ReportGenerator.{method_name} is not callable"


@pytest.mark.parametrize(
    "task_turns, expected_line",
    [
        # Mixed: 1 recovered + 2 terminal partials → breakdown line present.
        (
            [
                [
                    {"iteration": 1, "duration_seconds": 5.0, "command_count": 1, "crashed": True},
                    {"iteration": 1, "duration_seconds": 25.0, "command_count": 3, "crashed": False},
                ],
                [
                    {"iteration": 1, "duration_seconds": 10.0, "command_count": 1, "crashed": True},
                    {"iteration": 1, "duration_seconds": 10.0, "command_count": 1, "crashed": True},
                ],
            ],
            "Crashed Partials**: 3 (1 recovered, 2 terminal)",
        ),
        # No crashed partials → no Crashed Partials line at all.
        (
            [[{"iteration": 1, "duration_seconds": 30.0, "command_count": 1, "crashed": False}]],
            None,
        ),
    ],
)
def test_markdown_summary_crashed_partials_breakdown(task_turns, expected_line):
    """Mixed runs render the breakdown; clean runs omit the line entirely."""
    task_results = [
        _make_task_result(f"task{i}", "SUCCESS", 1.0, 30.0, iteration_count=1, turns=turns)
        for i, turns in enumerate(task_turns, start=1)
    ]
    summary = RunSummary(
        run_id="r",
        start_time=datetime(2026, 4, 25, 12, 0, 0),
        end_time=datetime(2026, 4, 25, 12, 1, 0),
        total_duration_seconds=60.0,
        tasks_run=len(task_results),
        tasks_succeeded=len(task_results),
        tasks_failed=0,
        tasks_error=0,
        task_results=task_results,
        framework_version="0.1.0",
        environment_info={"coder_eval": "0.1.0"},
    )
    report_md = ReportGenerator.generate_markdown(summary)
    if expected_line is None:
        assert "Crashed Partials" not in report_md
    else:
        assert expected_line in report_md


def _summary_with_notes(
    *,
    max_turns_exhausted: bool = False,
    expected_turns_overage: list[int] | None = None,
) -> RunSummary:
    task = _make_task_result("t1", "SUCCESS", 1.0, 10.0)
    task["max_turns_exhausted"] = max_turns_exhausted
    task["expected_turns_overage"] = expected_turns_overage
    return RunSummary(
        run_id="r",
        start_time=datetime(2026, 5, 21, 12, 0, 0),
        end_time=datetime(2026, 5, 21, 12, 0, 30),
        total_duration_seconds=30.0,
        tasks_run=1,
        tasks_succeeded=1,
        tasks_failed=0,
        tasks_error=0,
        task_results=[task],
        framework_version="0.1.0",
        environment_info={"coder_eval": "0.1.0"},
    )


def test_generate_markdown_renders_expected_turns_marker_when_exceeded():
    summary = _summary_with_notes(expected_turns_overage=[7, 5])
    report_md = ReportGenerator.generate_markdown(summary)
    assert "## Run-time Notes" in report_md
    assert "expected_turns exceeded" in report_md
    assert "7/5" in report_md


def test_generate_markdown_no_expected_turns_marker_when_under():
    # Under-budget: the dict carries no overage field — emit nothing.
    summary = _summary_with_notes(expected_turns_overage=None)
    report_md = ReportGenerator.generate_markdown(summary)
    assert "expected_turns exceeded" not in report_md


def test_generate_markdown_no_expected_turns_marker_when_unset():
    summary = _summary_with_notes()
    report_md = ReportGenerator.generate_markdown(summary)
    assert "expected_turns exceeded" not in report_md
    assert "## Run-time Notes" not in report_md


def test_generate_markdown_renders_max_turns_exhausted_marker():
    summary = _summary_with_notes(max_turns_exhausted=True)
    report_md = ReportGenerator.generate_markdown(summary)
    assert "## Run-time Notes" in report_md
    assert "max_turns exhausted" in report_md


def test_generate_markdown_no_max_turns_marker_when_not_exhausted():
    summary = _summary_with_notes(max_turns_exhausted=False)
    report_md = ReportGenerator.generate_markdown(summary)
    assert "max_turns exhausted" not in report_md


def test_generate_markdown_renders_both_markers_when_both_fire():
    summary = _summary_with_notes(max_turns_exhausted=True, expected_turns_overage=[7, 5])
    report_md = ReportGenerator.generate_markdown(summary)
    assert "max_turns exhausted" in report_md
    assert "expected_turns exceeded" in report_md
    assert "7/5" in report_md
