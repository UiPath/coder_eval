"""Plan command - validate task files without executing."""

import warnings
from pathlib import Path

import typer

from ..models.tasks import UnknownTaskFieldWarning
from ..orchestration.task_loader import load_task
from .console import console
from .run_helpers import discover_default_tasks
from .utils import check_api_keys, check_tools


def plan_command(
    task_files: list[Path] | None = typer.Argument(  # noqa: B008
        None,
        help="Path(s) to task YAML file(s) to validate. Defaults to all tasks/ recursively.",
    ),
    experiment: Path | None = typer.Option(  # noqa: B008
        None,
        "--experiment",
        "-e",
        help="Experiment definition YAML (default: experiments/default.yaml)",
    ),
) -> None:
    """Validate task files without executing (dry-run).

    When no TASK_FILES are provided, all .yaml files under tasks/ are discovered recursively.

    This command checks:
    - Task file syntax and schema validity
    - Required CLI tools are available (claude, uv)
    - API keys are configured
    - Task configuration is reasonable

    When an experiment is provided (or experiments/default.yaml exists),
    also shows experiment info and resolved agent configs per variant.

    Unknown top-level fields on TaskDefinition currently soft-warn
    (see _warn_on_unknown_fields in models/tasks.py) — those warnings
    surface inline under each task as ⚠ notices, without failing the run.

    Examples:
        coder-eval plan
        coder-eval plan tasks/hello_date.yaml
        coder-eval plan tasks/*.yaml
        coder-eval plan tasks/*.yaml -e experiments/model-comparison.yaml
    """
    # Default to discovering all tasks under tasks/ when none provided
    resolved_task_files = task_files if task_files else discover_default_tasks()

    console.print("\n[bold]Task Validation (Dry-Run)[/bold]\n")

    # Check required tools
    check_tools()

    # Check API keys
    check_api_keys()

    # Lazy import to avoid circular dependency at module level
    from ..orchestration.experiment import DEFAULT_EXPERIMENT_PATH, load_experiment, resolve_task_for_variant

    # Always load experiment (defaults to experiments/default.yaml)
    exp_path = experiment if isinstance(experiment, Path) else DEFAULT_EXPERIMENT_PATH
    try:
        exp_def = load_experiment(exp_path)
        if exp_path == DEFAULT_EXPERIMENT_PATH:
            default_exp = exp_def
        elif DEFAULT_EXPERIMENT_PATH.exists():
            default_exp = load_experiment(DEFAULT_EXPERIMENT_PATH)
        else:
            default_exp = exp_def  # fall back to custom as its own baseline
    except Exception as e:
        console.print(f"[red]Failed to load experiment ({exp_path}): {e}[/red]")
        raise typer.Exit(1) from e

    # Show experiment info
    console.print("[bold cyan]Experiment[/bold cyan]")
    console.print(f"  [dim]ID: {exp_def.experiment_id}[/dim]")
    if exp_def.description:
        console.print(f"  [dim]Description: {exp_def.description}[/dim]")
    console.print(
        f"  [dim]Variants ({len(exp_def.variants)}): {', '.join(v.variant_id for v in exp_def.variants)}[/dim]"
    )
    if exp_def.defaults and exp_def.defaults.agent:
        console.print(f"  [dim]Default agent config: {exp_def.defaults.agent}[/dim]")
    console.print()

    # Validate each task file
    all_valid = True
    for task_file in resolved_task_files:
        try:
            # Capture warnings so unknown-field UnknownTaskFieldWarnings
            # (emitted by TaskDefinition._warn_on_unknown_fields while the
            # top-level schema stays in soft-launch mode) surface inline
            # below \u2014 they don't fail the run, but they're visible to the
            # author and to any CI log scraper. Other DeprecationWarnings
            # raised during load (legacy-timing migrations, pydantic,
            # transitive libs) are re-emitted through warnings.showwarning
            # so they still reach stderr instead of getting swallowed.
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", DeprecationWarning)
                task, _source_yaml = load_task(task_file)

            console.print(f"[green]\u2713[/green] {task_file.name}")
            console.print(f"  [dim]Task ID: {task.task_id}[/dim]")

            # Handle optional agent field. Phase 3 made agent.type optional —
            # tasks may now defer it to experiment defaults / --type.
            if task.agent is None:
                console.print("  [dim]Agent: N/A (resolved from experiment)[/dim]")
            elif task.agent.type is None:
                console.print("  [dim]Agent type: (deferred to experiment / --type)[/dim]")
            else:
                console.print(f"  [dim]Agent: {task.agent.type.value}[/dim]")

            console.print(f"  [dim]Success criteria: {len(task.success_criteria)}[/dim]")

            # Surface unknown-field warnings as inline notices (non-blocking;
            # catches stale top-level fields like max_iterations / llm_reviewer
            # that the soft-launch validator otherwise drops silently). Match
            # by category, not message text, so a reworded warning string
            # doesn't silently break this rendering. Anything else captured
            # gets re-emitted to stderr so non-target deprecations stay visible.
            for w in caught:
                if issubclass(w.category, UnknownTaskFieldWarning):
                    console.print(f"  [yellow]⚠[/yellow] [yellow]{w.message}[/yellow]")
                else:
                    warnings.showwarning(w.message, w.category, w.filename, w.lineno)

            # Show resolved agent per variant
            for variant in exp_def.variants:
                try:
                    resolved, _lineage, _ = resolve_task_for_variant(default_exp, task, exp_def, variant)
                    agent_type = resolved.agent.type.value if resolved.agent else "unknown"
                    agent_model = resolved.agent.model if resolved.agent else None
                    model_str = f" ({agent_model})" if agent_model else ""
                    console.print(f"    [dim]Variant '{variant.variant_id}': {agent_type}{model_str}[/dim]")
                except Exception as e:
                    console.print(f"    [red]Variant '{variant.variant_id}': resolution failed - {e}[/red]")

        except Exception as e:
            console.print(f"[red]\u2717[/red] {task_file.name}")
            console.print(f"  [red]Error: {e}[/red]")
            all_valid = False

    if all_valid:
        console.print("\n[green]All tasks are valid![/green]")
    else:
        console.print("\n[red]Some tasks have errors.[/red]")
        raise typer.Exit(1)
