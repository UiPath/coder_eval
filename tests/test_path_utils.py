"""Tests for path utilities."""

import platform
import re
from pathlib import Path

from coder_eval.path_utils import (
    create_latest_symlink,
    ensure_run_structure,
    generate_run_id,
    get_task_artifact_dir,
    get_task_report_path,
    get_task_run_dir,
)


def test_generate_run_id():
    """Test run ID generation format."""
    run_id = generate_run_id()
    # Should match format: YYYY-MM-DD_HH-MM-SS
    assert re.match(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}", run_id)


def test_get_task_run_dir():
    """Test task directory path construction."""
    run_dir = Path("/tmp/runs/2025-01-01_12-00-00")
    task_dir = get_task_run_dir(run_dir, "hello_world")
    assert task_dir == run_dir / "hello_world"


def test_get_task_report_path():
    """Test report path construction."""
    run_dir = Path("/tmp/runs/2025-01-01_12-00-00")
    report_path = get_task_report_path(run_dir, "hello_world")
    assert report_path == run_dir / "hello_world" / "task.json"


def test_get_task_artifact_dir():
    """Test artifact directory path construction."""
    run_dir = Path("/tmp/runs/2025-01-01_12-00-00")
    artifact_dir = get_task_artifact_dir(run_dir, "hello_world")
    assert artifact_dir == run_dir / "hello_world" / "artifacts"


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


def test_ensure_run_structure(tmp_path):
    """Test directory creation."""
    run_dir = tmp_path / "runs" / "2025-01-01_12-00-00"
    ensure_run_structure(run_dir, "hello_world")

    task_dir = run_dir / "hello_world"
    assert task_dir.exists()
    assert task_dir.is_dir()


def test_ensure_run_structure_idempotent(tmp_path):
    """Test that ensure_run_structure can be called multiple times."""
    run_dir = tmp_path / "runs" / "2025-01-01_12-00-00"

    # Call twice
    ensure_run_structure(run_dir, "hello_world")
    ensure_run_structure(run_dir, "hello_world")

    task_dir = run_dir / "hello_world"
    assert task_dir.exists()
    assert task_dir.is_dir()
