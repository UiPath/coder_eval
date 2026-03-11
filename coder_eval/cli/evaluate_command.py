"""Evaluate command - run criteria against a directory without an agent."""

import asyncio
from pathlib import Path

import typer

from ..logging_config import setup_logging
from ..models import TemplateDirSource
from ..orchestration.task_loader import load_task
from ..orchestrator import Orchestrator
from ..sandbox import Sandbox
from .console import console
from .run_helpers import prepare_run_directory


def evaluate_command(
    task_file: Path = typer.Argument(  # noqa: B008
        ...,
        help="Path to task YAML file",
        exists=True,
    ),
    work_dir: Path = typer.Argument(  # noqa: B008
        ...,
        help="Directory containing the code to evaluate",
        exists=True,
        file_okay=False,
        dir_okay=True,
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose (DEBUG level) logging",
    ),
    preserve: bool = typer.Option(
        True,
        "--preserve/--no-preserve",
        "-p/-P",
        help="Copy sandbox artifacts to run directory (default: preserve). The temp sandbox is always removed.",
    ),
    run_dir: Path | None = typer.Option(  # noqa: B008
        None,
        "--run-dir",
        help="Custom run directory (default: auto-generated timestamped directory in runs/)",
    ),
) -> None:
    """Evaluate criteria against a directory without running an agent.

    Runs the success criteria defined in a task against a work directory.
    Artifacts are saved to a run directory when --preserve is used.

    Examples:
        coder-eval evaluate tasks/hello.yaml ./my_solution
        coder-eval evaluate tasks/test.yaml /path/to/code --preserve
        coder-eval evaluate tasks/test.yaml /path/to/code --run-dir ./my_eval_run
    """
    setup_logging(verbose=verbose)

    console.print("\n[bold]Evaluating Criteria[/bold]\n")

    try:
        task = load_task(task_file)
    except Exception as e:
        console.print(f"[red]✗ Failed to load task:[/red] {e}")
        raise typer.Exit(1) from e

    if not work_dir.is_dir():
        console.print(f"[red]✗ Work directory is not a directory:[/red] {work_dir}")
        raise typer.Exit(1)

    try:
        prepared_run_dir = prepare_run_directory(run_dir)
    except Exception as e:
        console.print(f"[red]✗ Failed to prepare run directory:[/red] {e}")
        raise typer.Exit(1) from e

    # Build a sandbox pre-loaded with the work_dir contents, then run evaluate-only
    sandbox_config = task.sandbox.model_copy(deep=True)
    template_source = TemplateDirSource(path=str(work_dir.resolve()))
    if sandbox_config.template_sources:
        sandbox_config.template_sources = [template_source, *sandbox_config.template_sources]
    else:
        sandbox_config.template_sources = [template_source]

    task_dir = task_file.parent.resolve()
    sandbox = Sandbox(sandbox_config, task_id=task.task_id, task_dir=task_dir)

    async def _setup_and_run():
        await asyncio.to_thread(sandbox.setup)
        orchestrator = Orchestrator(
            task=task,
            run_dir=prepared_run_dir,
            preserve_sandbox=preserve,
            task_file=task_file,
            sandbox=sandbox,
            variant_id="evaluate",
        )
        return await orchestrator.run()

    result = asyncio.run(_setup_and_run())

    # Display results
    console.print("[bold]Criteria Results:[/bold]\n")

    criteria_results = result.success_criteria_results or []
    if len(criteria_results) != len(task.success_criteria):
        console.print(
            f"[red]✗ Result count mismatch: got {len(criteria_results)}, expected {len(task.success_criteria)}[/red]"
        )
        raise typer.Exit(1)

    for criterion, cr in zip(task.success_criteria, criteria_results, strict=True):
        status = "[green]✓[/green]" if cr.score >= criterion.pass_threshold else "[red]✗[/red]"
        console.print(f"{status} {cr.criterion_type}")
        console.print(f"  [dim]{cr.description}[/dim]")
        console.print(f"  [dim]Score: {cr.score:.2f}[/dim]")
        if cr.details:
            console.print(f"  [dim]Details: {cr.details}[/dim]")
        if cr.error:
            console.print(f"  [red]Error: {cr.error}[/red]")
        console.print()

    passed = sum(
        1 for cr, c in zip(criteria_results, task.success_criteria, strict=True) if cr.score >= c.pass_threshold
    )
    total = len(task.success_criteria)
    failed = total - passed

    console.print("[bold]Summary:[/bold]")
    console.print(f"  Passed: {passed}/{total}")
    console.print(f"  Failed: {failed}/{total}")
    console.print(f"\n[dim]Run directory: {prepared_run_dir}[/dim]")
    if result.sandbox_path:
        console.print(f"[dim]Artifacts: {result.sandbox_path}[/dim]")

    if result.final_status == "ERROR":
        console.print(f"\n[red]✗ Evaluation error: {result.error_message}[/red]")
        raise typer.Exit(1)
    elif failed == 0:
        console.print("\n[green]All criteria passed! ✓[/green]")
        raise typer.Exit(0)
    else:
        console.print(f"\n[red]{failed} criterion/criteria failed.[/red]")
        raise typer.Exit(1)
