"""Run command - execute evaluation tasks."""

import asyncio
from pathlib import Path

import typer

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
    )

    # Run batch (business logic delegated to orchestrator)
    summary = await Orchestrator.run_batch(task_files=all_task_files, config=config)

    # Create 'latest' symlink
    if run_dir.parent == settings.runs_dir:  # Only if using default runs/ directory
        create_latest_symlink(settings.runs_dir, run_dir.name)

    # Aggregate task logs into run.log
    from ..logging_config import aggregate_task_logs

    aggregate_task_logs(run_dir)

    # Print execution summary
    print_execution_summary(run_dir, summary)
