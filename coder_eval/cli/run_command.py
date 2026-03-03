"""Run command - execute evaluation tasks."""

import asyncio
import sys
from pathlib import Path
from typing import Any

import typer
from tqdm import tqdm

from ..config import settings
from ..logging_config import setup_logging
from ..orchestration.config import BatchRunConfig
from ..orchestrator import Orchestrator
from ..path_utils import create_latest_symlink
from .run_helpers import (
    expand_task_files,
    prepare_run_directory,
    print_execution_mode,
    print_execution_summary,
)


def run_command(
    task_files: list[Path] = typer.Argument(  # noqa: B008
        ...,
        help="Path(s) to task YAML file(s). Supports glob patterns.",
        exists=True,
    ),
    max_iterations: int | None = typer.Option(
        None,
        "--max-iter",
        "-i",
        help="Override max iterations for all tasks",
    ),
    preserve: bool = typer.Option(
        True,
        "--preserve/--no-preserve",
        "-p/-P",
        help="Preserve sandbox after execution (default: preserve)",
    ),
    run_dir: Path | None = typer.Option(  # noqa: B008
        None,
        "--run-dir",
        help="Custom run directory (default: auto-generated timestamped directory in runs/)",
    ),
    max_parallel: int = typer.Option(
        1,
        "--max-parallel",
        "-j",
        help="Maximum number of tasks to run concurrently (default: 1 = sequential)",
        min=1,
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose (DEBUG level) logging",
    ),
    log_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--log-file",
        help="Log to file in addition to console",
    ),
    snapshot_mode: str | None = typer.Option(
        None,
        "--snapshot-mode",
        help="Override snapshot mode for all tasks (disabled/full/incremental/hybrid)",
    ),
    snapshot_checkpoint_freq: int | None = typer.Option(
        None,
        "--snapshot-checkpoint-freq",
        help="Checkpoint frequency for hybrid mode (default: 5)",
        min=1,
    ),
    tags: str | None = typer.Option(
        None,
        "--tags",
        "-t",
        help="Only run tasks matching any of these tags (comma-separated, e.g., 'smoke,golden')",
    ),
    exclude_tags: str | None = typer.Option(
        None,
        "--exclude-tags",
        help="Skip tasks matching any of these tags (comma-separated, e.g., 'example,integration')",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        "-m",
        help="Override agent model for all tasks (e.g., claude-sonnet-4-20250514)",
    ),
    permission_mode: str | None = typer.Option(
        None,
        "--permission-mode",
        help="Override permission mode for all tasks (default/acceptEdits/plan/bypassPermissions)",
    ),
    max_turns: int | None = typer.Option(
        None,
        "--max-turns",
        help="Override max agent inner-loop turns per iteration for all tasks",
        min=1,
    ),
) -> None:
    """Run evaluation tasks (optionally in parallel).

    Sandboxes are preserved by default for debugging. Use --no-preserve to clean up.

    Examples:

        coder-eval run tasks/hello_date.yaml

        coder-eval run tasks/*.yaml --no-preserve

        coder-eval run tasks/task1.yaml tasks/task2.yaml --max-iter 5

        coder-eval run tasks/*.yaml --run-dir ./my-custom-run

        coder-eval run tasks/*.yaml --max-parallel 3

        coder-eval run tasks/*.yaml --verbose --log-file debug.log

        coder-eval run tasks/*.yaml --tags smoke

        coder-eval run tasks/*.yaml --tags golden,basic --exclude-tags example
    """
    # Validate permission mode early for clear error message
    if permission_mode is not None:
        allowed_modes = {"default", "acceptEdits", "plan", "bypassPermissions"}
        if permission_mode not in allowed_modes:
            raise typer.BadParameter(
                f"Invalid --permission-mode '{permission_mode}'. Choose from: {', '.join(sorted(allowed_modes))}."
            )

    # Parse tag filters
    include_tags = {t.strip() for t in tags.split(",") if t.strip()} if tags else None
    exclude_tags_set = {t.strip() for t in exclude_tags.split(",") if t.strip()} if exclude_tags else None

    # Setup logging before running tasks
    log_level = settings.log_level
    setup_logging(level=log_level, log_file=log_file, verbose=verbose)

    # Run the async entry point
    asyncio.run(
        _run_all_tasks(
            task_files,
            max_iterations,
            preserve,
            run_dir,
            max_parallel,
            snapshot_mode,
            snapshot_checkpoint_freq,
            include_tags,
            exclude_tags_set,
            model,
            permission_mode,
            max_turns,
        )
    )


