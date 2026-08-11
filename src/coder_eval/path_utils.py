"""Path utilities for run directory management."""

import platform
import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path


TASK_LOG_FILENAME = "task.log"


def ignore_patterns_and_symlinks(patterns: list[str]) -> Callable[[str, list[str]], set[str]]:
    """``copytree`` ``ignore`` callable that drops pattern matches AND every symlink.

    Symlinks in a copied tree — whether malicious or accidental — are rejected
    rather than dereferenced into the destination, which would leak host files
    (e.g. a ``creds -> /root/.aws/credentials`` plant) into a judge workspace or
    a staged reference directory.

    Shared by ``evaluation.sub_agent`` (sandbox → judge workspace copies) and
    ``orchestration.evaluation`` (reference → per-run staged copy) so the
    no-symlinks rule cannot drift between the two.
    """
    pattern_ignore = shutil.ignore_patterns(*patterns)

    def _ignore(src: str, names: list[str]) -> set[str]:
        ignored = set(pattern_ignore(src, names))
        src_path = Path(src)
        for name in names:
            if name in ignored:
                continue
            if (src_path / name).is_symlink():
                ignored.add(name)
        return ignored

    return _ignore


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
