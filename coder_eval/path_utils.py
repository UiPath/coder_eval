"""Path utilities for run directory management."""

import platform
from datetime import datetime
from pathlib import Path


def generate_run_id() -> str:
    """Generate filesystem-safe timestamp: YYYY-MM-DD_HH-MM-SS."""
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def get_task_run_dir(run_dir: Path, task_id: str) -> Path:
    """Get directory for task within run: {run_dir}/{task_id}/."""
    return run_dir / task_id


def get_task_report_path(run_dir: Path, task_id: str) -> Path:
    """Get report path: {run_dir}/{task_id}/report.json."""
    return get_task_run_dir(run_dir, task_id) / "report.json"


def get_task_artifact_dir(run_dir: Path, task_id: str) -> Path:
    """Get artifact dir: {run_dir}/{task_id}/artifacts/."""
    return get_task_run_dir(run_dir, task_id) / "artifacts"


def create_latest_symlink(runs_base: Path, run_id: str) -> None:
    """Create/update 'latest' symlink to current run.

    Gracefully handles Windows where symlinks may fail.

    Args:
        runs_base: Base directory containing all runs (e.g., "runs/")
        run_id: ID of the current run (e.g., "2025-10-09_15-30-45")
    """
    latest_link = runs_base / "latest"
    # Use relative path for symlink target (just the run_id directory name)
    # This ensures the symlink works correctly when both are in the same directory
    target = Path(run_id)

    try:
        # Remove existing symlink/file
        if latest_link.exists() or latest_link.is_symlink():
            latest_link.unlink()

        # Create symlink with relative path
        latest_link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Windows may not support symlinks, skip gracefully
        if platform.system() != "Windows":
            raise


def ensure_run_structure(run_dir: Path, task_id: str) -> None:
    """Create necessary directories for a task run.

    Args:
        run_dir: Run directory (e.g., "runs/2025-10-09_15-30-45/")
        task_id: Task identifier (e.g., "hello_date")
    """
    get_task_run_dir(run_dir, task_id).mkdir(parents=True, exist_ok=True)
