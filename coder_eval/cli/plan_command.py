"""Plan command - validate task files without executing."""

from pathlib import Path

import typer

from ..orchestration.task_loader import load_task
from .console import console
from .utils import check_api_keys, check_tools


def plan_command(
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
    check_tools()

    # Check API keys
    check_api_keys()

    # Validate each task file
    all_valid = True
    for task_file in task_files:
        try:
            task = load_task(task_file)

            console.print(f"[green]✓[/green] {task_file.name}")
            console.print(f"  [dim]Task ID: {task.task_id}[/dim]")
            if task.agent is not None:
                console.print(f"  [dim]Agent: {task.agent.type.value}[/dim]")
            elif task.agents is not None:
                agent_names = ", ".join(f"{a.name} ({a.type.value})" for a in task.agents)
                console.print(f"  [dim]Agents: {agent_names}[/dim]")
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
