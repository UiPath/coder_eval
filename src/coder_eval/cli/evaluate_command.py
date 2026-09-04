"""Evaluate command - run criteria against a directory or re-grade a finished run."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import typer

from ..evaluation.judge_persistence import TASK_JSON_TRANSCRIPT_EXCLUDE
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
            target, workspace, allow_recorded_commands=allow_recorded_commands, in_place=in_place
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
) -> _ResolvedInputs:
    """The mode-specific half of :func:`_resolve_inputs`."""
    prior: EvaluationResult | None = None
    if target.mode is EvaluateMode.RUN_DIR and target.task_file is not None:
        # `is_run_dir` is a filename probe, so a plain work directory holding an
        # unrelated file called task.json lands here. That would abort the
        # pre-existing `evaluate <task.yaml> <dir>` form on a pydantic wall the
        # user can only escape by renaming their own file. The task file is
        # already in hand, so fall back to the shape they asked for.
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
            # pre_run/post_run and the sandbox's installers both run only on the
            # --copy path (an adopted workspace must not have its hooks re-run
            # over the agent's deliverables, and `adopt` installs nothing), so
            # in place they are not capabilities the run dir can reach.
            #
            # Derived through the SAME function `run_evaluation` uses, not
            # restated. This value decides whether recorded shell is refused, so
            # a second copy of the rule would keep answering the old question if
            # the default ever moved — and silently stop covering commands that
            # then do run.
            setup_will_run = not resolve_grade_in_place(target, in_place)
            task, source_yaml = task_from_prior(
                prior,
                target.target,
                allow_recorded_commands=allow_recorded_commands,
                include_setup_phase=setup_will_run,
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
            console.print(f"[red]✗ Failed to load task:[/red] {e}")
            raise typer.Exit(1) from e
        work_dir = target.target

    if not work_dir.is_dir():
        console.print(f"[red]✗ Work directory is not a directory:[/red] {work_dir}")
        raise typer.Exit(1)

    # Evaluate-only mode bypasses experiment resolution + CLI overrides, so
    # `agent` may be None or `agent.type` may be unset for tasks that defer
    # those to the experiment / CLI layers. The orchestrator only uses
    # `agent.type` for result labeling here (no agent is created), so a
    # default is safe.
    if task.agent is None:
        task.agent = parse_agent_config(type=AgentKind.CLAUDE_CODE)
    elif task.agent.type is None:
        task.agent = parse_agent_config(**{**task.agent.model_dump(exclude_unset=True), "type": AgentKind.CLAUDE_CODE})

    if prior is not None:
        verify_reference_unchanged(prior, task, task_file)
        # Snapshot the ungraded record BEFORE anything grades. Taking it inside
        # _write_back instead would capture an ALREADY-GRADED record whenever
        # --run-dir points at the target run dir (the orchestrator writes there
        # first), destroying the very evidence the copy exists to preserve.
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
        # One metavar per positional, so the usage line reads as Click renders
        # it. A composite metavar on the first ("[TASK_FILE] TARGET") plus an
        # empty one on the second produced `[TASK_FILE] TARGET []`.
        metavar="TASK_FILE_OR_RUN_DIR",
        help="Task YAML file, or (when it is the only argument) a finished run directory.",
        exists=True,
    ),
    work_dir: Path | None = typer.Argument(  # noqa: B008
        None,
        # No metavar="" here: an empty one leaks a bare `[]` into both the usage
        # line and the arguments table. The first positional's metavar already
        # spells out the two shapes.
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
            "Accept shell commands (run_command criteria, pre_run/post_run) rebuilt from the run "
            "directory's own task.json. A run directory is a shareable artifact, so its recorded "
            "config is untrusted input; without this, grading refuses rather than running it here."
        ),
    ),
    allow_host_grading: bool = typer.Option(
        False,
        "--allow-host-grading",
        help=(
            "Grade a `driver: docker` run on this host. Grading cannot start a container, so the "
            "criteria run against a filesystem that lacks the container's paths and toolchain — "
            "scores may differ from the run. Such rows are stamped graded_on_host."
        ),
    ),
    run_dir: Path | None = typer.Option(  # noqa: B008
        None,
        "--run-dir",
        help="Where the graded task.json lands (default: auto-generated timestamped directory in runs/)",
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
    )
    task = inputs.task
    source_yaml = inputs.source_yaml
    graded_dir = inputs.work_dir
    task_file = inputs.task_file
    prior = inputs.prior
    target = inputs.target

    grade_in_place = resolve_grade_in_place(target, in_place)

    try:
        prepared_run_dir = prepare_run_directory(run_dir)
    except Exception as e:
        console.print(f"[red]✗ Failed to prepare run directory:[/red] {e}")
        raise typer.Exit(1) from e

    try:
        sandbox_config = grading_sandbox_config(task, allow_host_grading=allow_host_grading)
    except RegradeError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(1) from e
    if not grade_in_place:
        # Copy path: preload the sandbox with the work dir as a template source.
        template_source = TemplateDirSource(path=str(graded_dir.resolve()))
        sandbox_config.template_sources = [template_source, *(sandbox_config.template_sources or [])]

    task_dir = task_file.parent.resolve() if task_file is not None else None
    sandbox = Sandbox(sandbox_config, task_id=task.task_id, task_dir=task_dir)

    async def _setup_and_run() -> EvaluationResult:
        if grade_in_place and prior is not None:
            # Delegate to the shared re-grade core. Restating its body here is
            # how this path and `run --resume` came to differ (replicate_index,
            # error semantics) while CLAUDE.md called regrade.py the single
            # implementation — two copies of "how to re-grade" drift into two
            # verdicts for the same run.
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
        # Same stamp the delegating branch gets from `regrade_in_place`. Line 357
        # above accepted the docker→host downgrade for THIS branch too, and
        # CLAUDE.md, the user guide and CE051's own noqa all state the stamp as
        # unconditional — so `evaluate <run_dir> --copy --allow-host-grading`
        # was writing an unstamped host verdict that nothing downstream could
        # tell apart from a container-graded one.
        stamp_host_grading(graded, task)
        return graded

    result = asyncio.run(_setup_and_run())
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

    # BEFORE the count guard below. A grading crash returns a populated ERROR
    # result with an EMPTY criteria list (Orchestrator.run() converts internal
    # failures into a result rather than raising), so the count check fires
    # first and the user is told only "Result count mismatch: got 0, expected 2"
    # — the real error is never printed, and the "still re-gradeable" notice is
    # unreachable on exactly the path it was written for.
    # Whether the terminal status describes THIS pass or was carried over from
    # the run being graded. `Orchestrator._terminal_status` preserves a prior
    # execution fact (TIMEOUT / ERROR / BUILD_FAILED / a budget stop) because
    # grading may not overturn it — so reading `result.final_status` as this
    # pass's own outcome misreports both arms below. It made a preserved ERROR
    # print the ORIGINAL run's crash message as though grading had crashed,
    # claim the row was "left ungraded" (it was not — the restored record still
    # reads ERROR), and throw away a verdict that had just been computed at
    # 1.000; and it made a preserved TIMEOUT exit 0 under "All criteria passed",
    # so a CI wrapper reading the exit code goes green on a row run.json counts
    # as failed.
    inherited = prior is not None and prior.final_status.is_execution_fact

    if result.final_status is FinalStatus.ERROR and not inherited:
        console.print(f"\n[red]✗ Evaluation error: {result.error_message}[/red]")
        if prior is not None:
            # A grading-time crash (a failing checker, an unreachable judge) is
            # not a verdict about the run. Leaving ERROR on disk would replace a
            # perfectly re-gradeable NOT_GRADED row with one BOTH commands treat
            # as permanently complete, so the run could never be graded again
            # without hand-restoring task.execute.json.
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

    if prior is not None:
        console.print(
            f"[dim]Re-graded {prior.final_status.value} → {result.final_status.value} "
            + f"over {len(result.iterations)} recorded turn(s).[/dim]"
        )
        _write_back(target.target, result)

    if result.final_status.is_execution_fact:
        # The criteria tally is real and worth printing — it is why the table
        # above still renders — but it is not the row's outcome. run.json will
        # count this row under its preserved status, and the exit code must
        # agree with run.json rather than with the tally.
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


def _write_back(run_dir: Path, result: EvaluationResult) -> None:
    """Replace the graded run's ``task.json`` with the verdict, keeping a copy of the original.

    Updating in place is what makes the rest of the toolchain free: plain
    ``coder-eval aggregate <run>`` then rebuilds ``run.json`` from these rows
    with no new code, and every report and evalboard view reads the graded row.

    The pre-grade original is kept alongside as ``task.execute.json`` so the
    ungraded record is auditable — the write is not a silent overwrite of the
    only evidence that the run was executed separately.
    """
    target = run_dir / TASK_JSON_FILENAME
    backup = run_dir / PRE_GRADE_JSON_FILENAME
    if target.is_symlink():
        # A run directory is a shareable artifact, so its task.json is untrusted
        # input. Following a symlink here turns `evaluate <run_dir>` into an
        # arbitrary-file-overwrite primitive on the grader's host.
        console.print(f"[yellow]⚠[/] {target} is a symlink; refusing to write through it.")
        return
    try:
        # Atomic, matching the orchestrator's own task.json writer: a torn write
        # here makes the row parse as malformed, which a later --resume reads as
        # "not complete" and re-pays for the agent.
        write_text_atomic(target, result.model_dump_json(indent=2, exclude=TASK_JSON_TRANSCRIPT_EXCLUDE))
    except OSError as e:
        # Never fail the grade over the write-back: the verdict was computed and
        # already printed, and the fresh run dir holds its own task.json.
        console.print(f"[yellow]⚠[/] Could not update {target}: {e}")
        return
    console.print(
        f"[dim]Updated {target} (original kept as {backup.name}); "
        + "run `coder-eval aggregate` to refresh run.json.[/dim]"
    )
