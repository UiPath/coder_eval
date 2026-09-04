"""Grade a run that already executed — the shared core behind two callers.

``coder-eval execute`` leaves every row ``NOT_GRADED``. Two commands can supply
the verdict afterwards, and both must do it identically:

* ``coder-eval evaluate <run_dir>`` — grade one finished task explicitly.
* ``coder-eval run --resume`` — grade the ungraded rows it finds in the run dir
  instead of re-executing them (see ``partition_for_resume``).

The logic lives here rather than in ``cli/`` because the resume path is not a CLI
concern, and because two copies of "how to re-grade" would drift into two
different verdicts for the same run. Errors surface as :class:`RegradeError`, a
plain exception the CLI wraps into its own error type — ``orchestration/`` must
not depend on the CLI layer (CE004).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from coder_eval.models import (
    EvaluationResult,
    PreservationMode,
    SandboxConfig,
    TaskConfigRecord,
    TaskDefinition,
)
from coder_eval.path_utils import PRE_GRADE_JSON_FILENAME, TASK_JSON_FILENAME, write_text_atomic
from coder_eval.sandbox import Sandbox


logger = logging.getLogger(__name__)

ARTIFACTS_DIRNAME = "artifacts"


class RegradeError(Exception):
    """A finished run cannot be re-graded as asked."""


def load_prior_result(run_dir: Path) -> EvaluationResult:
    """Read a finished run's ``task.json``."""
    path = run_dir / TASK_JSON_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise RegradeError(f"Cannot read {path}: {e}") from e
    try:
        return EvaluationResult.model_validate_json(raw)
    except ValueError as e:
        raise RegradeError(f"{path} is not a readable EvaluationResult: {e}") from e


def task_from_prior(
    prior: EvaluationResult,
    run_dir: Path,
    *,
    allow_recorded_commands: bool = False,
    include_setup_phase: bool = True,
) -> tuple[TaskDefinition, str]:
    """Rebuild the executed task from the run's own recorded config.

    Rebuilding from ``task_config.resolved`` rather than re-reading the YAML is
    what makes the grade describe the run that happened: ``resolved`` is the
    post-merge definition, so variant overrides, ``-D`` flags and dataset row
    expansion are all already baked in. Re-loading the source YAML would silently
    grade a DIFFERENT task whenever any of those were used.

    Falls back to the source YAML only when ``resolved`` will not validate (a
    schema change since the run), and says so loudly — a quiet fallback would
    reintroduce exactly the drift above.

    ``allow_recorded_commands`` gates the shell half. See
    :func:`check_embedded_commands`.
    """
    record = prior.task_config
    if record is None:
        raise RegradeError(
            f"{run_dir / TASK_JSON_FILENAME} carries no task_config, so the executed task cannot be "
            + "rebuilt. Pass the task file explicitly: coder-eval evaluate <task.yaml> <run_dir>"
        )
    try:
        task = TaskDefinition.model_validate(record.resolved)
    except ValueError as e:
        return _fall_back_to_source(
            record, run_dir, e, allow_recorded_commands=allow_recorded_commands, include_setup_phase=include_setup_phase
        )
    check_embedded_commands(
        task, run_dir, allow_recorded_commands=allow_recorded_commands, include_setup_phase=include_setup_phase
    )
    return task, record.source_yaml


