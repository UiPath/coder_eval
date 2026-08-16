"""Plan command - validate task files without executing."""

import warnings
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape

from ..models import ROW_SELECTOR_FLAGS
from ..models.tasks import TaskDefinition, UnknownTaskFieldWarning
from ..orchestration.task_loader import (
    STRATIFIED_CAUSE_PREFIXES,
    expand_dataset_with_selection,
    load_dataset_rows,
    load_task,
    row_split_label,
    stratum_key,
)
from .console import console
from .row_selectors import SAMPLE_HELP, SAMPLE_PER_STRATUM_HELP, SPLIT_HELP
from .run_helpers import discover_default_tasks
from .utils import check_api_keys, check_tools


# The empty stratum key rendered for a human. `stratum_key` folds a missing field and an
# empty string into "", which prints as nothing at all — a count with no label. The LABEL is
# a display concern and lives here; the RULE lives on `stratum_key` and is not restated.
_EMPTY_STRATUM_LABEL = "(none)"


def _preview_dataset(
    task: TaskDefinition,
    task_file: Path,
    *,
    split_name: str | None = None,
    max_rows: int | None = None,
    sample_per_stratum: int | None = None,
    emit: Callable[[str], None],
) -> list[TaskDefinition]:
    """Expand a dataset-backed task, print its row accounting, and return the rows.

    Runs at the one surface that costs nothing, so a suite's resolved row count — the
    number every cost estimate and every A/B comparison depends on — is knowable before
    any money is spent. Expansion also validates row ids and every ``${row.*}``
    substitution, which previously failed per-row at run time after the sandbox was built.

    The narrowing suffix is printed from what the selector code **reports**
    (``RowSelectionOutcome.applied``), never re-derived here. The win-order lives in
    ``task_loader.select_rows``; restating it would rebuild the defect this preview exists
    to fix — the old line named whichever selector was *set*, so a reduction caused by a
    task's own ``dataset.sample_per_stratum`` was attributed to ``--split``.

    Returns ``[task]`` unchanged when the task carries no ``dataset:`` block; a "1 row"
    line there would be noise about a concept the task does not have.

    Lines go to ``emit`` rather than straight to the console because the caller BUFFERS a
    file's whole preview: this function can raise part-way through, and the ✓/✗ banner is only
    knowable once it has either finished or not. Printing directly is what let one file print
    ✓ and then ✗.
    """
    if task.dataset is None:
        return [task]

    # ONE draw, previewed and reported. Calling expand_dataset and then re-selecting for the
    # breakdown would re-run a nondeterministic stratified sampler and print counts for rows
    # this command did not return.
    expanded, outcome = expand_dataset_with_selection(
        task,
        task_file.parent,
        max_rows=max_rows,
        sample_per_stratum=sample_per_stratum,
        split=split_name,
    )
    # The unfiltered total, from a list length rather than a second expansion: that would
    # re-run whole-dataset id validation and full substitution over every row for a number
    # already in hand.
    rows = load_dataset_rows(task.dataset, task_file.parent)
    # `row_split_label` is the runtime's single definition of "labelled" (select_rows and
    # CE035 call it too) — do not re-derive the rule here.
    labels = [row_split_label(r, task.dataset.split_field) for r in rows]
    labelled = [x for x in labels if x is not None]

    # Two different questions, and the line answers whichever applies:
    #   what NARROWED  -> `outcome.applied`, the selector code's own report;
    #   what was ASKED  -> the arguments, when nothing narrowed.
    # Printing only the first was a regression: `--split test` on an all-test suite is HONOURED
    # and removes nothing, so the line became byte-identical to passing no selector at all and a
    # user could no longer confirm the flag had been read. It also contradicted `run.md`, which
    # records what was REQUESTED — the two surfaces described one invocation two ways.
    requested = [
        f"{flag} {value}"
        for flag, value in (
            (ROW_SELECTOR_FLAGS["split"], split_name),
            (ROW_SELECTOR_FLAGS["max_rows"], max_rows),
            (ROW_SELECTOR_FLAGS["sample_per_stratum"], sample_per_stratum),
        )
        if value is not None
    ]
    if outcome.applied:
        suffix = f" ({escape(', '.join(outcome.applied))})"
    elif split_name is not None and not labelled:
        # Do not imply the selector did something. It did not: an unlabelled task passes
        # through --split untouched, because --split is global to the invocation.
        suffix = f" (--split {escape(split_name)}: not labelled, all rows kept)"
    elif requested:
        suffix = f" (requested {escape(', '.join(requested))}; removed no rows)"
    else:
        suffix = ""
    # `suffix` is BUILT from escaped parts above and must NOT be escaped again — escape() is not
    # idempotent, so a second pass renders a literal backslash before every bracket in a value.
    emit(f"  [dim]Dataset: {len(rows)} rows -> {len(expanded)} selected{suffix}[/dim]")

    _print_strata(task, outcome.rows, emit)

    # The pre-spend half of the partial-labelling signal (select_rows also logs a WARNING).
    # `plan` is the loudest of the two because it is free to run — and this is the state
    # that silently shrinks every metric.
    if split_name is not None and labelled and len(labelled) != len(labels):
        emit(
            f"  [yellow]⚠[/yellow] [yellow]{len(labels) - len(labelled)} of {len(labels)} rows carry "
            + f"no '{escape(task.dataset.split_field)}' label and are DROPPED by --split; every metric would be "
            + "computed over the smaller set[/yellow]"
        )

    # A stratified draw that actually narrowed the set, with no pinned seed, selects a
    # DIFFERENT subset every invocation. Without saying so the strata line above reads as a
    # promise of identity the sampler does not make.
    stratified = any(cause.startswith(STRATIFIED_CAUSE_PREFIXES) for cause in outcome.applied)
    if stratified and task.dataset.sample_seed is None:
        emit(
            "  [yellow]⚠[/yellow] [yellow]the stratified sample is re-drawn every invocation "
            + "(dataset.sample_seed is not set), so `run` will execute this many rows but not "
            + "necessarily these ones; set dataset.sample_seed to pin them[/yellow]"
        )
    return expanded


