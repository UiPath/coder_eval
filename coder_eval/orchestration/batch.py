"""Batch execution support for running multiple tasks in parallel.

This module provides functions for orchestrating the execution of multiple evaluation tasks,
managing concurrency, exception handling, and result aggregation.

These were extracted from Orchestrator to reduce its complexity while maintaining
the batch execution flow logic.
"""

# pyright: reportImportCycles=false

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import AgentKind, EvaluationResult, RunSummary, SnapshotMode, TaskDefinition
from ..streaming.callbacks import StreamCallback
from ..utils import get_version_info
from .config import BatchRunConfig
from .task_loader import load_task


logger = logging.getLogger(__name__)


async def run_batch(
    task_files: list[Path],
    config: BatchRunConfig,
    on_task_complete: Callable[[dict[str, Any]], None] | None = None,
    on_batch_start: Callable[[int], None] | None = None,
    stream_callback_factory: Callable[[str], StreamCallback] | None = None,
) -> RunSummary:
    """Run multiple tasks in batch with optional parallelism.

    This function orchestrates the execution of multiple evaluation tasks,
    managing concurrency, exception handling, and result aggregation.
    Returns a complete RunSummary with all results and statistics.

    Args:
        task_files: List of paths to task YAML files
        config: Batch execution configuration
        on_task_complete: Optional callback invoked after each task finishes,
            receives the task result dict with keys {task_id, result, duration}
        on_batch_start: Optional callback invoked after task loading and filtering,
            receives the final count of tasks that will be executed

    Returns:
        RunSummary with aggregated results and statistics

    Raises:
        FileNotFoundError: If task files don't exist
        ValueError: If task files are invalid or contain duplicate task IDs

    Example:
        >>> config = BatchRunConfig(
        ...     run_dir=Path("runs/my-run"),
        ...     max_parallel=3,
        ...     preserve_sandbox=True,
        ... )
        >>> summary = await run_batch(
        ...     task_files=[Path("task1.yaml"), Path("task2.yaml")],
        ...     config=config,
        ... )
        >>> print(f"Success: {summary.tasks_succeeded}/{summary.tasks_run}")
    """
    # Import here to avoid circular imports
    from ..config import settings as app_settings
    from ..orchestrator import Orchestrator

    start_time = datetime.now()

    # Load all tasks first (fail fast if any are invalid)
    tasks: list[tuple[Path, TaskDefinition]] = []
    for task_file in task_files:
        task = load_task(task_file)

        # Apply CLI overrides
        if config.max_iterations is not None:
            task.max_iterations = config.max_iterations

        # Apply snapshot overrides (consolidated logic)
        if config.snapshot_mode or config.snapshot_checkpoint_freq:
            from ..models import SnapshotConfig

            # Determine mode: use override if provided, otherwise preserve existing
            mode = SnapshotMode(config.snapshot_mode.lower()) if config.snapshot_mode else task.sandbox.snapshots.mode

            # Determine checkpoint frequency: use override if provided, otherwise preserve existing
            checkpoint_freq = (
                config.snapshot_checkpoint_freq
                if config.snapshot_checkpoint_freq is not None
                else task.sandbox.snapshots.checkpoint_frequency
            )

            # Create new snapshot config with overridden values
            task.sandbox.snapshots = SnapshotConfig(
                mode=mode,
                checkpoint_frequency=checkpoint_freq,
                ignore_patterns=task.sandbox.snapshots.ignore_patterns,  # Preserve task-specific patterns
            )

        # Apply agent overrides (CLI > .env > task YAML)
        effective_model = config.agent_model if config.agent_model is not None else app_settings.default_agent_model
        if effective_model is not None:
            task.agent.model = effective_model

        effective_perm = (
            config.permission_mode if config.permission_mode is not None else app_settings.default_permission_mode
        )
        if effective_perm is not None:
            task.agent.permission_mode = effective_perm  # type: ignore[assignment]  # validated by Pydantic via validate_assignment

        effective_max_turns = config.max_turns if config.max_turns is not None else app_settings.default_max_turns
        if effective_max_turns is not None:
            task.agent.max_turns = effective_max_turns

        # Apply timeout overrides (CLI > task YAML)
        if config.task_timeout_seconds is not None:
            task.task_timeout_seconds = config.task_timeout_seconds

        if config.turn_timeout_seconds is not None:
            task.agent.turn_timeout_seconds = config.turn_timeout_seconds

        tasks.append((task_file, task))

    # Filter tasks by tags
    tasks = filter_tasks_by_tags(tasks, include_tags=config.include_tags, exclude_tags=config.exclude_tags)

    # Validate no duplicate task IDs (would cause result clobbering)
    _validate_unique_task_ids(tasks)

    # Create semaphore for concurrency control
    semaphore = asyncio.Semaphore(config.max_parallel)

    # Build a mapping of task_id -> tags for the summary
    task_tags: dict[str, list[str]] = {task.task_id: task.tags for _, task in tasks}

    # Notify caller of final task count (after filtering)
    if on_batch_start is not None:
        on_batch_start(len(tasks))

    # Create coroutines for all tasks
    async def run_task_with_semaphore(task_file: Path, task: TaskDefinition) -> dict[str, Any]:
        """Run single task with semaphore for concurrency control."""
        task_callback = stream_callback_factory(task.task_id) if stream_callback_factory else None
        async with semaphore:
            try:
                task_result = await _run_single_task(
                    orchestrator_class=Orchestrator,
                    task_file=task_file,
                    task=task,
                    run_dir=config.run_dir,
                    preserve=config.preserve_sandbox,
                    stream_callback=task_callback,
                )
            except BaseException as exc:
                _safe_notify(on_task_complete, _create_error_result(task_file, exc, task_id=task.task_id))
                raise
            _safe_notify(on_task_complete, task_result)
            return task_result

    coroutines = [run_task_with_semaphore(task_file, task) for task_file, task in tasks]

    # Execute all tasks (with exception handling)
    results: list[dict[str, Any] | BaseException] = await asyncio.gather(*coroutines, return_exceptions=True)

    # Process results and handle exceptions
    # Note: `results` aligns with `tasks` (post-filter), not the original `task_files`
    processed_results: list[dict[str, Any]] = []
    for i, result in enumerate(results):
        if isinstance(result, BaseException):
            task_file, task = tasks[i]
            error_result = _create_error_result(task_file, result, task_id=task.task_id)
            processed_results.append(error_result)
        else:
            processed_results.append(result)

    end_time = datetime.now()

    # Generate and return summary (all-in-one)
    return _generate_run_summary(config.run_dir, processed_results, start_time, end_time, task_tags)