def embedded_commands(task: TaskDefinition, *, include_setup_phase: bool = True) -> list[str]:
    """Every shell command a rebuilt task definition would run on this host.

    ``include_setup_phase`` covers the two capability families that exist only on
    the ``--copy`` path: ``pre_run`` / ``post_run``, and the sandbox's own
    provisioning. Both are SKIPPED when grading in place (``Sandbox.adopt`` runs
    no installer, and re-running the hooks would overwrite the agent's
    deliverables before the criteria read them), so on that path they are not a
    capability the run dir has.

    Sandbox provisioning is the half this gate originally missed, and it was the
    worst one. ``grading_sandbox_config`` carries the recorded ``sandbox`` block
    through untouched, and the ``--copy`` branch then calls ``Sandbox.setup``,
    which reaches ``uv pip install <recorded packages>``, ``npm install <recorded
    packages>`` and ``git clone <recorded url>``. A package name is arbitrary
    code at install time. Because the scan walked only ``success_criteria``, a
    shared run directory whose criteria were all ``file_exists`` sailed through
    the gate and still ran installers of the attacker's choosing.

    ``isinstance`` narrowing, never ``getattr(c, "command", None)``: an untyped
    string probe over a discriminated union is invisible to pyright, so renaming
    a field silently degrades the only guard on this path to a permanent no-op —
    the exact hazard ``models/tasks.py`` already documents in prose. It also
    cannot reach ``agent_judge``, whose ``bash`` tooling is the widest blast
    radius of the three.
    """
    from coder_eval.models import (
        AgentJudgeCriterion,
        LLMJudgeCriterion,
        RepoSource,
        RunCommandCriterion,
        UiPathEvalCriterion,
    )

    commands: list[str] = []
    for c in task.success_criteria:
        if isinstance(c, RunCommandCriterion):
            commands.append(c.command)
        elif isinstance(c, AgentJudgeCriterion):
            # No command string of its own: it spawns a Claude Code SDK agent
            # with tool access (Bash included) under the grader's credentials,
            # which is a strictly wider capability than one shell line.
            commands.append(f"<agent_judge: spawns a tool-using agent — {c.description}>")
        elif isinstance(c, LLMJudgeCriterion):
            # No shell, but it spends the grader's model budget and ships the
            # graded artifacts (and optionally the trajectory) to a provider of
            # the recorded config's choosing. That is a capability the operator
            # should approve, even though nothing executes locally.
            commands.append(f"<llm_judge: sends artifacts to {c.model} on your credentials>")
        elif isinstance(c, UiPathEvalCriterion):
            # Builds and shells `uv run uipath eval …`. Every argument is
            # shlex-quoted, so this is disclosure rather than injection — but it
            # is still a subprocess the recorded config chose to start.
            commands.append(f"uv run uipath eval {c.agent_name} {c.eval_set}")
    if include_setup_phase:
        commands += [c.command for c in task.pre_run] + [c.command for c in task.post_run]
        sandbox = task.sandbox
        if sandbox.python is not None and sandbox.python.env_packages:
            commands.append(f"uv pip install {' '.join(sandbox.python.env_packages)}")
        if sandbox.node is not None and sandbox.node.env_packages:
            commands.append(f"npm install {' '.join(sandbox.node.env_packages)}")
        for source in sandbox.template_sources or []:
            if isinstance(source, RepoSource):
                commands.append(f"git clone -- {source.url}")
    return commands


def check_embedded_commands(
    task: TaskDefinition, run_dir: Path, *, allow_recorded_commands: bool, include_setup_phase: bool = True
) -> None:
    """Refuse — or at minimum name — the shell a rebuilt config will run here.

    ``task_config.resolved`` is data that travels inside a run directory, and a
    run directory is a shareable artifact — the detached-grading flow exists so
    one machine can execute and another can grade. Rebuilding the task from it
    means the *run dir* decides what ``run_command`` criteria the grader runs,
    with the grader's environment (API keys, cloud credentials, SSH agent).

    A warning is not a control: it is printed as the command is already being
    prepared, and nobody reads a log line fast enough to stop it. So a recorded
    config that carries shell is REFUSED unless the operator opted in. The common
    case — ``execute`` then ``evaluate`` on your own machine — is unaffected
    whenever the criteria are file/JSON checks, and the opt-in is one flag.

    Passing the task file explicitly (``evaluate <task.yaml> <run_dir>``) also
    bypasses this: that config came from the operator, not from the artifact.
    """
    commands = embedded_commands(task, include_setup_phase=include_setup_phase)
    if not commands:
        return
    rendered = "; ".join(commands)
    if not allow_recorded_commands:
        raise RegradeError(
            f"The config recorded in {run_dir / TASK_JSON_FILENAME} would run {len(commands)} shell "
            + f"command(s) on this host with your environment: {rendered}\n"
            + "A run directory is a shareable artifact, so its recorded config is untrusted input. "
            + "Re-run with --allow-recorded-commands to accept them, or pass the task file "
            + "explicitly: coder-eval evaluate <task.yaml> <run_dir>"
        )
    logger.warning(
        "Grading %s runs %d shell command(s) taken from that run's own recorded config: %s",
        run_dir,
        len(commands),
        rendered,
    )


