"""Batch execution support for running resolved tasks in parallel.

This module provides the unified run_batch() function that accepts pre-resolved
tasks (list[ResolvedTask]) and executes them with concurrency control,
exception handling, and result aggregation.

Task loading, config resolution, and CLI override application are handled
upstream by resolve_all_tasks() in experiment.py.
"""

# pyright: reportImportCycles=false

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ..models import (
    AgentKind,
    EvaluationResult,
    FinalStatus,
    ResolvedTask,
    RunSummary,
    SkippedTask,
    TaskDefinition,
    TaskResult,
)
from ..path_utils import format_task_log_id
from ..reports_experiment import eval_result_to_task_dict
from ..streaming.callbacks import StreamCallback
from ..utils import get_version_info
from .config import BatchRunConfig


logger = logging.getLogger(__name__)


async def run_batch(
    resolved_tasks: list[ResolvedTask],
    config: BatchRunConfig,
    on_task_complete: Callable[[TaskResult], None] | None = None,
    on_batch_start: Callable[[int], None] | None = None,
    stream_callback_factory: Callable[[str], StreamCallback] | None = None,
    skipped_tasks: list[SkippedTask] | None = None,
) -> tuple[RunSummary, list[TaskResult]]:
    """Run resolved tasks in batch with optional parallelism.

    Tasks must be fully resolved (all config layers applied, tag filtering done).
    This function is a pure executor — no configuration or loading logic.

    Args:
        resolved_tasks: List of fully-resolved tasks from resolve_all_tasks.
        config: Batch configuration (max_parallel, preserve_sandbox, run_dir).
        on_task_complete: Optional callback invoked after each task finishes.
        on_batch_start: Optional callback invoked with the final task count.
        stream_callback_factory: Optional factory for streaming callbacks.
        skipped_tasks: Task YAMLs that failed to load upstream and should be
            recorded in the run summary (informational; they don't run).

    Returns:
        Tuple of (RunSummary, list[TaskResult]).
    """
    from ..orchestrator import Orchestrator

    start_time = datetime.now()

    if on_batch_start is not None:
        on_batch_start(len(resolved_tasks))

    semaphore = asyncio.Semaphore(config.max_parallel)
    task_tags: dict[str, list[str]] = {rt.task.task_id: rt.task.tags for rt in resolved_tasks}
    task_paths: dict[str, str] = {rt.task.task_id: str(rt.task_file) for rt in resolved_tasks}

    async def run_single(rt: ResolvedTask) -> TaskResult:
        """Run a single resolved task with semaphore for concurrency control."""
        stream_label = format_task_log_id(rt.variant_id, rt.task.task_id, rt.replicate_index)
        task_callback = stream_callback_factory(stream_label) if stream_callback_factory else None
        async with semaphore:
            try:
                rt.run_dir.mkdir(parents=True, exist_ok=True)  # noqa: CE002 — mkdir on local FS is nanoseconds
                sandbox_cfg = rt.task.sandbox
                if sandbox_cfg is not None and sandbox_cfg.driver == "docker":
                    # Docker isolation: spawn one container per task, parse
                    # its task.json on completion. The in-container CLI
                    # serializes stream events as NDJSON on stdout; we
                    # forward them to the host callback so --stream
                    # renders identically to the in-process path.
                    from ..isolation.docker_runner import DockerRunner

                    result = await DockerRunner(
                        rt,
                        preserve_sandbox=config.preserve_sandbox,
                        stream_callback=task_callback,
                        verbose=config.verbose,
                    ).run()
                else:
                    orchestrator = Orchestrator(
                        task=rt.task,
                        run_dir=rt.run_dir,
                        preserve_sandbox=config.preserve_sandbox,
                        task_file=rt.task_file,
                        stream_callback=task_callback,
                        variant_id=rt.variant_id,
                        source_yaml=rt.source_yaml,
                        config_lineage=rt.config_lineage,
                        replicate_index=rt.replicate_index,
                    )
                    result = await orchestrator.run()
                tr = TaskResult(
                    task_id=rt.task.task_id,
                    variant_id=rt.variant_id,
                    result=result,
                    duration=result.duration_seconds,
                    suite_id=rt.task.suite_id,
                    row_id=rt.task.row_id,
                    replicate_index=rt.replicate_index,
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                tr = _create_error_task_result(
                    rt.task_file,
                    exc,
                    task_id=rt.task.task_id,
                    variant_id=rt.variant_id,
                    suite_id=rt.task.suite_id,
                    row_id=rt.task.row_id,
                    replicate_index=rt.replicate_index,
                )
            _safe_notify(on_task_complete, tr)
            return tr

    coroutines = [run_single(rt) for rt in resolved_tasks]
    results: list[TaskResult | BaseException] = await asyncio.gather(*coroutines, return_exceptions=True)

    # Re-raise fatal exceptions before processing results
    for result in results:
        if isinstance(result, (KeyboardInterrupt, SystemExit)):
            raise result

    processed: list[TaskResult] = []
    for i, result in enumerate(results):
        if isinstance(result, BaseException):
            rt = resolved_tasks[i]
            processed.append(
                _create_error_task_result(
                    rt.task_file,
                    result,
                    task_id=rt.task.task_id,
                    variant_id=rt.variant_id,
                    suite_id=rt.task.suite_id,
                    row_id=rt.task.row_id,
                    replicate_index=rt.replicate_index,
                )
            )
        else:
            processed.append(result)

    end_time = datetime.now()
    summary = _generate_run_summary(
        config.run_dir,
        processed,
        start_time,
        end_time,
        task_tags,
        task_paths=task_paths,
        max_parallel=config.max_parallel,
        skipped_tasks=skipped_tasks or [],
    )
    return summary, processed


def _safe_notify(callback: Callable[[TaskResult], None] | None, result: TaskResult) -> None:
    """Invoke a progress callback, swallowing any exceptions so UI failures never affect task outcomes."""
    if callback is None:
        return
    try:
        callback(result)
    except Exception:
        logger.warning("Progress callback failed (ignored)", exc_info=True)


def _create_error_task_result(
    task_file: Path,
    error: BaseException,
    *,
    task_id: str | None = None,
    variant_id: str,
    suite_id: str | None = None,
    row_id: str | None = None,
    replicate_index: int = 0,
) -> TaskResult:
    """Create a TaskResult for a failed task.

    Args:
        task_file: Path to task file that failed.
        error: Exception that was raised.
        task_id: Explicit task ID; falls back to task_file.stem when unavailable.
        variant_id: Experiment variant ID.
        suite_id: Parent suite id (dataset expansion).
        row_id: Row id within the suite.
        replicate_index: Replicate index from ResolvedTask.

    Returns:
        TaskResult with error information.
    """
    error_type = type(error).__name__
    error_result = EvaluationResult(
        task_id=task_id if task_id is not None else task_file.stem,
        task_description=f"Failed to load task from {task_file}: {error_type}",
        variant_id=variant_id,
        agent_type=AgentKind.UNKNOWN,
        started_at=datetime.now(),
        final_status=FinalStatus.ERROR,
        error_message=str(error),
        iteration_count=0,
        environment_info={},
    )
    return TaskResult(
        task_id=error_result.task_id,
        variant_id=error_result.variant_id,
        result=error_result,
        duration=0.0,
        suite_id=suite_id,
        row_id=row_id,
        replicate_index=replicate_index,
    )


def _generate_run_summary(
    run_dir: Path,
    task_results: list[TaskResult],
    start_time: datetime,
    end_time: datetime,
    task_tags: dict[str, list[str]] | None = None,
    *,
    task_paths: dict[str, str] | None = None,
    max_parallel: int = 1,
    skipped_tasks: list[SkippedTask] | None = None,
) -> RunSummary:
    """Generate run-level summary from batch results.

    Args:
        run_dir: Run directory path.
        task_results: List of typed task results.
        start_time: Batch start time.
        end_time: Batch end time.
        task_tags: Optional mapping of task_id -> tags.
        task_paths: Optional mapping of task_id -> source YAML path (string).
        skipped_tasks: Task YAMLs that failed to load upstream.

    Returns:
        RunSummary with aggregated statistics.
    """
    from ..reports import ReportGenerator

    # Create run directory first to eliminate race condition
    run_dir.mkdir(parents=True, exist_ok=True)

    statuses = [r.result.final_status for r in task_results]

    version_info = get_version_info()
    host_coder_eval = version_info.get("coder_eval", "unknown")
    # Surface host↔container version drift: under --driver docker the agent
    # ran against the image's version, not the host's. Without this warning
    # framework_version silently mis-attributes the runtime.
    container_versions = {
        (r.result.environment_info or {}).get("coder_eval") for r in task_results if r.result.environment_info
    }
    container_versions.discard(None)
    container_versions.discard(host_coder_eval)
    if container_versions:
        logger.warning(
            "Container coder_eval %s != host %s; framework_version is host. Per-task versions in task.json.",
            sorted(v for v in container_versions if v),
            host_coder_eval,
        )
    summary = RunSummary(
        run_id=run_dir.name,
        start_time=start_time,
        end_time=end_time,
        total_duration_seconds=(end_time - start_time).total_seconds(),
        tasks_run=len(task_results),
        tasks_succeeded=sum(1 for s in statuses if s.category == "succeeded"),
        tasks_failed=sum(1 for s in statuses if s.category == "failed"),
        tasks_error=sum(1 for s in statuses if s.category == "error"),
        tasks_token_budget_exceeded=sum(1 for s in statuses if s == FinalStatus.TOKEN_BUDGET_EXCEEDED),
        tasks_cost_budget_exceeded=sum(1 for s in statuses if s == FinalStatus.COST_BUDGET_EXCEEDED),
        skipped_tasks=skipped_tasks or [],
        max_parallel=max_parallel,
        task_results=[
            eval_result_to_task_dict(
                r.result,
                variant_id=r.variant_id,
                tags=(task_tags or {}).get(r.task_id, []),
                task_path=(task_paths or {}).get(r.task_id),
                duration_override=r.duration,
            )
            for r in task_results
        ],
        framework_version=version_info.get("coder_eval", "unknown"),
        environment_info=version_info,
    )

    # Save run.json (run-level summary — distinct from experiment.json written by ExperimentReportGenerator)
    summary_path = run_dir / "run.json"
    summary_path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")

    # Generate run.md with command statistics
    report_md = ReportGenerator.generate_markdown(summary, run_dir=run_dir)
    report_path = run_dir / "run.md"
    report_path.write_text(report_md, encoding="utf-8")

    return summary


def filter_tasks_by_tags(
    tasks: list[tuple[Path, TaskDefinition]],
    include_tags: set[str] | None = None,
    exclude_tags: set[str] | None = None,
) -> list[tuple[Path, TaskDefinition]]:
    """Filter tasks by tag inclusion/exclusion (OR logic).

    Args:
        tasks: List of (task_file, task_definition) tuples.
        include_tags: If set, only keep tasks matching ANY of these tags.
        exclude_tags: If set, remove tasks matching ANY of these tags.

    Returns:
        Filtered list of (task_file, task_definition) tuples.
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
