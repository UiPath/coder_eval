"""Command-line interface for coder_eval."""

import asyncio
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console

from .config import settings
from .logging_config import setup_logging
from .orchestrator import Orchestrator
from .reports import ReportGenerator


if TYPE_CHECKING:
    pass

# Create the Typer app
app = typer.Typer(
    name="coder-eval",
    help="A framework for evaluating AI coding agents",
    add_completion=False,
)

console = Console()


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """A framework for evaluating AI coding agents.

    Run 'coder-eval COMMAND --help' for help on a specific command.

    Available commands:
    - run: Execute evaluation tasks
    - plan: Validate task files (dry-run)
    - report: Display or export evaluation reports
    """
    # If no subcommand was invoked, show help and exit
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit(0)


@app.command()
def run(
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
    """
    # Setup logging before running tasks
    log_level = settings.log_level
    setup_logging(level=log_level, log_file=log_file, verbose=verbose)

    # Run the async entry point
    asyncio.run(
        _run_all_tasks(
            task_files, max_iterations, preserve, run_dir, max_parallel, snapshot_mode, snapshot_checkpoint_freq
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
    """
    from coder_eval.orchestrator import BatchRunConfig, Orchestrator
    from coder_eval.path_utils import create_latest_symlink, generate_run_id

    # Create timestamped run directory
    if run_dir is None:
        run_id = generate_run_id()
        run_dir = settings.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold]Run directory:[/bold] {run_dir}\n")

    # Collect all task files (glob expansion - stays in CLI)
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

    # Print execution mode (presentation)
    console.print(f"\n[bold]Running {len(all_task_files)} task(s)[/bold]")
    if max_parallel > 1:
        console.print(f"[dim]Max parallel tasks: {max_parallel}[/dim]\n")
    else:
        console.print("[dim]Mode: Sequential[/dim]\n")

    # Configure batch execution
    config = BatchRunConfig(
        run_dir=run_dir,
        max_parallel=max_parallel,
        preserve_sandbox=preserve,
        max_iterations=max_iterations,
        snapshot_mode=snapshot_mode,
        snapshot_checkpoint_freq=snapshot_checkpoint_freq,
    )

    # Run batch (business logic delegated to orchestrator)
    summary = await Orchestrator.run_batch(task_files=all_task_files, config=config)

    # Create 'latest' symlink
    if run_dir.parent == settings.runs_dir:  # Only if using default runs/ directory
        create_latest_symlink(settings.runs_dir, run_dir.name)

    # Aggregate task logs into run.log
    from coder_eval.logging_config import aggregate_task_logs

    aggregate_task_logs(run_dir)

    # Print summary (presentation)
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


# Legacy functions removed - batch orchestration now handled by Orchestrator.run_batch()


@app.command()
def plan(
    task_files: list[Path] = typer.Argument(  # noqa: B008
        ...,
        help="Path(s) to task YAML file(s) to validate",
        exists=True,
    ),
) -> None:
    """Validate task files without executing (dry-run).

    This command checks:
    - Task file syntax and schema validity
    - Required CLI tools are available (claude, uv)
    - API keys are configured
    - Task configuration is reasonable

    Examples:
        coder-eval plan tasks/hello_date.yaml
        coder-eval plan tasks/*.yaml
    """
    console.print("\n[bold]Task Validation (Dry-Run)[/bold]\n")

    # Check required tools
    _check_tools()

    # Check API keys
    _check_api_keys()

    # Validate each task file
    all_valid = True
    for task_file in task_files:
        try:
            task = Orchestrator.load_task(task_file)

            console.print(f"[green]✓[/green] {task_file.name}")
            console.print(f"  [dim]Task ID: {task.task_id}[/dim]")
            console.print(f"  [dim]Agent: {task.agent.type.value}[/dim]")
            console.print(f"  [dim]Max iterations: {task.max_iterations}[/dim]")
            console.print(f"  [dim]Success criteria: {len(task.success_criteria)}[/dim]")

        except Exception as e:
            console.print(f"[red]✗[/red] {task_file.name}")
            console.print(f"  [red]Error: {e}[/red]")
            all_valid = False

    if all_valid:
        console.print("\n[green]All tasks are valid![/green]")
    else:
        console.print("\n[red]Some tasks have errors.[/red]")
        raise typer.Exit(1)


@app.command()
def report(
    run_dir: Path = typer.Argument(  # noqa: B008
        ...,
        help="Path to a run directory (e.g., runs/latest or runs/2025-10-10_20-57-30)",
        exists=True,
    ),
    output_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--output",
        "-o",
        help="Output file for markdown report (default: display to stdout)",
    ),
) -> None:
    """Display or export a run report.

    The run command automatically generates a report during execution.
    This command allows you to view or export that report later.

    Examples:
        # Display latest run report
        coder-eval report runs/latest

        # Export specific run to file
        coder-eval report runs/2025-10-10_20-57-30 -o summary.md

        # View older run
        coder-eval report runs/2025-10-09_15-30-45
    """
    try:
        report_md, source_path = ReportGenerator.load_from_run_dir(run_dir)
        console.print(f"[dim]Reading report from {source_path}[/dim]\n")

    except FileNotFoundError as e:
        console.print(f"[red]Error: {e}[/red]")
        console.print("\n[dim]Hint: Use 'coder-eval run' to create evaluation runs.[/dim]")
        raise typer.Exit(1) from e

    # Output report
    if output_file:
        output_file.write_text(report_md)
        console.print(f"[green]✓ Report saved to {output_file}[/green]")
    else:
        from rich.markdown import Markdown

        console.print(Markdown(report_md))


def _check_tools() -> None:
    """Check that required tools are available."""
    console.print("[bold]Checking required tools...[/bold]")

    tools = {
        "claude": "Claude Code CLI",
        "uv": "UV package manager",
    }

    all_found = True
    for cmd, name in tools.items():
        if shutil.which(cmd):
            console.print(f"  [green]✓[/green] {name} ({cmd})")
        else:
            console.print(f"  [red]✗[/red] {name} ({cmd}) not found")
            all_found = False

    if not all_found:
        console.print("[yellow]Warning: Some tools are missing[/yellow]")


def _check_api_keys() -> None:
    """Check that API keys are configured."""
    console.print("\n[bold]Checking API keys...[/bold]")

    if settings.anthropic_api_key:
        console.print("  [green]✓[/green] ANTHROPIC_API_KEY is set")
    else:
        console.print("  [yellow]⚠[/yellow] ANTHROPIC_API_KEY not set")

    if settings.llmgw_url:
        console.print("  [green]✓[/green] LLM Gateway configured")
    else:
        console.print("  [dim]  LLM Gateway not configured (optional for LLM reviewer)[/dim]")


if __name__ == "__main__":
    app()
