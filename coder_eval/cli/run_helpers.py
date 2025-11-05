"""Helper functions for the run command."""

from pathlib import Path

import typer

from ..config import settings
from ..orchestrator import RunSummary
from ..path_utils import generate_run_id
from .console import console


def prepare_run_directory(run_dir: Path | None) -> Path:
    """Create and prepare the run directory.

    Args:
        run_dir: Custom run directory path, or None to auto-generate

    Returns:
        Path to the prepared run directory
    """
    if run_dir is None:
        run_id = generate_run_id()
        run_dir = settings.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold]Run directory:[/bold] {run_dir}\n")
    return run_dir


def expand_task_files(task_files: list[Path]) -> list[Path]:
    """Expand glob patterns and collect all task files.

    Args:
        task_files: List of task file paths or glob patterns

    Returns:
        List of resolved task file paths

    Raises:
        typer.Exit: If no task files are found
    """
    all_task_files = []
    for pattern in task_files:
        if pattern.is_file():
            all_task_files.append(pattern)
        else:
            # Try as glob pattern
            all_task_files.extend(pattern.parent.glob(pattern.name))

    if not all_task_files:
        console.print("[red]No task files found![/red]")
        raise typer.Exit(1)

    return all_task_files


def print_execution_mode(task_count: int, max_parallel: int) -> None:
    """Print execution mode information.

    Args:
        task_count: Number of tasks to execute
        max_parallel: Maximum parallel tasks
    """
    console.print(f"\n[bold]Running {task_count} task(s)[/bold]")
    if max_parallel > 1:
        console.print(f"[dim]Max parallel tasks: {max_parallel}[/dim]\n")
    else:
        console.print("[dim]Mode: Sequential[/dim]\n")


def print_execution_summary(run_dir: Path, summary: RunSummary) -> None:
    """Print execution summary with results and log file locations.

    Args:
        run_dir: Path to the run directory
        summary: Run execution summary
    """
    console.print(f"\n[bold green]Run complete:[/bold green] {run_dir}")
    console.print(f"[bold]Results:[/bold] {summary.tasks_succeeded}/{summary.tasks_run} succeeded")
    console.print(f"[dim]View report: {run_dir / 'run-report.md'}[/dim]")

    # Print log file locations
    console.print("\n[bold]Log Files:[/bold]")
    run_log_path = run_dir / "run.log"
    if run_log_path.exists():
        console.print(f"  Run log: {run_log_path}")

    # Print task logs
    for task_result in summary.task_results:
        task_id = task_result.get("task_id", "unknown")
        task_log = run_dir / task_id / "task.log"
        if task_log.exists():
            console.print(f"  Task log ({task_id}): {task_log}")
