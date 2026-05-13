"""Evaluation support functions for orchestrator.

This module provides helpers for:
- Creating iteration snapshots with hybrid mode support
- Loading reference code for comparison

These were extracted from Orchestrator to reduce its complexity while
maintaining the evaluation flow logic.
"""

import asyncio
import logging
from pathlib import Path

from ..models import SnapshotMode, TaskDefinition, TurnRecord
from ..sandbox import Sandbox


logger = logging.getLogger(__name__)


async def create_iteration_snapshot(
    sandbox: Sandbox,
    snapshot_base_dir: Path,
    task: TaskDefinition,
    iteration: int,
    turn_record: TurnRecord,
) -> None:
    """Create a snapshot of the sandbox after this iteration.

    Implements hybrid mode: full snapshots at checkpoints, incremental otherwise.
    Gracefully handles errors to prevent snapshot failures from breaking evaluation.

    Args:
        sandbox: Sandbox instance for accessing file state
        snapshot_base_dir: Base directory for snapshot storage
        task: Task definition with snapshot configuration
        iteration: Current iteration number (1-indexed)
        turn_record: TurnRecord to update with snapshot info
    """
    snapshot_config = task.sandbox.snapshots
    if snapshot_config.mode == SnapshotMode.DISABLED:
        return

    try:
        # Determine snapshot mode for this iteration
        snapshot_dir = snapshot_base_dir / f"iteration_{iteration}"

        # Hybrid mode: full at checkpoints, incremental otherwise
        if snapshot_config.mode == SnapshotMode.HYBRID:
            is_checkpoint = iteration % snapshot_config.checkpoint_frequency == 0
            mode = SnapshotMode.FULL if is_checkpoint else SnapshotMode.INCREMENTAL
        else:
            # Use configured mode directly (FULL or INCREMENTAL)
            mode = snapshot_config.mode

        # Create snapshot
        logger.debug(f"Creating {mode.value} snapshot for iteration {iteration}")

        manifest = await sandbox.create_snapshot(
            snapshot_dir=snapshot_dir,
            mode=mode,
            changed_files=turn_record.files_changed if mode == SnapshotMode.INCREMENTAL else None,
            ignore_patterns=snapshot_config.ignore_patterns,
        )

        # Update manifest with correct iteration number
        manifest.iteration = iteration

        # Update turn record with snapshot info
        turn_record.snapshot_path = str(snapshot_dir)
        turn_record.snapshot_size_bytes = manifest.size_bytes

        logger.info(
            f"Snapshot created: {manifest.file_count} files, {manifest.size_bytes / 1024:.1f} KB, mode={mode.value}"
        )

    except asyncio.CancelledError:
        # Re-raise to allow proper task cancellation
        raise
    except Exception as e:
        # Log error but don't fail the evaluation
        logger.warning(f"Failed to create snapshot for iteration {iteration}: {e}")
        # Don't set snapshot_path on turn_record if snapshot failed


def load_reference(
    task: TaskDefinition,
    task_file: Path | None,
    cached_reference: str | None,
) -> tuple[str | None, Path | None, str | None]:
    """Load reference solution from task definition.

    Returns the reference in whichever form the task declared:
    ``code`` / ``file`` produce a string ``reference_code``; ``directory``
    produces a resolved ``reference_dir`` ``Path``. At most one is non-None
    on any given call.

    Args:
        task: Task definition with reference configuration
        task_file: Path to task YAML file (for resolving relative paths)
        cached_reference: Previously loaded ``reference_code`` (for caching).
            Directory paths are resolved fresh each call — path resolution
            is cheap and the directory contents are read by the consumer.

    Returns:
        Tuple of (reference_code, reference_dir, cached_reference).
        ``cached_reference`` should be stored for future calls (string forms only).

    Raises:
        FileNotFoundError: if the reference file or directory doesn't exist.
        ValueError: if ``task_file`` is not provided when needed for path resolution.

    Security: the reference is NEVER shown to the agent. It is only consumed
    by ``llm_judge`` / ``agent_judge`` and ``reference_comparison`` criteria.
    """
    # String-form cache short-circuit. Directory form is resolved fresh below
    # because Path resolution is nanoseconds and directory content varies.
    if cached_reference is not None:
        return cached_reference, None, cached_reference

    if not task.reference:
        return None, None, None

    reference_code: str | None = None
    reference_dir: Path | None = None

    if task.reference.code:
        reference_code = task.reference.code
    elif task.reference.file:
        if not task_file:
            raise ValueError("task_file not set, cannot resolve reference file path")
        ref_path = task_file.parent / task.reference.file
        if not ref_path.exists():
            raise FileNotFoundError(f"Reference file not found: {ref_path} (specified in {task_file})")
        reference_code = ref_path.read_text(encoding="utf-8")
    elif task.reference.directory:
        if not task_file:
            raise ValueError("task_file not set, cannot resolve reference directory path")
        ref_dir = (task_file.parent / task.reference.directory).resolve()
        if not ref_dir.is_dir():
            raise FileNotFoundError(f"Reference directory not found: {ref_dir} (specified in {task_file})")
        reference_dir = ref_dir

    # Log that reference was loaded (but NOT the content for security)
    logger.info("Reference solution loaded (content hidden for security)")
    return reference_code, reference_dir, reference_code


# Backward-compat alias — orchestrator.py still imports this name in places we
# didn't yet update. New code should use ``load_reference``.
def load_reference_code(
    task: TaskDefinition,
    task_file: Path | None,
    cached_reference: str | None,
) -> tuple[str | None, str | None]:
    """Compatibility wrapper around ``load_reference`` returning the legacy two-tuple.

    Use ``load_reference`` directly when you also need the directory path
    (i.e. anywhere agent_judge can run). Kept so existing call sites that
    only consume the string form (``reference_comparison``,
    pre-directory-feature paths) don't have to change.
    """
    code, _dir, cache = load_reference(task=task, task_file=task_file, cached_reference=cached_reference)
    return code, cache
