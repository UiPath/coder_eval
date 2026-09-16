"""Evaluate command - run criteria against a directory or re-grade a finished run."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.markup import escape

from ..evaluation.judge_persistence import TASK_JSON_TRANSCRIPT_EXCLUDE
from ..harbor.atif_hydrate import seed_from_atif_trajectory
from ..harbor.atif_models import Trajectory
from ..logging_config import setup_logging
from ..models import (
    AgentKind,
    EvaluationResult,
    FinalStatus,
    PreservationMode,
    TaskDefinition,
    TemplateDirSource,
    parse_agent_config,
)
from ..orchestration import run_summary_rebuild
from ..orchestration.regrade import (
    RegradeError,
    back_up_pre_grade_record,
    default_workspace,
    grading_sandbox_config,
    load_prior_result,
    regrade_in_place,
    restore_pre_grade_record,
    stamp_host_grading,
    task_from_prior,
    verify_reference_unchanged,
)
from ..orchestration.task_loader import load_task
from ..orchestrator import Orchestrator
from ..path_utils import PRE_GRADE_JSON_FILENAME, TASK_JSON_FILENAME, write_text_atomic
from ..sandbox import Sandbox
from .console import console
from .evaluate_target import (
    EvaluateMode,
    EvaluateTarget,
    EvaluateTargetError,
    as_work_dir,
    resolve_evaluate_target,
)
from .run_helpers import prepare_run_directory


logger = logging.getLogger(__name__)


def resolve_grade_in_place(target: EvaluateTarget, in_place: bool | None) -> bool:
    """Whether this grade runs in the target directory or in a copy of it.

    In-place is the default for a run directory: that workspace is the run's own
    output, and copying it filters build artifacts (``node_modules``, ``dist``,
    ``.venv``) out of the grade, so a criterion reading them fails as a copying
    artifact rather than as a verdict. A plain work directory defaults to
    copying, because criteria can mutate the target and it is the user's own
    tree.

    A function rather than an expression because two places need the answer and
    one of them — the recorded-shell refusal — changes what the command is
    willing to execute. A restated copy of the rule silently stops matching the
    moment the default moves.
    """
    return in_place if in_place is not None else (target.mode is EvaluateMode.RUN_DIR)


@dataclass(frozen=True)
class _ResolvedInputs:
    """Everything the two positionals + ``--workspace`` decide, resolved once."""

    target: EvaluateTarget
    task: TaskDefinition
    source_yaml: str
    work_dir: Path
    task_file: Path | None
    prior: EvaluationResult | None


def _resolve_inputs(
    task_or_run_dir: Path,
    work_dir: Path | None,
    workspace: Path | None,
    *,
    allow_recorded_commands: bool,
    in_place: bool | None,
    allow_host_grading: bool = False,
) -> _ResolvedInputs:
    """Turn the CLI positionals into a task, a workspace, and (maybe) a prior run.

    Split out of the command because it is where both shapes converge: after this
    the rest of ``evaluate`` is one code path regardless of which form was used.
    """
    try:
        target = resolve_evaluate_target(task_or_run_dir, work_dir)
    except EvaluateTargetError as e:
        raise typer.BadParameter(str(e)) from e

    if workspace is not None and target.mode is not EvaluateMode.RUN_DIR:
        raise typer.BadParameter(
            "--workspace applies to a run directory only; in the two-argument form the "
            + "directory to grade is already the second argument."
        )

    try:
        return _resolve_run_dir_or_work_dir(
            target,
            workspace,
            allow_recorded_commands=allow_recorded_commands,
            in_place=in_place,
            allow_host_grading=allow_host_grading,
        )
    except RegradeError as e:
        # The shared core raises a plain exception (orchestration/ must not
        # depend on the CLI layer, CE004); surface it as a CLI error here.
        raise typer.BadParameter(str(e)) from e


def _resolve_run_dir_or_work_dir(
    target: EvaluateTarget,
    workspace: Path | None,
    *,
    allow_recorded_commands: bool,
    in_place: bool | None,
    allow_host_grading: bool = False,
) -> _ResolvedInputs:
    """The mode-specific half of :func:`_resolve_inputs`."""
    prior: EvaluationResult | None = None
    if target.mode is EvaluateMode.RUN_DIR and target.task_file is not None:
        # `is_run_dir` is a filename probe, so a plain work directory holding an
        # unrelated file called task.json lands here. The task file is already in
        # hand, so fall back to the shape the user asked for.
        # Rationale: .claude/notes/isolation.md § Detached grading from the CLI
        try:
            load_prior_result(target.target)
        except RegradeError as e:
            logger.warning(
                "%s holds a %s that is not a readable run record (%s); grading it as a plain " + "work directory.",
                target.target,
                TASK_JSON_FILENAME,
                e,
            )
            target = as_work_dir(target)

    if target.mode is EvaluateMode.RUN_DIR:
        prior = load_prior_result(target.target)
        if target.task_file is not None:
            task, source_yaml = load_task(target.task_file)
            console.print(f"[dim]Grading with {target.task_file} (overrides the run's recorded config).[/dim]")
        else:
            # ONE lever, passed once and derived through the SAME function
            # `run_evaluation` uses: the gate works out both answers itself rather
            # than taking two arguments a caller could set incoherently.
            # Rationale: .claude/notes/isolation.md § Detached grading from the CLI
            task, source_yaml = task_from_prior(
                prior,
                target.target,
                allow_recorded_commands=allow_recorded_commands,
                grade_in_place=resolve_grade_in_place(target, in_place),
                allow_host_grading=allow_host_grading,
            )
        work_dir = workspace or default_workspace(target.target, prior)
        recorded_source = prior.task_config.source_file if prior.task_config else None
        task_file = target.task_file or (Path(recorded_source) if recorded_source else None)
    else:
        assert target.task_file is not None  # guaranteed by resolve_evaluate_target
        task_file = target.task_file
        try:
            task, source_yaml = load_task(task_file)
        except Exception as e:
            console.print(f"[red]✗ Failed to load task:[/red] {escape(str(e))}")
            raise typer.Exit(1) from e
        work_dir = target.target

    if not work_dir.is_dir():
        console.print(f"[red]✗ Work directory is not a directory:[/red] {escape(str(work_dir))}")
        raise typer.Exit(1)

    # Evaluate-only mode bypasses experiment resolution and CLI overrides, so
    # `agent.type` may be unset. It is used only for result labeling here.
    if task.agent is None:
        task.agent = parse_agent_config(type=AgentKind.CLAUDE_CODE)
    elif task.agent.type is None:
        task.agent = parse_agent_config(**{**task.agent.model_dump(exclude_unset=True), "type": AgentKind.CLAUDE_CODE})

    if prior is not None:
        verify_reference_unchanged(prior, task, task_file)
        # BEFORE anything grades: taken inside _write_back it would capture an
        # ALREADY-GRADED record whenever --run-dir points at the target run dir.
        # Rationale: .claude/notes/isolation.md § Detached grading from the CLI
        back_up_pre_grade_record(target.target)

    return _ResolvedInputs(
        target=target,
        task=task,
        source_yaml=source_yaml,
        work_dir=work_dir,
        task_file=task_file,
        prior=prior,
    )


def _replicate_index_of(run_dir: Path) -> int:
    """Recover the replicate index a run directory encodes in its leaf name.

    Preservation lays runs out as ``<run>/<variant>/<task>/<NN>``. Hardcoding 0
    would relabel every replicate but the first as replicate 0.
    """
    try:
        return int(run_dir.name)
    except ValueError:
        return 0


def evaluate_command(
    task_or_run_dir: Path = typer.Argument(  # noqa: B008
        ...,
        # One metavar per positional: a composite on the first plus an empty one
        # on the second rendered `[TASK_FILE] TARGET []`.
        metavar="TASK_FILE_OR_RUN_DIR",
        help="Task YAML file, or (when it is the only argument) a finished run directory.",
        exists=True,
    ),
    work_dir: Path | None = typer.Argument(  # noqa: B008
        None,
        # No metavar="" here: an empty one leaks a bare `[]` into the usage line
        # and the arguments table.
        help="Directory containing the code to evaluate. Omit when TASK_FILE is a run directory.",
    ),
    workspace: Path | None = typer.Option(  # noqa: B008
        None,
        "--workspace",
        help=(
            "Grade this directory instead of the run's own artifacts. Run-directory mode only (e.g. a verifier's /app)."
        ),
    ),
    in_place: bool | None = typer.Option(
        None,
        "--in-place/--copy",
        help=(
            "Grade the workspace where it is, or copy it into a fresh sandbox first. "
            "Default: in-place for a run directory, copy for a plain work directory. "
            "Copying filters build output (node_modules, dist, build, .venv), so a "
            "criterion that reads those needs --in-place."
        ),
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
        help=(
            "Move sandbox artifacts to run directory (default: preserve). The temp sandbox is "
            "always removed. Ignored when grading in place (the default for a run directory) — "
            "an adopted directory is never moved or deleted."
        ),
    ),
    allow_recorded_commands: bool = typer.Option(
        False,
        "--allow-recorded-commands",
        help=(
            "Accept the capabilities rebuilt from the run directory's own task.json: shell "
            "(run_command criteria, pre_run/post_run) and, for a `driver: docker` row, starting a "
            "container of the image the record names with your credentials in its environment. A "
            "run directory is a shareable artifact, so its recorded config is untrusted input; "
            "without this, grading refuses rather than running it here."
        ),
    ),
    allow_host_grading: bool = typer.Option(
        False,
        "--allow-host-grading",
        help=(
            "Grade a `driver: docker` run on THIS HOST instead of in a container of the task's "
            "own image (the default). For a machine with no docker, or criteria you know are "
            "host-portable. The criteria then run against a filesystem lacking the container's "
            "paths and toolchain, so scores may differ from the run; such rows are stamped "
            "graded_on_host."
        ),
    ),
    run_dir: Path | None = typer.Option(  # noqa: B008
        None,
        "--run-dir",
        help="Where the graded task.json lands (default: auto-generated timestamped directory in runs/)",
    ),
    format: str | None = typer.Option(
        None,
        "--format",
        help=(
            "Only 'harbor' is supported: grade a directory whose agent phase ran OUTSIDE this "
            "process (a Harbor agent's `coder-eval execute --format harbor`) by hydrating trajectory "
            "context from an ATIF trajectory.json instead of a run directory's task.json. Requires "
            "--trajectory and the two-argument `TASK_FILE WORK_DIR` form."
        ),
    ),
    trajectory: Path | None = typer.Option(  # noqa: B008
        None,
        "--trajectory",
        help="Path to an ATIF trajectory.json to hydrate trajectory context from. Required with --format harbor.",
        exists=True,
        dir_okay=False,
    ),
) -> None:
    """Evaluate criteria against a directory, or re-grade a finished run.

    Two shapes, told apart by whether the target holds a task.json:

    \b
    Grade a directory against a task (no agent runs):
        coder-eval evaluate tasks/hello.yaml ./my_solution

    \b
    Re-grade a finished run — including one produced by `coder-eval execute`,
    which leaves every task NOT_GRADED. The run's own task.json supplies the
    resolved config AND the trajectory, so criteria that read the agent's tool
    calls score exactly as they would have during the run:
        coder-eval execute tasks/hello.yaml --run-dir ./r
        coder-eval evaluate ./r/default/hello/00

    \b
    Iterate on criteria against a run you already paid for, by passing a task
    file over a run directory (its trajectory and workspace are still used):
        coder-eval evaluate tasks/hello.edited.yaml ./r/default/hello/00
    """
    run_evaluation(
        task_or_run_dir=task_or_run_dir,
        work_dir=work_dir,
        workspace=workspace,
        in_place=in_place,
        verbose=verbose,
        preserve=preserve,
        allow_recorded_commands=allow_recorded_commands,
        allow_host_grading=allow_host_grading,
        run_dir=run_dir,
        format=format,
        trajectory=trajectory,
    )


def run_evaluation(
    *,
    task_or_run_dir: Path,
    work_dir: Path | None = None,
    workspace: Path | None = None,
    in_place: bool | None = None,
    verbose: bool = False,
    preserve: bool = True,
    allow_recorded_commands: bool = False,
    allow_host_grading: bool = False,
    run_dir: Path | None = None,
    format: str | None = None,
    trajectory: Path | None = None,
) -> None:
    """The body of ``coder-eval evaluate``, with real Python defaults.

    Split from the Typer signature so it is directly callable: invoking a Typer
    command function in-process hands every unspecified option an ``OptionInfo``
    sentinel rather than its default, which silently turns ``in_place=None`` into
    a truthy object. Callers (tests, and any library use) call this instead.
    """
    setup_logging(verbose=verbose)

    console.print("\n[bold]Evaluating Criteria[/bold]\n")

    inputs = _resolve_inputs(
        task_or_run_dir,
        work_dir,
        workspace,
        allow_recorded_commands=allow_recorded_commands,
        in_place=in_place,
        allow_host_grading=allow_host_grading,
    )
    task = inputs.task
    source_yaml = inputs.source_yaml
    graded_dir = inputs.work_dir
    task_file = inputs.task_file
    prior = inputs.prior
    target = inputs.target

    if format is not None and format != "harbor":
        console.print(f"[red]✗ Unsupported --format {format!r}. Supported: harbor.[/red]")
        raise typer.Exit(1)
    if format == "harbor":
        if target.mode is not EvaluateMode.WORK_DIR:
            console.print(
                "[red]✗ --format harbor only applies to the two-argument `TASK_FILE WORK_DIR` form — "
                + "a run directory already carries its own trajectory in task.json.[/red]"
            )
            raise typer.Exit(1)
        if trajectory is None:
            console.print("[red]✗ --format harbor requires --trajectory <path to trajectory.json>.[/red]")
            raise typer.Exit(1)
        try:
            atif_trajectory = Trajectory.model_validate_json(trajectory.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            console.print(f"[red]✗ Could not read {trajectory} as an ATIF trajectory:[/red] {escape(str(e))}")
            raise typer.Exit(1) from e
        prior = seed_from_atif_trajectory(
            atif_trajectory,
            task_id=task.task_id,
            task_description=task.description,
        )

    if not task.success_criteria:
        # This guard covers the orchestrator-direct branch below, which never
        # calls `regrade_in_place` and would otherwise finalize a criteria-free
        # task as SUCCESS at weighted_score 0.0.
        # Rationale: .claude/notes/orchestration.md § Refusing a criteria-free task under grade
        console.print(
            f"[red]✗ Task {task.task_id!r} has no `success_criteria` and cannot be graded "
            + "(it would silently score SUCCESS at weighted_score 0.0). Add at least one criterion.[/red]"
        )
        raise typer.Exit(1)

    grade_in_place = resolve_grade_in_place(target, in_place)

    try:
        prepared_run_dir = prepare_run_directory(run_dir)
    except Exception as e:
        console.print(f"[red]✗ Failed to prepare run directory:[/red] {escape(str(e))}")
        raise typer.Exit(1) from e

    # Branching on ``prior is not None`` directly, and building the sandbox inside
    # the branch that uses it, so NEITHER value is Optional at its use site --
    # `grading_sandbox_config` REFUSES a docker driver, so building one up front
    # fired that refusal before the branch that no longer needs it.
    # Rationale: .claude/notes/isolation.md § Detached grading from the CLI
    async def _setup_and_run() -> EvaluationResult:
        if grade_in_place and prior is not None:
            # Delegate to the shared re-grade core rather than restating it: two
            # copies of "how to re-grade" drift into two verdicts for one run.
            return await regrade_in_place(
                task=task,
                prior=prior,
                workspace=graded_dir,
                run_dir=prepared_run_dir,
                task_file=task_file,
                source_yaml=source_yaml,
                variant_id=prior.variant_id,
                replicate_index=_replicate_index_of(target.target),
                allow_host_grading=allow_host_grading,
            )
        sandbox_config = grading_sandbox_config(task, allow_host_grading=allow_host_grading)
        if not grade_in_place:
            # Copy path: preload the sandbox with the work dir as a template source.
            template_source = TemplateDirSource(path=str(graded_dir.resolve()))
            sandbox_config.template_sources = [template_source, *(sandbox_config.template_sources or [])]
        task_dir = task_file.parent.resolve() if task_file is not None else None
        sandbox = Sandbox(sandbox_config, task_id=task.task_id, task_dir=task_dir)
        if grade_in_place:
            await asyncio.to_thread(sandbox.adopt, graded_dir)
        else:
            await asyncio.to_thread(sandbox.setup)
        orchestrator = Orchestrator(
            task=task,
            run_dir=prepared_run_dir,
            # An adopted directory is the caller's; never move or delete it.
            preservation_mode=(
                PreservationMode.NONE
                if grade_in_place
                else (PreservationMode.MOVE_ON_WRITE if preserve else PreservationMode.NONE)
            ),
            task_file=task_file,
            sandbox=sandbox,
            variant_id=prior.variant_id if prior is not None else "evaluate",
            replicate_index=_replicate_index_of(target.target),
            source_yaml=source_yaml,
            prior_result=prior,
        )
        graded = await orchestrator.run()
        # HAZARD: the same stamp the delegating branch gets from
        # `regrade_in_place`. CLAUDE.md, the user guide and CE051's own noqa all
        # state it as unconditional.
        # Rationale: .claude/notes/isolation.md § Detached grading from the CLI
        stamp_host_grading(graded, task)
        return graded

    try:
        result = asyncio.run(_setup_and_run())
    except RegradeError as e:
        # Rendered like the three sibling handlers above: unwrapped, these
        # operator-facing messages arrived as the tail of a stack trace.
        console.print(f"[red]✗ {escape(str(e))}[/red]")
        raise typer.Exit(1) from e
    _report_and_exit(result, task=task, prior=prior, target=target, prepared_run_dir=prepared_run_dir)


def _report_and_exit(
    result: EvaluationResult,
    *,
    task: TaskDefinition,
    prior: EvaluationResult | None,
    target: EvaluateTarget,
    prepared_run_dir: Path,
) -> None:
    """Render the graded row, write it back, and choose the exit code.

    Split out of ``run_evaluation`` because it answers a different question —
    what to TELL the operator about a result that already exists — and because
    the two together grew past the function-size bound the moment the inherited
    status handling landed. Always raises ``typer.Exit``.
    """

    # BEFORE the count guard below: a grading crash returns a populated ERROR result
    # with an EMPTY criteria list, so the count check fires first and hides the real
    # error. `inherited` says whether the terminal status describes THIS pass or was
    # carried over from the run being graded.
    # Rationale: .claude/notes/isolation.md § Detached grading from the CLI
    inherited = prior is not None and prior.final_status.is_execution_fact

    if result.final_status is FinalStatus.ERROR and not inherited:
        console.print(f"\n[red]✗ Evaluation error: {result.error_message}[/red]")
        if prior is not None:
            # A grading-time crash is not a verdict about the run. Leaving ERROR
            # on disk replaces a re-gradeable NOT_GRADED row with one BOTH
            # commands treat as permanently complete.
            restore_pre_grade_record(target.target)
            console.print(
                f"[yellow]⚠[/] Grading errored; {target.target / TASK_JSON_FILENAME} is left "
                + "ungraded so the run stays re-gradeable."
            )
        raise typer.Exit(1)

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
        console.print(f"  [dim]{cr.description}[/dim]")
        console.print(f"  [dim]Score: {cr.score:.2f}[/dim]")
        if cr.details:
            console.print(f"  [dim]Details: {cr.details}[/dim]")
        if cr.error:
            console.print(f"  [red]Error: {cr.error}[/red]")
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
        console.print(f"  [dim]Informational (weight=0, not gated): {informational}[/dim]")
    console.print(f"\n[dim]Run directory: {prepared_run_dir}[/dim]")
    if result.sandbox_path:
        console.print(f"[dim]Artifacts: {result.sandbox_path}[/dim]")

    # RUN_DIR mode, not merely `prior is not None`: `--format harbor` seeds a
    # SYNTHETIC prior on the WORK_DIR shape, which is not a run directory and has
    # no `task.execute.json` sibling to preserve.
    # Rationale: .claude/notes/isolation.md § Detached grading from the CLI
    if prior is not None and target.mode is EvaluateMode.RUN_DIR:
        console.print(
            f"[dim]Re-graded {prior.final_status.value} → {result.final_status.value} "
            + f"over {len(result.iterations)} recorded turn(s).[/dim]"
        )
        _write_back(target.target, result, prepared_run_dir)

    if result.final_status.is_execution_fact:
        # The criteria tally is real -- it is why the table above still renders --
        # but it is not the row's outcome, and the exit code must agree with
        # run.json rather than with the tally.
        console.print(
            f"\n[red]Criteria: {passed}/{total} passed, but the run itself ended as "
            + f"{result.final_status.value} — grading cannot overturn that.[/red]"
        )
        raise typer.Exit(1)
    if failed == 0:
        console.print("\n[green]All criteria passed! ✓[/green]")
        raise typer.Exit(0)
    else:
        console.print(f"\n[red]{failed} criterion/criteria failed.[/red]")
        raise typer.Exit(1)


def _write_back(run_dir: Path, result: EvaluationResult, grading_run_dir: Path) -> None:
    """Replace the graded run's ``task.json`` with the verdict, keeping a copy of the original.

    Updating in place is what makes the rest of the toolchain free: the owning run's
    ``run.json`` is then rebuilt from these rows (``_refresh_run_summary``), and every
    report and evalboard view reads the graded row.

    The pre-grade original is kept alongside as ``task.execute.json`` so the
    ungraded record is auditable — the write is not a silent overwrite of the
    only evidence that the run was executed separately.
    """
    target = run_dir / TASK_JSON_FILENAME
    backup = run_dir / PRE_GRADE_JSON_FILENAME
    if target.is_symlink():
        # HAZARD: a run directory is a shareable artifact, so following a symlink
        # here is an arbitrary-file-overwrite primitive on the grader's host.
        console.print(f"[yellow]⚠[/] {target} is a symlink; refusing to write through it.")
        return
    try:
        # Atomic, matching the orchestrator's own writer: a torn write makes the
        # row parse as malformed, which a later --resume re-pays for.
        write_text_atomic(target, result.model_dump_json(indent=2, exclude=TASK_JSON_TRANSCRIPT_EXCLUDE))
    except OSError as e:
        # Never fail the grade over the write-back: the verdict was computed and
        # already printed, and the fresh run dir holds its own task.json.
        console.print(f"[yellow]⚠[/] Could not update {target}: {e}")
        return
    console.print(f"[dim]Updated {escape(str(target))} (original kept as {backup.name}).[/dim]")
    _refresh_run_summary(run_dir, grading_run_dir)


def _refresh_run_summary(row_dir: Path, grading_run_dir: Path) -> None:
    """Rebuild the run-level ``run.json`` of the run that owns ``row_dir``, best-effort.

    Never raises and never changes the exit code: the verdict is already computed and
    printed. A row with no ``run.json`` above it gets none. Skipped when this grade wrote
    its own ``task.json`` inside the owning run (``grading_run_dir`` under it and not the
    row itself), because that record would be counted as a second row.

    Rationale: .claude/notes/isolation.md § Detached grading from the CLI
    """
    try:
        root = run_summary_rebuild.find_run_root(row_dir)
        if root is None:
            console.print(
                f"[dim]{escape(str(row_dir))} is not inside a run directory; "
                + "no run-level run.json was refreshed.[/dim]"
            )
            return
        grading = grading_run_dir.resolve()
        if grading != row_dir.resolve() and grading.is_relative_to(root):
            console.print(
                f"[yellow]⚠[/] The grading run dir {escape(str(grading_run_dir))} is inside the run at "
                + f"{escape(str(root))}; its task.json would count as a second row, so run.json was not "
                + "refreshed. Grade with a --run-dir outside the run."
            )
            return
        summary = run_summary_rebuild.rebuild_run_summary(root)
    except Exception as e:
        console.print(f"[yellow]⚠[/] Could not refresh the run-level run.json: {escape(str(e))}")
        return
    if summary is None:
        console.print(f"[yellow]⚠[/] No finalized task.json under {escape(str(root))}; its run.json was not refreshed.")
        return
    console.print(f"[dim]Refreshed {escape(str(root / 'run.json'))}[/dim]")