async def _run_single_task(
    orchestrator_class: type,
    task_file: Path,
    task: TaskDefinition,
    run_dir: Path,
    preserve: bool,
    stream_callback: StreamCallback | None = None,
) -> dict[str, Any]:
    """Run a single task as part of a batch (internal helper).

    Args:
        orchestrator_class: Orchestrator class to instantiate
        task_file: Path to task file (for logging/error reporting)
        task: Loaded task definition
        run_dir: Run-level directory
        preserve: Whether to preserve sandbox
        stream_callback: Optional streaming callback for this task

    Returns:
        Dictionary with {task_id, result, duration}
    """
    # Create per-task subdirectory
    task_run_dir = run_dir / task.task_id
    task_run_dir.mkdir(parents=True, exist_ok=True)

    # Create orchestrator for single task
    orchestrator = orchestrator_class(
        task=task,
        run_dir=task_run_dir,
        preserve_sandbox=preserve,
        task_file=task_file,
        stream_callback=stream_callback,
    )

    # Run evaluation
    result = await orchestrator.run()

    return {
        "task_id": task.task_id,
        "result": result,
        "duration": result.duration_seconds,
    }


def _safe_notify(callback: Callable[[dict[str, Any]], None] | None, result: dict[str, Any]) -> None:
    """Invoke a progress callback, swallowing any exceptions so UI failures never affect task outcomes."""
    if callback is None:
        return
    try:
        callback(result)
    except Exception:
        logger.warning("Progress callback failed (ignored)", exc_info=True)


def _create_error_result(task_file: Path, error: BaseException, *, task_id: str | None = None) -> dict[str, Any]:
    """Create an error result for a failed task.

    Args:
        task_file: Path to task file that failed
        error: Exception that was raised
        task_id: Explicit task ID; falls back to task_file.stem when unavailable

    Returns:
        Dictionary with error result in same format as successful results
    """
    # Include exception type for better triage
    error_type = type(error).__name__
    resolved_task_id = task_id if task_id is not None else task_file.stem
    error_result = EvaluationResult(
        task_id=resolved_task_id,
        task_description=f"Failed to load task from {task_file}: {error_type}",
        agent_type=AgentKind.UNKNOWN,  # Agent type unknown when task loading fails
        started_at=datetime.now(),
        final_status="ERROR",
        error_message=str(error),
        iteration_count=0,
        environment_info={},
    )
    return {
        "task_id": error_result.task_id,
        "result": error_result,
        "duration": 0.0,
    }


