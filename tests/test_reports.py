"""Tests for report generation module."""

from datetime import datetime

import pytest

from coder_eval.models import RunSummary
from coder_eval.reports import ReportGenerator


def _make_task_result(
    task_id: str,
    status: str,
    weighted_score: float | None,
    duration: float,
    iteration_count: int | None = None,
    turns: list[dict] | None = None,
    reference_similarity: float | None = None,
) -> dict:
    """Helper to create a task_result dict with all expected fields."""
    return {
        "task_id": task_id,
        "status": status,
        "weighted_score": weighted_score,
        "duration": duration,
        "iteration_count": iteration_count,
        "turns": turns or [],
        "reference_similarity": reference_similarity,
    }


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
    assert "Success Rate**: 66.7%" in report_md

    # Check P0 aggregate metrics
    assert "Avg Reliability Score**:" in report_md
    assert "Avg Generation Latency**:" in report_md
    assert "Avg Self-Correction Iterations**:" in report_md
    assert "Avg Ground Truth Similarity**:" in report_md

    # Check task details table has new columns
    assert "Reliability Score" in report_md
    assert "Iterations" in report_md
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
    assert "Self-Corrections" in report_md

    # Check environment section
    assert "Framework**: 0.1.0" in report_md
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
    assert "Success Rate**: 0.0%" in report_md
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
    # task1: 1 iteration = 0 self-corrections
    assert "| task1 | 50.0s | 1 | 50.0s | 0 |" in report_md
    # task2: 3 iterations = 2 self-corrections, avg turn = (25+22+23)/3 = 23.3s
    assert "| task2 | 70.0s | 3 | 23.3s | 2 |" in report_md


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
    # Avg iterations = (2 + 4) / 2 = 3.0
    assert "Avg Self-Correction Iterations**: 3.0" in report_md


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
    report_path = run_dir / "run-report.md"
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

    summary_path = run_dir / "run-summary.json"
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


def test_load_from_run_dir_resolves_symlink(tmp_path):
    """Test that symlinks are resolved when loading reports."""
    # Create actual run directory
    actual_run_dir = tmp_path / "runs" / "2025-10-11_12-00-00"
    actual_run_dir.mkdir(parents=True)

    report_content = "# Symlink Test Report"
    report_path = actual_run_dir / "run-report.md"
    report_path.write_text(report_content)

    # Create symlink
    symlink_path = tmp_path / "runs" / "latest"
    symlink_path.symlink_to(actual_run_dir)

    # Load via symlink
    loaded_report, source_path = ReportGenerator.load_from_run_dir(symlink_path)

    assert loaded_report == report_content
    # Source path should be from resolved directory, not symlink
    assert "2025-10-11_12-00-00" in str(source_path)
