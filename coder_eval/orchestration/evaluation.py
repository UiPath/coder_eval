"""Evaluation support functions for orchestrator.

This module provides helpers for:
- Generating feedback prompts based on success criteria results
- Creating iteration snapshots with hybrid mode support
- Loading reference code for comparison

These were extracted from Orchestrator to reduce its complexity while
maintaining the evaluation flow logic.
"""

import asyncio
import logging
from pathlib import Path

from ..errors.executor import execute_with_retry
from ..evaluation.reviewer import LLMReviewer
from ..models import CriteriaResults, SnapshotMode, TaskDefinition, TurnRecord
from ..sandbox import Sandbox


async def generate_next_prompt(
    task: TaskDefinition,
    agent_output: str,
    criteria_results: CriteriaResults,
    iteration: int,
    llm_reviewer: LLMReviewer | None,
    reference_code: str | None,
    logger: logging.Logger | logging.LoggerAdapter[logging.Logger],
) -> str:
    """Generate the next prompt based on results and feedback.

    Tries LLM review first if configured. Falls back to deterministic
    feedback listing failed criteria (those with score < pass_threshold).

    Args:
        task: Task definition with criteria and configuration
        agent_output: The agent's output from this turn
        criteria_results: Results of success criteria checks
        iteration: Current iteration number
        llm_reviewer: Optional LLM reviewer instance
        reference_code: Optional reference solution code
        logger: Logger for this task

    Returns:
        Next prompt to send to the agent with actionable feedback
    """
    # Try LLM review first if enabled
    if llm_reviewer:
        logger.info("Requesting LLM review")

        # Wrap LLM reviewer call with retry logic for network resilience
        async def _review_operation():
            assert llm_reviewer is not None
            return await asyncio.to_thread(
                llm_reviewer.review,
                task_description=task.description,
                agent_output=agent_output,
                current_iteration=iteration,
                max_iterations=task.max_iterations,
                reference_solution=reference_code,
            )

        decision = await execute_with_retry(
            operation=_review_operation,
            operation_name="LLM reviewer",
            context={
                "task_id": task.task_id,
                "component": "evaluator",
            },
        )

        if decision:
            logger.info(f"Issues:\n{decision.issues[:100]}...")
            logger.info(f"LLM Score: {decision.score}")

            if decision.next_steps:
                steps_text = "\n".join(f"- {s}" for s in decision.next_steps)
                return f"""The task is not yet complete. Here's the feedback:

Issues:
{decision.issues}

Next steps:
{steps_text}

Please address these issues and continue working on the task."""
            elif decision.issues:
                return f"""The task is not yet complete. Here's the feedback:

Issues:
{decision.issues}

Please address these issues and continue working on the task."""

    # Fallback to deterministic feedback from criteria
    logger.info("Using deterministic feedback from failed criteria")

    # Check which criteria failed their pass_threshold
    failed_criteria = [
        (result, criterion)
        for result, criterion in zip(criteria_results, task.success_criteria, strict=True)
        if result.score < criterion.pass_threshold
    ]

    if failed_criteria:
        feedback_parts = ["The following checks failed:\n"]
        for result, criterion in failed_criteria:
            feedback_parts.append(f"- {criterion.description}")
            feedback_parts.append(f"  Score: {result.score:.2f} (threshold: {criterion.pass_threshold})")
            if result.error:
                feedback_parts.append(f"  Error: {result.error}")
            elif result.details:
                feedback_parts.append(f"  Details: {result.details}")

        feedback_parts.append("\nPlease fix these issues and complete the task.")

        return "\n".join(feedback_parts)

    # Fallback message if no specific feedback (rare edge case)
    logger.warning(
        f"No specific failures detected but task did not pass (iteration {iteration}). "
        + "This may indicate an issue with success criteria configuration."
    )
    return "The task is not yet complete. Please continue working on it."


async def create_iteration_snapshot(
    sandbox: Sandbox,
    snapshot_base_dir: Path,
    task: TaskDefinition,
    iteration: int,
    turn_record: TurnRecord,
    logger: logging.Logger | logging.LoggerAdapter[logging.Logger],
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
        logger: Logger for this task
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
    logger: logging.Logger | logging.LoggerAdapter[logging.Logger],
) -> tuple[str | None, str | None]:
    """Load reference code from task definition.

    Args:
        task: Task definition with reference configuration
        task_file: Path to task YAML file (for resolving relative paths)
        cached_reference: Previously loaded reference code (for caching)
        logger: Logger for this task

    Returns:
        Tuple of (reference_code, cached_reference) where cached_reference
        should be stored for future calls

    Raises:
        FileNotFoundError: If reference file path doesn't exist
        ValueError: If task_file not provided when needed

    Security: Reference code is NEVER shown to the agent.
    It is only used by LLM reviewer and reference comparison criterion.
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