def _extract_reference_similarity(result: EvaluationResult) -> float | None:
    """Extract reference_comparison score from criteria results, if present."""
    for cr in result.success_criteria_results:
        if cr.criterion_type == "reference_comparison":
            return cr.score
    return None


def _generate_run_summary(
    run_dir: Path,
    task_results: list[dict[str, Any]],
    start_time: datetime,
    end_time: datetime,
    task_tags: dict[str, list[str]] | None = None,
) -> RunSummary:
    """Generate run-level summary from batch results.

    Args:
        run_dir: Run directory path
        task_results: List of task result dictionaries
        start_time: Batch start time
        end_time: Batch end time
        task_tags: Optional mapping of task_id -> tags

    Returns:
        RunSummary with aggregated statistics
    """
    from ..reports import ReportGenerator

    # Create run directory first to eliminate race condition
    run_dir.mkdir(parents=True, exist_ok=True)

    statuses = [r["result"].final_status for r in task_results]

    summary = RunSummary(
        run_id=run_dir.name,
        start_time=start_time,
        end_time=end_time,
        total_duration_seconds=(end_time - start_time).total_seconds(),
        tasks_run=len(task_results),
        tasks_succeeded=statuses.count("SUCCESS"),
        tasks_failed=statuses.count("FAILURE"),
        tasks_error=statuses.count("ERROR"),
        task_results=[
            {
                "task_id": r["task_id"],
                "status": r["result"].final_status,
                "weighted_score": r["result"].weighted_score,
                "duration": r["duration"],
                "iteration_count": r["result"].iteration_count,
                "tags": (task_tags or {}).get(r["task_id"], []),
                "turns": [
                    {
                        "iteration": t.iteration,
                        "duration_seconds": t.duration_seconds,
                        "command_count": len(t.commands),
                        "assistant_turn_count": t.assistant_turn_count,
                    }
                    for t in r["result"].turns
                ],
                "model_used": r["result"].model_used,
                "reference_similarity": _extract_reference_similarity(r["result"]),
                "total_tokens": (r["result"].total_token_usage.total_tokens if r["result"].total_token_usage else None),
                "total_cost_usd": (
                    r["result"].total_token_usage.total_cost_usd if r["result"].total_token_usage else None
                ),
                "agent_config": (r["result"].agent_config.model_dump() if r["result"].agent_config else None),
                "sdk_options": r["result"].sdk_options,
            }
            for r in task_results
        ],
        framework_version=get_version_info().get("coder_eval", "unknown"),
        environment_info=get_version_info(),
    )

    # Save run-summary.json
    summary_path = run_dir / "run-summary.json"
    summary_path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")

    # Generate run-report.md with command statistics
    report_md = ReportGenerator.generate_markdown(summary, run_dir=run_dir)
    report_path = run_dir / "run-report.md"
    report_path.write_text(report_md, encoding="utf-8")

    return summary


def _validate_unique_task_ids(tasks: list[tuple[Path, TaskDefinition]]) -> None:
    """Raise ValueError if any tasks share the same task_id."""
    seen: dict[str, list[Path]] = {}
    for task_file, task in tasks:
        seen.setdefault(task.task_id, []).append(task_file)
    duplicates = {tid: files for tid, files in seen.items() if len(files) > 1}
    if duplicates:
        lines = [f"  - '{tid}': {', '.join(str(f) for f in files)}" for tid, files in duplicates.items()]
        raise ValueError("Duplicate task IDs found:\n" + "\n".join(lines))


def filter_tasks_by_tags(
    tasks: list[tuple[Path, TaskDefinition]],
    include_tags: set[str] | None = None,
    exclude_tags: set[str] | None = None,
) -> list[tuple[Path, TaskDefinition]]:
    """Filter tasks by tag inclusion/exclusion (OR logic).

    Args:
        tasks: List of (task_file, task_definition) tuples
        include_tags: If set, only keep tasks matching ANY of these tags
        exclude_tags: If set, remove tasks matching ANY of these tags

    Returns:
        Filtered list of (task_file, task_definition) tuples
    """
    result = tasks
    if include_tags:
        result = [(p, t) for p, t in result if include_tags & set(t.tags)]
        skipped = len(tasks) - len(result)
        if skipped:
            logger.info("Tag filter: included %d/%d tasks (tags: %s)", len(result), len(tasks), ", ".join(include_tags))
    if exclude_tags:
        before = len(result)
        result = [(p, t) for p, t in result if not (exclude_tags & set(t.tags))]
        skipped = before - len(result)
        if skipped:
            logger.info("Tag filter: excluded %d tasks (tags: %s)", skipped, ", ".join(exclude_tags))
    return result