async def _run_all_tasks(
    task_files: list[Path],
    max_iterations: int | None,
    preserve: bool,
    run_dir: Path | None,
    max_parallel: int,
    snapshot_mode: str | None,
    snapshot_checkpoint_freq: int | None,
    include_tags: set[str] | None = None,
    exclude_tags: set[str] | None = None,
    agent_model: str | None = None,
    permission_mode: str | None = None,
    max_turns: int | None = None,
) -> None:
    """Async entry point for running all tasks (optionally in parallel).

    This is now a thin wrapper around Orchestrator.run_batch().
    The CLI handles presentation (glob expansion, Rich output) while
    the Orchestrator handles business logic (execution, concurrency, summarization).

    Args:
        task_files: List of task file paths or glob patterns
        max_iterations: Optional override for max iterations
        preserve: Whether to preserve sandbox
        run_dir: Custom run directory (or None for auto-generated)
        max_parallel: Maximum number of concurrent tasks
        snapshot_mode: Optional override for snapshot mode
        snapshot_checkpoint_freq: Optional override for checkpoint frequency
        include_tags: Only run tasks matching any of these tags
        exclude_tags: Skip tasks matching any of these tags
        agent_model: Optional override for agent model
        permission_mode: Optional override for permission mode
        max_turns: Optional override for max agent turns
    """
    # Prepare run directory
    run_dir = prepare_run_directory(run_dir)

    # Expand glob patterns and collect task files
    all_task_files = expand_task_files(task_files)

    # Print execution mode
    print_execution_mode(len(all_task_files), max_parallel)

    # Configure batch execution
    config = BatchRunConfig(
        run_dir=run_dir,
        max_parallel=max_parallel,
        preserve_sandbox=preserve,
        max_iterations=max_iterations,
        snapshot_mode=snapshot_mode,
        snapshot_checkpoint_freq=snapshot_checkpoint_freq,
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        agent_model=agent_model,
        permission_mode=permission_mode,
        max_turns=max_turns,
    )

    # Run batch with tqdm progress bar
    progress_bar: tqdm[Any] | None = None

    def _on_batch_start(task_count: int) -> None:
        nonlocal progress_bar
        progress_bar = tqdm(
            total=task_count, desc="Tasks", unit="task", dynamic_ncols=True, disable=not sys.stderr.isatty()
        )

    def _on_task_complete(result: dict[str, Any]) -> None:
        if progress_bar is None:
            return
        status = result["result"].final_status
        task_id = result["task_id"]
        status_icon = {"SUCCESS": "\u2713", "FAILURE": "\u2717", "ERROR": "!"}.get(status, "?")
        progress_bar.set_postfix_str(f"{status_icon} {task_id}")
        progress_bar.update(1)

    try:
        summary = await Orchestrator.run_batch(
            task_files=all_task_files,
            config=config,
            on_task_complete=_on_task_complete,
            on_batch_start=_on_batch_start,
        )
    finally:
        if progress_bar is not None:
            progress_bar.close()

    # Create 'latest' symlink
    if run_dir.parent == settings.runs_dir:  # Only if using default runs/ directory
        create_latest_symlink(settings.runs_dir, run_dir.name)

    # Aggregate task logs into run.log
    from ..logging_config import aggregate_task_logs

    aggregate_task_logs(run_dir)

    # Print execution summary
    print_execution_summary(run_dir, summary)