def _fall_back_to_source(
    record: TaskConfigRecord,
    run_dir: Path,
    e: ValueError,
    *,
    allow_recorded_commands: bool,
    include_setup_phase: bool = True,
) -> tuple[TaskDefinition, str]:
    """The loud source-YAML fallback for a resolved config that no longer validates."""
    from .task_loader import load_task

    if not record.source_file or not Path(record.source_file).is_file():
        raise RegradeError(
            f"The resolved task config in {run_dir / TASK_JSON_FILENAME} no longer validates ({e}), and "
            + "its source YAML is unavailable. Pass the task file explicitly."
        ) from e
    logger.warning(
        "The recorded resolved config does not validate (%s); falling back to %s. Variant "
        + "overrides, -D flags and dataset expansion from the original run are NOT reapplied, "
        + "so this grade may not match what ran.",
        e,
        record.source_file,
    )
    task, source_yaml = load_task(Path(record.source_file))
    check_embedded_commands(
        task, run_dir, allow_recorded_commands=allow_recorded_commands, include_setup_phase=include_setup_phase
    )
    return task, source_yaml


def default_workspace(run_dir: Path, prior: EvaluationResult) -> Path:
    """Locate the workspace a finished run left behind.

    ``sandbox_path`` is authoritative when it still exists — it is where the run
    actually worked. Otherwise fall back to the preserved artifacts tree, where
    preservation nests the workspace under the task id.

    Raises rather than guessing when neither is conclusive. Guessing is worse
    than failing here: grading the WRONG directory makes every path-relative
    criterion fail as a locating artifact rather than as a verdict, and it
    reports that as an ordinary score.
    """
    if prior.sandbox_path:
        recorded = Path(prior.sandbox_path)
        if recorded.is_dir():
            if not _is_within(recorded, run_dir):
                # An absolute path out of the run's own task.json, which is
                # untrusted input for a shared run dir. Criteria execute with
                # cwd there and may mutate it, so an out-of-tree location has to
                # be the operator's explicit choice.
                raise RegradeError(
                    f"The recorded sandbox_path ({recorded}) is outside the run directory "
                    + f"({run_dir}). Pass --workspace explicitly to grade it."
                )
            return recorded

    artifacts = run_dir / ARTIFACTS_DIRNAME
    if not artifacts.is_dir():
        raise RegradeError(
            f"No workspace to grade: {artifacts} does not exist and the recorded sandbox_path "
            + f"({prior.sandbox_path or 'unset'}) is gone. The run was probably made with "
            + "--preservation-mode NONE."
        )
    # The exact path, not a heuristic. `task_id` may contain "/" (dataset rows
    # are "<suite>/<row>"), so "the single child of artifacts/" resolves one
    # level too high for every row task.
    #
    # Containment-checked like the sandbox_path branch above, and for the same
    # reason: `task_id` is an unvalidated string out of the run's own task.json,
    # so `"../../../../home/victim"` joins to a real directory that `is_dir()`
    # happily confirms. Every run_command criterion then executes with that as
    # its cwd. The two branches read the same untrusted record; only one of them
    # used to check.
    by_task_id = artifacts / prior.task_id
    if by_task_id.is_dir():
        if not _is_within(by_task_id, artifacts):
            raise RegradeError(
                f"The recorded task_id ({prior.task_id!r}) resolves outside {artifacts}. "
                + "Pass --workspace explicitly to grade a directory outside the run."
            )
        return by_task_id

    children = [p for p in sorted(artifacts.iterdir()) if p.is_dir()]
    if not children:
        # A flat artifacts dir (no subdirectory) means the workspace IS artifacts/.
        return artifacts
    if len(children) == 1:
        return children[0]
    raise RegradeError(
        f"Cannot tell which directory under {artifacts} is the workspace: no {prior.task_id!r} "
        + f"child, and {len(children)} candidates ({', '.join(p.name for p in children)}). "
        + "Pass --workspace explicitly."
    )


