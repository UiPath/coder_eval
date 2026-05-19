"""Tests for path utilities."""

import platform
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from coder_eval.models import ResolvedTask, TaskDefinition
from coder_eval.path_utils import (
    TASK_LOG_FILENAME,
    build_task_run_dir,
    create_latest_symlink,
    format_task_log_id,
    generate_run_id,
    replicate_subdir_name,
    task_log_path,
)
from tests._path_helpers import tmp_subdir


def test_generate_run_id():
    """Test run ID generation format."""
    run_id = generate_run_id()
    # Should match format: YYYY-MM-DD_HH-MM-SS
    assert re.match(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}", run_id)


def test_replicate_subdir_name_zero_pads():
    assert replicate_subdir_name(0) == "00"
    assert replicate_subdir_name(5) == "05"
    assert replicate_subdir_name(99) == "99"


def test_build_task_run_dir_default_replicate():
    run_dir = tmp_subdir("runs", "2025-01-01_12-00-00")
    result = build_task_run_dir(run_dir, "default", "hello_world")
    assert result == run_dir / "default" / "hello_world" / "00"


def test_build_task_run_dir_dataset_row_task_id():
    run_dir = tmp_subdir("runs", "2025-01-01_12-00-00")
    result = build_task_run_dir(run_dir, "sonnet", "classify/row-001")
    assert result == run_dir / "sonnet" / "classify" / "row-001" / "00"


def test_build_task_run_dir_custom_replicate_index():
    run_dir = tmp_subdir("runs", "2025-01-01_12-00-00")
    result = build_task_run_dir(run_dir, "v1", "task_a", replicate_index=3)
    assert result == run_dir / "v1" / "task_a" / "03"


def test_task_log_filename_constant():
    assert TASK_LOG_FILENAME == "task.log"


def test_task_log_path_helper(tmp_path: Path):
    assert task_log_path(tmp_path) == tmp_path / TASK_LOG_FILENAME


def test_format_task_log_id_basic():
    assert format_task_log_id("default", "hello_date", 0) == "default/hello_date/00"


def test_format_task_log_id_nonzero_replicate():
    assert format_task_log_id("v1", "task", 7) == "v1/task/07"


def test_format_task_log_id_large_replicate_no_truncation():
    assert format_task_log_id("v", "t", 100) == "v/t/100"


def test_format_task_log_id_matches_build_task_run_dir_relative_path():
    run_dir = tmp_subdir("runs", "X")
    task_dir = build_task_run_dir(run_dir, "v", "t", 3)
    assert format_task_log_id("v", "t", 3) == task_dir.relative_to(run_dir).as_posix()


def test_resolved_task_rejects_negative_replicate_index(tmp_path: Path):
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox={"driver": "tempdir"},
        success_criteria=[{"type": "file_exists", "path": "out.txt", "description": "f"}],
    )
    with pytest.raises(ValidationError):
        ResolvedTask(
            task=task,
            task_file=tmp_path / "t.yaml",
            run_dir=tmp_path / "run",
            variant_id="v1",
            replicate_index=-1,
        )


def test_create_latest_symlink(tmp_path):
    """Test symlink creation."""
    runs_base = tmp_path / "runs"
    runs_base.mkdir()
    run_dir = runs_base / "2025-01-01_12-00-00"
    run_dir.mkdir()

    create_latest_symlink(runs_base, "2025-01-01_12-00-00")

    latest = runs_base / "latest"
    if platform.system() != "Windows":  # May not work on Windows
        assert latest.is_symlink()
        assert latest.resolve() == run_dir


def test_create_latest_symlink_updates_existing(tmp_path):
    """Test that symlink updates when new run is created."""
    runs_base = tmp_path / "runs"
    runs_base.mkdir()

    # Create first run
    run_dir1 = runs_base / "2025-01-01_12-00-00"
    run_dir1.mkdir()
    create_latest_symlink(runs_base, "2025-01-01_12-00-00")

    # Create second run
    run_dir2 = runs_base / "2025-01-01_13-00-00"
    run_dir2.mkdir()
    create_latest_symlink(runs_base, "2025-01-01_13-00-00")

    latest = runs_base / "latest"
    if platform.system() != "Windows":
        assert latest.is_symlink()
        assert latest.resolve() == run_dir2  # Should point to newer run
