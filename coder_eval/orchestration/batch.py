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
from .task_loader import expand_task_for_agents, load_task


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
    loaded_tasks: list[tuple[Path, TaskDefinition]] = []
    for task_file in task_files:
        task = load_task(task_file)

        # Apply task-level CLI overrides (apply to all tasks regardless of single/multi-agent)
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

        # Apply timeout overrides (CLI > task YAML)
        if config.task_timeout_seconds is not None:
            task.task_timeout_seconds = config.task_timeout_seconds

        # Apply agent-level overrides only for single-agent tasks.
        # Multi-agent tasks define per-agent config explicitly in the YAML.
        if task.agents is None:
            assert task.agent is not None  # guaranteed by task validation (either agent or agents must be set)
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

            if config.turn_timeout_seconds is not None:
                task.agent.turn_timeout_seconds = config.turn_timeout_seconds
        else:
            # Warn if agent-level CLI overrides are set but will be ignored for multi-agent tasks
            ignored: list[str] = []
            if config.agent_model is not None:
                ignored.append(f"--model={config.agent_model!r}")
            if config.permission_mode is not None:
                ignored.append(f"--permission-mode={config.permission_mode!r}")
            if config.max_turns is not None:
                ignored.append(f"--max-turns={config.max_turns}")
            if config.turn_timeout_seconds is not None:
                ignored.append(f"--turn-timeout={config.turn_timeout_seconds}")
            if ignored:
                msg = "Task %r is multi-agent; CLI flags %s are ignored (configure each agent in the YAML instead)"
                logger.warning(msg, task.task_id, ", ".join(ignored))

        loaded_tasks.append((task_file, task))

    # Filter tasks by tags (before expansion so tags apply to the whole task, not per-agent)
    loaded_tasks = filter_tasks_by_tags(
        loaded_tasks, include_tags=config.include_tags, exclude_tags=config.exclude_tags
    )

    # Expand multi-agent tasks into one entry per agent.
    # Each entry is (task_file, single-agent TaskDefinition, agent_name | None).
    # agent_name is None for single-agent tasks; it drives the nested run subdirectory.
    tasks: list[tuple[Path, TaskDefinition, str | None]] = []
    for task_file, task in loaded_tasks:
        if task.agents is not None:
            for expanded in expand_task_for_agents(task):
                expanded_agent = expanded.agent
                assert expanded_agent is not None  # guaranteed by expand_task_for_agents
                tasks.append((task_file, expanded, expanded_agent.name))
        else:
            tasks.append((task_file, task, None))

    # Validate no duplicate (task_id, agent_name) pairs (would cause result clobbering)
    _validate_unique_task_ids(tasks)

    # Create semaphore for concurrency control
    semaphore = asyncio.Semaphore(config.max_parallel)

    # Build a mapping of task_id -> tags for the summary
    task_tags: dict[str, list[str]] = {task.task_id: task.tags for _, task, _ in tasks}

    # Notify caller of final task count (after filtering and expansion)
    if on_batch_start is not None:
        on_batch_start(len(tasks))

    # Create coroutines for all tasks
    async def run_task_with_semaphore(task_file: Path, task: TaskDefinition, agent_name: str | None) -> dict[str, Any]:
        """Run single task with semaphore for concurrency control."""
        callback_key = f"{task.task_id}/{agent_name}" if agent_name else task.task_id
        task_callback = stream_callback_factory(callback_key) if stream_callback_factory else None
        async with semaphore:
            try:
                task_result = await _run_single_task(
                    orchestrator_class=Orchestrator,
                    task_file=task_file,
                    task=task,
                    run_dir=config.run_dir,
                    preserve=config.preserve_sandbox,
                    agent_name=agent_name,
                    stream_callback=task_callback,
                )
            except BaseException as exc:
                _safe_notify(
                    on_task_complete, _create_error_result(task_file, exc, task_id=task.task_id, agent_name=agent_name)
                )
                raise
            _safe_notify(on_task_complete, task_result)
            return task_result

    coroutines = [run_task_with_semaphore(task_file, task, agent_name) for task_file, task, agent_name in tasks]

    # Execute all tasks (with exception handling)
    results: list[dict[str, Any] | BaseException] = await asyncio.gather(*coroutines, return_exceptions=True)

    # Process results and handle exceptions
    # Note: `results` aligns with `tasks` (post-filter, post-expansion), not the original `task_files`
    processed_results: list[dict[str, Any]] = []
    for i, result in enumerate(results):
        if isinstance(result, BaseException):
            task_file, task, agent_name = tasks[i]
            error_result = _create_error_result(task_file, result, task_id=task.task_id, agent_name=agent_name)
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
    agent_name: str | None = None,
    stream_callback: StreamCallback | None = None,
) -> dict[str, Any]:
    """Run a single task as part of a batch (internal helper).

    Args:
        orchestrator_class: Orchestrator class to instantiate
        task_file: Path to task file (for logging/error reporting)
        task: Loaded task definition (always single-agent after expansion)
        run_dir: Run-level directory
        preserve: Whether to preserve sandbox
        agent_name: Agent name for multi-agent tasks; drives nested subdirectory.
            None for single-agent tasks → run_dir/task_id/
            Set for multi-agent tasks  → run_dir/task_id/agent_name/
        stream_callback: Optional streaming callback for this task

    Returns:
        Dictionary with {task_id, agent_name, result, duration}
    """
    # For multi-agent tasks nest under task_id/agent_name/; single-agent stays flat.
    task_run_dir = run_dir / task.task_id / agent_name if agent_name else run_dir / task.task_id
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
        "agent_name": agent_name,
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


def _create_error_result(
    task_file: Path,
    error: BaseException,
    *,
    task_id: str | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Create an error result for a failed task.

    Args:
        task_file: Path to task file that failed
        error: Exception that was raised
        task_id: Explicit task ID; falls back to task_file.stem when unavailable
        agent_name: Agent name for multi-agent tasks; None for single-agent

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
        "agent_name": agent_name,
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

    version_info = get_version_info()
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
                "agent_name": r.get("agent_name"),
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
                "installed_tools": r["result"].environment_info.get("installed_tools"),
            }
            for r in task_results
        ],
        framework_version=version_info.get("coder_eval", "unknown"),
        environment_info=version_info,
    )

    # Save run-summary.json
    summary_path = run_dir / "run-summary.json"
    summary_path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")

    # Generate run-report.md with command statistics
    report_md = ReportGenerator.generate_markdown(summary, run_dir=run_dir)
    report_path = run_dir / "run-report.md"
    report_path.write_text(report_md, encoding="utf-8")

    return summary


def _validate_unique_task_ids(tasks: list[tuple[Path, TaskDefinition, str | None]]) -> None:
    """Raise ValueError if any (task_id, agent_name) pairs are duplicated.

    For single-agent tasks agent_name is None; uniqueness is checked on task_id alone.
    For multi-agent tasks the pair (task_id, agent_name) must be unique across all files.
    """
    seen: dict[tuple[str, str | None], list[Path]] = {}
    for task_file, task, agent_name in tasks:
        key = (task.task_id, agent_name)
        seen.setdefault(key, []).append(task_file)
    duplicates = {k: files for k, files in seen.items() if len(files) > 1}
    if duplicates:
        lines = [
            f"  - task_id={tid!r}, agent={aname!r}: {', '.join(str(f) for f in files)}"
            for (tid, aname), files in duplicates.items()
        ]
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