def _is_within(candidate: Path, root: Path) -> bool:
    """True when ``candidate`` resolves inside ``root``."""
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def verify_reference_unchanged(prior: EvaluationResult, task: TaskDefinition, task_file: Path | None) -> None:
    """Refuse to grade when the reference tree changed since the run.

    ``reference_comparison`` and reference-carrying judges score against
    ``task.reference.directory``. If it moved since the run, the re-grade would
    silently measure the agent's old work against a new answer key.

    ``task_file`` is what ``reference.directory`` resolves against, so it is
    required for any task that declares one — resolving without it raises, which
    is why it is threaded through rather than passed as ``None``.
    """
    if task.reference is None:
        return
    recorded = prior.environment_info.get("reference_digest")
    if not isinstance(recorded, str):
        # A run that predates the digest being persisted. Say so: silence here is
        # what made this whole guard dead code for its first release.
        logger.warning(
            "This run recorded no reference_digest, so the answer key cannot be verified. "
            + "Grading proceeds; a reference edited since the run would go undetected."
        )
        return
    from .evaluation import resolve_reference_dir

    try:
        resolved = resolve_reference_dir(task, task_file)
    except (FileNotFoundError, ValueError) as e:
        raise RegradeError(
            f"This run's task declares a reference directory that cannot be resolved now ({e}), "
            + "so its contents cannot be verified against the executed run."
        ) from e
    if resolved is None or not resolved.is_dir():
        raise RegradeError(
            f"The reference directory recorded for this run is gone ({resolved}). Grading now "
            + "would score against a missing answer key. Restore it, or re-run the task."
        )
    if _staged_digest(resolved) != recorded:
        raise RegradeError(
            f"The reference directory {resolved} changed since this run was executed "
            + "(digest mismatch). Grading now would score the agent's work against a "
            + "different answer key. Restore the reference, or re-run the task."
        )


def _staged_digest(source: Path) -> str:
    """Digest ``source`` the way the run recorded it — through a staged copy.

    The recorded ``reference_digest`` is taken over the per-run STAGED copy
    (``Orchestrator._stage_reference``), which ``stage_reference_dir`` filters
    through ``REFERENCE_COPY_IGNORE`` (``.git``) and strips of symlinks.
    Digesting the raw source instead compares two differently-filtered trees, so
    any reference that is a git checkout — the case the ignore list exists for —
    reports a permanent false mismatch and un-grades the row for good.

    Re-staging rather than re-implementing the filter keeps the two in step: a
    future entry in the ignore list applies here without a second edit.
    """
    import tempfile

    from coder_eval.path_utils import digest_tree

    from .evaluation import stage_reference_dir

    with tempfile.TemporaryDirectory(prefix="coder-eval-refdigest-") as tmp:
        staged = stage_reference_dir(source, Path(tmp) / "reference")
        return digest_tree(staged)


def back_up_pre_grade_record(run_dir: Path) -> None:
    """Keep the ungraded ``task.json`` beside the graded one, once.

    The write-back replaces the only on-disk evidence that this run was executed
    separately from grading. Copying it first keeps that auditable. Written once:
    a second grade must not overwrite the ORIGINAL execute record with an
    already-graded one.
    """
    source, backup = run_dir / TASK_JSON_FILENAME, run_dir / PRE_GRADE_JSON_FILENAME
    if backup.exists() or not source.is_file():
        return
    if source.is_symlink() or backup.is_symlink():
        # Untrusted run dir: writing through a symlink would let a shared
        # artifact clobber an arbitrary file the grading user can write.
        logger.warning("Not preserving the pre-grade record: %s or %s is a symlink.", source, backup)
        return
    try:
        backup.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError as e:
        # Never fail a grade over the audit copy.
        logger.warning("Could not preserve the pre-grade record at %s: %s", backup, e)


def restore_pre_grade_record(run_dir: Path) -> bool:
    """Put the ungraded ``task.json`` back after a grading crash.

    ``Orchestrator._finalize_result`` writes ``task.json`` into its run dir
    *before* returning, so by the time a caller sees ``FinalStatus.ERROR`` the
    ERROR row is already on disk whenever the grade wrote into the run being
    graded (always, on ``run --resume``). Both commands treat ERROR as complete,
    so the row would be permanently un-regradeable — and a caller that only fixes
    its in-memory result leaves ``run.json`` disagreeing with ``task.json``.

    Returns whether the restore happened; there is nothing to restore when the
    grade wrote elsewhere and the original was never replaced.
    """
    source, backup = run_dir / TASK_JSON_FILENAME, run_dir / PRE_GRADE_JSON_FILENAME
    if not backup.is_file() or backup.is_symlink() or source.is_symlink():
        return False
    try:
        text = backup.read_text(encoding="utf-8")
        if source.is_file() and source.read_text(encoding="utf-8") == text:
            return False  # never overwritten; nothing to undo
        write_text_atomic(source, text)
    except OSError as e:
        logger.warning("Could not restore the ungraded record at %s: %s", source, e)
        return False
    logger.info("Restored the ungraded record at %s after a grading failure.", source)
    return True