def _print_strata(task: TaskDefinition, selected: list[dict[str, Any]], emit: Callable[[str], None]) -> None:
    """Print the per-stratum breakdown of the SELECTED rows, or nothing.

    Silent below two distinct strata: a one-entry breakdown restates the row count.

    Grouping goes through ``task_loader.stratum_key`` — the sampler's own rule — so the
    counts describe the strata ``--sample-per-stratum`` actually draws from. A preview that
    invented its own grouping would print numbers for strata the sampler does not use, which
    is the class of bug this whole surface exists to close.
    """
    if task.dataset is None or not selected:
        return
    field = task.dataset.stratify_field
    counts = Counter(stratum_key(row, field) for row in selected)
    if len(counts) < 2:
        return
    # Stratum values and the field name come from user YAML/JSONL, so they are ESCAPED: the
    # threat is not that they sit outside the [dim] span, it is that a value can BE a tag.
    # An `expected_skill: "[/dim]"` raises MarkupError — swallowed by the per-task handler
    # into a red ✗ and a non-zero exit on a perfectly valid suite — and an `[bold]` silently
    # renders as an empty label, i.e. a preview that misreports its own breakdown.
    rendered = ", ".join(f"{escape(key or _EMPTY_STRATUM_LABEL)}={n}" for key, n in sorted(counts.items()))
    # `rendered` is assembled from escaped keys just above and must NOT be escaped again: escape()
    # is not idempotent, so a second pass prints a backslash before every bracket in a value.
    emit(f"  [dim]strata ({escape(field)}): {rendered}[/dim]")


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
    split: str | None = typer.Option(None, "--split", help=SPLIT_HELP),
    sample: int | None = typer.Option(None, "--sample", help=SAMPLE_HELP, min=1),
    sample_per_stratum: int | None = typer.Option(None, "--sample-per-stratum", help=SAMPLE_PER_STRATUM_HELP, min=1),
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
        coder-eval plan evals/outcome.yaml --split test --sample-per-stratum 3
    """
    # Default to discovering all tasks under tasks/ when none provided
    resolved_task_files = task_files if task_files else discover_default_tasks()
    # Tests call plan_command(...) directly rather than through CliRunner, so an unpassed
    # option arrives as a typer OptionInfo rather than its declared default — the same
    # guard `experiment` already carries below. `not isinstance(x, bool)` on the integers
    # because bool is an int subclass, and a True would otherwise become a sample of 1.
    split_name = split if isinstance(split, str) else None
    max_rows = sample if isinstance(sample, int) and not isinstance(sample, bool) else None
    per_stratum = (
        sample_per_stratum if isinstance(sample_per_stratum, int) and not isinstance(sample_per_stratum, bool) else None
    )

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
        console.print(f"[red]Failed to load experiment ({escape(str(exp_path))}): {escape(str(e))}[/red]")
        raise typer.Exit(1) from e

    # Show experiment info
    console.print("[bold cyan]Experiment[/bold cyan]")
    console.print(f"  [dim]ID: {escape(str(exp_def.experiment_id))}[/dim]")
    if exp_def.description:
        console.print(f"  [dim]Description: {escape(str(exp_def.description))}[/dim]")
    variant_ids = escape(", ".join(v.variant_id for v in exp_def.variants))
    console.print(f"  [dim]Variants ({len(exp_def.variants)}): {variant_ids}[/dim]")  # noqa: CE050
    if exp_def.defaults and exp_def.defaults.agent:
        console.print(f"  [dim]Default agent config: {escape(str(exp_def.defaults.agent))}[/dim]")
    console.print()

    # Validate each task file
    #
    # BUFFERED, one file at a time. The ✓/✗ banner is not knowable until the whole per-file body
    # has either finished or raised — `_preview_dataset` and the per-variant loop both run AFTER
    # the file has loaded and both can fail — so printing the banner on a successful `load_task`
    # made a file that loaded and then failed print ✓ and, from the outer handler, ✗ as well. The
    # banner is emitted first and the buffered lines under it, because the detail lines belong
    # beneath their own filename heading: simply moving the banner below the body would leave a
    # multi-file plan with every heading detached from its own detail.
    all_valid = True
    for task_file in resolved_task_files:
        detail: list[str] = []
        loaded = True
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

            detail.append(f"  [dim]Task ID: {escape(str(task.task_id))}[/dim]")

            # Handle optional agent field. Phase 3 made agent.type optional —
            # tasks may now defer it to experiment defaults / --type.
            if task.agent is None:
                detail.append("  [dim]Agent: N/A (resolved from experiment)[/dim]")
            elif task.agent.type is None:
                detail.append("  [dim]Agent type: (deferred to experiment / --type)[/dim]")
            else:
                detail.append(f"  [dim]Agent: {escape(str(task.agent.type))}[/dim]")

            detail.append(f"  [dim]Success criteria: {len(task.success_criteria)}[/dim]")

            expanded = _preview_dataset(
                task,
                task_file,
                split_name=split_name,
                max_rows=max_rows,
                sample_per_stratum=per_stratum,
                emit=detail.append,
            )

            # Surface unknown-field warnings as inline notices (non-blocking;
            # catches stale top-level fields like max_iterations / llm_reviewer
            # that the soft-launch validator otherwise drops silently). Match
            # by category, not message text, so a reworded warning string
            # doesn't silently break this rendering. Anything else captured
            # gets re-emitted to stderr so non-target deprecations stay visible.
            for w in caught:
                if issubclass(w.category, UnknownTaskFieldWarning):
                    detail.append(f"  [yellow]⚠[/yellow] [yellow]{escape(str(w.message))}[/yellow]")
                else:
                    warnings.showwarning(w.message, w.category, w.filename, w.lineno)

            # Show resolved agent per variant
            for variant in exp_def.variants:
                variant_id = escape(str(variant.variant_id))
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
                    detail.append(f"    [dim]Variant '{variant_id}': {escape(agent_type)}{escape(model_str)}[/dim]")
                except EarlyStopConfigError as e:
                    # A hard config error (unlike generic per-variant resolution
                    # failures, which stay soft): flip the plan exit code. The banner stays ✓ —
                    # it reports whether the FILE is loadable, and this file is; the red line
                    # right beneath it names the variant that is not.
                    detail.append(f"    [red]Variant '{variant_id}': early-stop config error - {escape(str(e))}[/red]")
                    all_valid = False
                except Exception as e:
                    detail.append(f"    [red]Variant '{variant_id}': resolution failed - {escape(str(e))}[/red]")

        except Exception as e:
            loaded = False
            all_valid = False
            detail.append(f"  [red]Error: {escape(str(e))}[/red]")

        # ONE banner per file, and only now — after everything that could fail has run.
        console.print(
            f"[green]\u2713[/green] {escape(str(task_file.name))}"
            if loaded
            else f"[red]\u2717[/red] {escape(str(task_file.name))}"
        )
        for line in detail:
            console.print(line)

    if all_valid:
        console.print("\n[green]All tasks are valid![/green]")
    else:
        console.print("\n[red]Some tasks have errors.[/red]")
        raise typer.Exit(1)
