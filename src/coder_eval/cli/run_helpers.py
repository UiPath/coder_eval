"""Helper functions for the run command."""

import random
from pathlib import Path

import typer

from ..config import settings
from ..models import RunSummary
from ..path_utils import TASK_LOG_FILENAME, generate_run_id
from .console import console


# Resolve tasks/ relative to project root — this is a repo-only feature.
# When running from a wheel install, users must provide explicit task file paths.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DEFAULT_TASKS_DIR = _PROJECT_ROOT / "tasks"


def discover_default_tasks() -> list[Path]:
    """Recursively find all .yaml task files under the default tasks/ directory.

    This feature requires a source checkout — the tasks/ directory is not
    shipped in the wheel. When running from an installed package, provide
    explicit task file paths instead.

    Returns:
        Sorted list of task file paths.

    Raises:
        typer.Exit: If the tasks/ directory doesn't exist or contains no YAML files.
    """
    if not DEFAULT_TASKS_DIR.is_dir():
        console.print(f"[red]Default tasks directory not found: {DEFAULT_TASKS_DIR}[/red]")
        console.print(
            "[yellow]Hint: Zero-argument task discovery requires a source checkout. "
            + "Provide explicit task file paths instead: coder-eval run task1.yaml task2.yaml[/yellow]"
        )
        raise typer.Exit(1)

    task_files = list(DEFAULT_TASKS_DIR.rglob("*.yaml"))
    if not task_files:
        console.print(f"[red]No .yaml files found in {DEFAULT_TASKS_DIR}[/red]")
        raise typer.Exit(1)

    random.shuffle(task_files)
    console.print(f"[dim]Discovered {len(task_files)} task(s) from {DEFAULT_TASKS_DIR}[/dim]")
    return task_files


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
        typer.Exit: If any pattern matches no task file
    """
    all_task_files = []
    # Per-pattern, not just on the union. Accumulating and checking only the
    # total meant one stale entry among several (a renamed or moved suite)
    # silently ran the surviving subset and exited 0, so a CI gate reported
    # green over tasks it never measured. A pattern the caller wrote is a
    # pattern the caller expects to match something.
    unmatched = []
    for pattern in task_files:
        if pattern.is_file():
            all_task_files.append(pattern)
            continue
        # Try as glob pattern (supports ** for recursive matching)
        if pattern.is_absolute():
            matches = list(Path(pattern.anchor).glob(str(pattern.relative_to(pattern.anchor))))
        else:
            matches = list(Path().glob(str(pattern)))
        if not matches:
            unmatched.append(pattern)
        all_task_files.extend(matches)

    # The union check still stands on its own: an empty `task_files` reaches here
    # with nothing unmatched, and returning [] would run a zero-task suite green.
    if unmatched or not all_task_files:
        console.print("[red]No task files found![/red]")
        for pattern in unmatched:
            console.print(f"[red]  no match: {pattern}[/red]")
        raise typer.Exit(1)

    random.shuffle(all_task_files)
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
    # An ungraded run has no pass rate to report — printing "0/N succeeded" for a
    # clean `coder-eval execute` reads as a total failure. Report what actually
    # happened instead, and keep the graded line for whatever WAS graded.
    if summary.tasks_not_graded:
        console.print(f"[bold]Results:[/bold] {summary.tasks_not_graded}/{summary.tasks_run} executed, not graded")
        # Point at the run-dir form, not `evaluate <task.yaml> <workspace>`: the
        # two-argument shape grades a bare directory with NO trajectory, so
        # command_executed / skill_triggered / trajectory-reading judges score
        # differently from what `run` would have produced. The run-dir form
        # restores the trajectory AND the resolved config.
        console.print(f"[dim]Grade later: uv run coder-eval run <tasks> --run-dir {run_dir} --resume[/dim]")
        console.print("[dim]           or: uv run coder-eval evaluate <run_dir>/<variant>/<task_id>/00[/dim]")
    if summary.tasks_graded or not summary.tasks_not_graded:
        # The `or not ...` keeps the pre-existing "0/0 succeeded" line for an
        # empty run: without it a run with no tasks at all prints no Results
        # line whatsoever, since both counters are falsy.
        console.print(f"[bold]Results:[/bold] {summary.tasks_succeeded}/{summary.tasks_graded} succeeded")
    console.print(f"[dim]View report: open {run_dir / 'experiment.md'}[/dim]")
    console.print(f"[dim]View report: uv run coder-eval report {run_dir}[/dim]")

    # Print log file locations
    console.print("\n[bold]Log Files:[/bold]")
    run_log_path = run_dir / "experiment.log"
    if run_log_path.exists():
        console.print(f"  Experiment log: {run_log_path}")

    # Print task logs (find all task.log files under variant directories)
    for task_log in sorted(run_dir.glob(f"**/{TASK_LOG_FILENAME}")):
        # Dataset fan-out task_ids contain slashes, so render the full
        # relative path rather than indexing fixed parent segments.
        rel = task_log.parent.relative_to(run_dir).as_posix()
        console.print(f"  Task log ({rel}): {task_log}")
