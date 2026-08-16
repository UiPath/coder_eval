"""Evaluate command - run criteria against a directory without an agent."""

import asyncio
from pathlib import Path

import typer
from rich.markup import escape

from ..logging_config import setup_logging
from ..models import (
    AgentKind,
    EvaluationResult,
    FinalStatus,
    PreservationMode,
    TemplateDirSource,
    parse_agent_config,
)
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
        help="Move sandbox artifacts to run directory (default: preserve). The temp sandbox is always removed.",
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
        task, source_yaml = load_task(task_file)
    except Exception as e:
        console.print(f"[red]✗ Failed to load task:[/red] {escape(str(e))}")
        raise typer.Exit(1) from e

    # Evaluate-only mode bypasses experiment resolution + CLI overrides, so
    # `agent` may be None or `agent.type` may be unset for tasks that defer
    # those to the experiment / CLI layers. The orchestrator only uses
    # `agent.type` for result labeling here (no agent is created), so a
    # default is safe.

    if task.agent is None:
        task.agent = parse_agent_config(type=AgentKind.CLAUDE_CODE)
    elif task.agent.type is None:
        task.agent = parse_agent_config(**{**task.agent.model_dump(exclude_unset=True), "type": AgentKind.CLAUDE_CODE})

    if not work_dir.is_dir():
        console.print(f"[red]✗ Work directory is not a directory:[/red] {escape(str(work_dir))}")
        raise typer.Exit(1)

    try:
        prepared_run_dir = prepare_run_directory(run_dir)
    except Exception as e:
        console.print(f"[red]✗ Failed to prepare run directory:[/red] {escape(str(e))}")
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

    async def _setup_and_run() -> EvaluationResult:
        await asyncio.to_thread(sandbox.setup)
        orchestrator = Orchestrator(
            task=task,
            run_dir=prepared_run_dir,
            preservation_mode=PreservationMode.MOVE_ON_WRITE if preserve else PreservationMode.NONE,
            task_file=task_file,
            sandbox=sandbox,
            variant_id="evaluate",
            source_yaml=source_yaml,
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
        if not criterion.is_gating:
            # weight=0 is informational: it cannot pass/fail the task, so don't
            # render it as ✓/✗ (that would contradict the gate and the exit code).
            status = "[dim]○[/dim]"
        else:
            status = "[green]✓[/green]" if cr.score >= criterion.pass_threshold else "[red]✗[/red]"
        console.print(f"{status} {cr.criterion_type}")
        console.print(f"  [dim]{escape(str(cr.description))}[/dim]")
        console.print(f"  [dim]Score: {cr.score:.2f}[/dim]")
        if cr.details:
            console.print(f"  [dim]Details: {escape(str(cr.details))}[/dim]")
        if cr.error:
            console.print(f"  [red]Error: {escape(str(cr.error))}[/red]")
        console.print()

    # Gate over gating criteria only (weight=0 is informational and cannot fail
    # the task) so this summary + the exit code below match final_status.
    gating = [(cr, c) for cr, c in zip(criteria_results, task.success_criteria, strict=True) if c.is_gating]
    passed = sum(1 for cr, c in gating if cr.score >= c.pass_threshold)
    total = len(gating)
    failed = total - passed
    informational = len(task.success_criteria) - total

    console.print("[bold]Summary:[/bold]")
    console.print(f"  Passed: {passed}/{total}")
    console.print(f"  Failed: {failed}/{total}")
    if informational:
        # CE050 exemption: an int (a criteria count), which cannot carry a bracket.
        console.print(f"  [dim]Informational (weight=0, not gated): {informational}[/dim]")  # noqa: CE050
    console.print(f"\n[dim]Run directory: {escape(str(prepared_run_dir))}[/dim]")
    if result.sandbox_path:
        console.print(f"[dim]Artifacts: {escape(str(result.sandbox_path))}[/dim]")

    if result.final_status == FinalStatus.ERROR:
        console.print(f"\n[red]✗ Evaluation error: {escape(str(result.error_message))}[/red]")
        raise typer.Exit(1)
    elif failed == 0:
        console.print("\n[green]All criteria passed! ✓[/green]")
        raise typer.Exit(0)
    else:
        # CE050 exemption: an int (a criteria count), which cannot carry a bracket.
        console.print(f"\n[red]{failed} criterion/criteria failed.[/red]")  # noqa: CE050
        raise typer.Exit(1)
