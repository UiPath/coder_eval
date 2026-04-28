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


def load_reference_code(
    task: TaskDefinition,
    task_file: Path | None,
    cached_reference: str | None,
) -> tuple[str | None, str | None]:
    """Load reference code from task definition.

    Args:
        task: Task definition with reference configuration
        task_file: Path to task YAML file (for resolving relative paths)
        cached_reference: Previously loaded reference code (for caching)

    Returns:
        Tuple of (reference_code, cached_reference) where cached_reference
        should be stored for future calls

    Raises:
        FileNotFoundError: If reference file path doesn't exist
        ValueError: If task_file not provided when needed

    Security: Reference code is NEVER shown to the agent.
    It is only used by llm_judge / agent_judge and reference_comparison criteria.
    """
    # Return cached if already loaded
    if cached_reference is not None:
        return cached_reference, cached_reference

    if not task.reference:
        return None, None

    reference_code: str | None = None

    if task.reference.code:
        # Inline code
        reference_code = task.reference.code
    elif task.reference.file:
        # Load from file (resolve relative to task YAML location)
        if not task_file:
            raise ValueError("task_file not set, cannot resolve reference file path")
        ref_path = task_file.parent / task.reference.file
        if not ref_path.exists():
            raise FileNotFoundError(f"Reference file not found: {ref_path} (specified in {task_file})")
        reference_code = ref_path.read_text()

    # Log that reference was loaded (but NOT the content for security)
    logger.info("Reference solution loaded (content hidden for security)")
    return reference_code, reference_code
