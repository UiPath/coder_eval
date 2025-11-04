"""Tests for report generation module."""

from datetime import datetime

import pytest

from coder_eval.models import RunSummary
from coder_eval.reports import ReportGenerator


def test_generate_markdown_basic():
    """Test basic markdown generation."""
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
            {
                "task_id": "task1",
                "status": "SUCCESS",
                "weighted_score": 1.0,
                "duration": 120.5,
            },
            {
                "task_id": "task2",
                "status": "SUCCESS",
                "weighted_score": 0.95,
                "duration": 100.3,
            },
            {
                "task_id": "task3",
                "status": "FAILED",
                "weighted_score": 0.5,
                "duration": 109.2,
            },
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

    # Check task details table
    assert "task1" in report_md
    assert "task2" in report_md
    assert "task3" in report_md
    assert "1.000" in report_md  # weighted_score for task1
    assert "0.950" in report_md  # weighted_score for task2
    assert "0.500" in report_md  # weighted_score for task3

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
            {
                "task_id": "task_error",
                "status": "ERROR",
                "weighted_score": None,
                "duration": 60.0,
            },
        ],
        framework_version="0.1.0",
        environment_info={},
    )

    report_md = ReportGenerator.generate_markdown(summary)

    assert "task_error" in report_md
    assert "ERROR" in report_md
    assert "N/A" in report_md  # Should show N/A for null weighted_score


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