def grading_sandbox_config(task: TaskDefinition, *, allow_host_grading: bool = False) -> SandboxConfig:
    """The sandbox config a grading pass runs under.

    Grading never runs a container: the docker driver dispatches through
    DockerRunner, which needs an agent. So a ``driver: docker`` task can only be
    graded on the host — and that is a DIFFERENT machine from the one its
    criteria were written against.

    It is therefore refused rather than downgraded. A container task's criteria
    address container paths (``/verifier``, ``/logs/verifier``) and container
    toolchains; run on the host they score 0.0 for a trajectory ``run`` scored
    1.0, and the row is written back FAILURE. The same commands (``rm -rf
    /verifier``, ``mkdir -p /logs/verifier``) also execute unsandboxed on the
    grading machine. A silent rewrite additionally neutralized the ``docker``
    refusal in ``Sandbox.adopt``, which exists to catch exactly this.

    ``allow_host_grading`` is the operator's explicit acceptance of both. Rows
    graded that way are stamped ``graded_on_host`` in ``environment_info``
    (:func:`stamp_host_grading`) so they are never silently comparable with rows
    a container graded.

    Re-validated rather than ``model_copy(update=...)``: ``update`` skips both
    pydantic validation and pyright, so a typo would produce a SandboxConfig
    violating its own ``Literal`` and surface much later at an unrelated
    ``if driver == "docker"`` branch.
    """
    if task.sandbox.driver != "docker":
        return task.sandbox.model_copy(deep=True)
    if not allow_host_grading:
        raise RegradeError(
            f"Task {task.task_id!r} ran under `driver: docker`, and grading cannot start a container "
            + "(there is no agent to run in it). Grading on the host would execute this task's "
            + "criteria against a filesystem that lacks the container's paths and toolchain, "
            + "scoring a FAILURE for a run that passed — and would run its shell commands "
            + "unsandboxed here. Re-run with --allow-host-grading to accept that, or grade on a "
            + "machine that reproduces the container."
        )
    logger.warning(
        "Grading %r on the host: its `driver: docker` sandbox cannot be reproduced here, so "
        + "path- and toolchain-dependent criteria may score differently than they did in the run. "
        + "The row is stamped graded_on_host.",
        task.task_id,
    )
    return SandboxConfig.model_validate({**task.sandbox.model_dump(), "driver": "tempdir"})  # noqa: CE051


def stamp_host_grading(result: EvaluationResult, task: TaskDefinition) -> None:
    """Record that a docker task's verdict was produced on the host.

    Written onto the result, not just logged: a console warning does not travel
    with ``task.json`` into ``run.json``, the reports or the evalboard, and this
    row must never be compared with a container-graded one without that caveat
    attached.
    """
    if task.sandbox.driver == "docker":
        result.environment_info["graded_on_host"] = True


async def regrade_in_place(
    *,
    task: TaskDefinition,
    prior: EvaluationResult,
    workspace: Path,
    run_dir: Path,
    task_file: Path | None,
    source_yaml: str,
    variant_id: str,
    replicate_index: int = 0,
    allow_host_grading: bool = False,
) -> EvaluationResult:
    """Run ``task``'s criteria against an already-executed ``workspace``.

    The workspace is *adopted*, never copied: it is the run's own output, and the
    template-copy path filters out ``node_modules`` / ``dist`` / ``build`` /
    ``.venv``, which would make a criterion reading those fail as a copying
    artifact rather than as a verdict.

    ``prior`` supplies the trajectory and the run's execution facts (see
    ``Orchestrator._seed_from_prior_result``), so criteria that read the agent's
    tool calls score exactly as they would have during the run.
    """
    from coder_eval.orchestrator import Orchestrator

    # Inside the shared entry point, not at each caller: a guard a caller has to
    # remember is one a third caller will forget, and this one is the difference
    # between a verdict and a verdict against the wrong answer key.
    verify_reference_unchanged(prior, task, task_file)

    sandbox = Sandbox(
        grading_sandbox_config(task, allow_host_grading=allow_host_grading),
        task_id=task.task_id,
        task_dir=task_file.parent.resolve() if task_file is not None else None,
    )
    await asyncio.to_thread(sandbox.adopt, workspace)

    orchestrator = Orchestrator(
        task=task,
        run_dir=run_dir,
        # The workspace belongs to the run being graded; never move or delete it.
        preservation_mode=PreservationMode.NONE,
        task_file=task_file,
        sandbox=sandbox,
        variant_id=variant_id,
        source_yaml=source_yaml,
        replicate_index=replicate_index,
        prior_result=prior,
    )
    result = await orchestrator.run()
    stamp_host_grading(result, task)
    return result
