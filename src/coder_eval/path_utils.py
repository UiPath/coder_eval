"""Path utilities for run directory management."""

import os
import platform
from datetime import datetime
from pathlib import Path


TASK_LOG_FILENAME = "task.log"


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (tmp sibling + ``os.replace``).

    A SIGKILL mid-write would otherwise leave a truncated file that readers
    parse as malformed rather than as "absent" — the same discipline the
    orchestrator applies to task.json.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def task_log_path(run_dir: Path) -> Path:
    """Per-task log file path inside a task run directory."""
    return run_dir / TASK_LOG_FILENAME


def generate_run_id() -> str:
    """Generate filesystem-safe timestamp: YYYY-MM-DD_HH-MM-SS."""
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def replicate_subdir_name(replicate_index: int) -> str:
    """Two-digit, zero-padded directory name for a replicate (``'00'``, ``'01'``, ...).

    Two-digit padding caps unique replicate names at 100 (indices 0-99); if a
    follow-up PR ever needs >=100 replicates, widen the padding here.
    """
    return f"{replicate_index:02d}"


def build_task_run_dir(
    run_dir: Path,
    variant_id: str,
    task_id: str,
    replicate_index: int = 0,
) -> Path:
    """Build the per-task run dir: ``<run_dir>/<variant_id>/<task_id>/<NN>/``."""
    return run_dir / variant_id / task_id / replicate_subdir_name(replicate_index)


def format_task_log_id(variant_id: str, task_id: str, replicate_index: int = 0) -> str:
    """Canonical ``<variant_id>/<task_id>/<NN>`` identifier used by:
    - Orchestrator ``_log_task_id`` (console/file log tag, streaming events)
    - Batch ``stream_label``
    - CLI tqdm progress-bar postfix

    Shape mirrors ``build_task_run_dir`` (same three segments, same NN padding
    via ``replicate_subdir_name``) so log tags and on-disk paths stay in
    lockstep. Callers MUST use this helper rather than hand-rolling the
    f-string so future format changes (e.g., NN → NNN) touch exactly one
    place.
    """
    return f"{variant_id}/{task_id}/{replicate_subdir_name(replicate_index)}"


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
