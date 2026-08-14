"""Plan command - validate task files without executing."""

import warnings
from pathlib import Path

import typer

from ..models.tasks import TaskDefinition, UnknownTaskFieldWarning
from ..orchestration.task_loader import _load_dataset_rows, expand_dataset, load_task, row_split_label
from .console import console
from .run_helpers import discover_default_tasks
from .utils import check_api_keys, check_tools


def _preview_dataset(task: TaskDefinition, task_file: Path, split_name: str | None) -> list[TaskDefinition]:
    """Expand a dataset-backed task, print its row accounting, and return the rows.

    Runs at the one surface that costs nothing, so a suite's resolved row count — the
    number every cost estimate and every A/B comparison depends on — is knowable before
    any money is spent. Expansion also validates row ids and every ``${row.*}``
    substitution, which previously failed per-row at run time after the sandbox was built.

    Returns ``[task]`` unchanged when the task carries no ``dataset:`` block; a "1 row"
    line there would be noise about a concept the task does not have.
    """
    if task.dataset is None:
        return [task]

    expanded = expand_dataset(task, task_file.parent, split=split_name)
    # The unfiltered total, from a list length rather than a second expand_dataset call:
    # that would re-run whole-dataset id validation and full substitution over every row
    # for a number already in hand.
    rows = _load_dataset_rows(task.dataset, task_file.parent)
    # `row_split_label` is the runtime's single definition of "labelled" (expand_dataset
    # and CE035 call it too) — do not re-derive the rule here.
    labels = [row_split_label(r, task.dataset.split_field) for r in rows]
    labelled = [x for x in labels if x is not None]

    if split_name is None:
        suffix = ""
    elif not labelled:
        # Do not imply the selector did something. It did not: an unlabelled task passes
        # through --split untouched, because --split is global to the invocation.
        suffix = f" (--split {split_name}: not labelled, all rows kept)"
    else:
        suffix = f" (--split {split_name})"
    console.print(f"  [dim]Dataset: {len(rows)} rows -> {len(expanded)} selected{suffix}[/dim]")

    # The pre-spend half of the partial-labelling signal (expand_dataset also logs a
    # WARNING). `plan` is the loudest of the two because it is free to run — and this is
    # the state that silently shrinks every metric.
    if split_name is not None and labelled and len(labelled) != len(labels):
        console.print(
            f"  [yellow]⚠[/yellow] [yellow]{len(labels) - len(labelled)} of {len(labels)} rows carry "
            + f"no '{task.dataset.split_field}' label and are DROPPED by --split; every metric would be "
            + "computed over the smaller set[/yellow]"
        )
    assert expanded, "expand_dataset returned no rows (the empty-dataset raise should have fired)"
    return expanded


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
    split: str | None = typer.Option(
        None,
        "--split",
        help=(
            "For dataset-backed tasks, preview only rows whose dataset.split_field value "
            "(default field: split) matches this name — e.g. --split train / --split test, "
            "matching `coder-eval run --split`. Tasks whose rows are all unlabelled are "
            "unaffected; a labelled task with no row in this split is an error naming the "
            "splits that exist."
        ),
    ),
) -> None:
    """Validate task files without executing (dry-run).

    When no TASK_FILES are provided, all .yaml files under tasks/ are discovered recursively.

    This command checks:
    - Task file syntax and schema validity
    - Required CLI tools are available (claude, uv)
    - API keys are configured
    - Task configuration is reasonable
    - Dataset-backed tasks expand: row ids validate, ``${row.*}`` substitutions resolve,
      and the selected row count is printed BEFORE any money is spent

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
        coder-eval plan evals/outcome.yaml --split test
    """
    # Default to discovering all tasks under tasks/ when none provided
    resolved_task_files = task_files if task_files else discover_default_tasks()
    # Tests call plan_command(...) directly rather than through CliRunner, so an unpassed
    # option arrives as a typer OptionInfo rather than its declared default — the same
    # guard `experiment` already carries below.
    split_name = split if isinstance(split, str) else None

    console.print("\n[bold]Task Validation (Dry-Run)[/bold]\n")

    # Check required tools
    check_tools()

    # Check API keys
    check_api_keys()

    # Lazy import to avoid circular dependency at module level
    from ..orchestration.early_stop import EarlyStopConfigError, validate_early_stop
    from ..orchestration.experiment import DEFAULT_EXPERIMENT_PATH, load_experiment, resolve_task_for_variant
    from ..orchestration.run_limits import validate_run_limits

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
                console.print(f"  [dim]Agent: {task.agent.type}[/dim]")

            console.print(f"  [dim]Success criteria: {len(task.success_criteria)}[/dim]")

            expanded = _preview_dataset(task, task_file, split_name)

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
                    # Resolve the FIRST expanded row for a dataset-backed task: that is the
                    # shape a run actually resolves, and it is where a criterion that only
                    # becomes invalid after ${row.*} substitution shows up.
                    resolved, _lineage, _ = resolve_task_for_variant(default_exp, expanded[0], exp_def, variant)
                    # Early-stop guardrails (no-op unless a criterion carries a stop_early: block).
                    validate_early_stop(resolved)
                    for message in validate_run_limits(resolved):
                        console.print(
                            f"    [yellow]⚠[/yellow] [yellow]Variant '{variant.variant_id}': {message}[/yellow]"
                        )
                    agent_type = str(resolved.agent.type) if resolved.agent else "unknown"
                    agent_model = resolved.agent.model if resolved.agent else None
                    model_str = f" ({agent_model})" if agent_model else ""
                    console.print(f"    [dim]Variant '{variant.variant_id}': {agent_type}{model_str}[/dim]")
                except EarlyStopConfigError as e:
                    # A hard config error (unlike generic per-variant resolution
                    # failures, which stay soft): flip the plan exit code.
                    console.print(f"    [red]Variant '{variant.variant_id}': early-stop config error - {e}[/red]")
                    all_valid = False
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
