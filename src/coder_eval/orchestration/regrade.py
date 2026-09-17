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
import contextlib
import logging
import os
import shutil
import tempfile
from functools import cache
from pathlib import Path
from typing import Any, TypedDict

from coder_eval.models import (
    IN_CONTAINER_ENV,
    EvaluationResult,
    PreservationMode,
    ResolvedTask,
    SandboxConfig,
    TaskConfigRecord,
    TaskDefinition,
)
from coder_eval.path_utils import (
    DOCKER_LOG_FILENAME,
    GRADE_DOCKER_LOG_FILENAME,
    GRADE_LOG_FILENAME,
    PRE_GRADE_JSON_FILENAME,
    TASK_JSON_FILENAME,
    write_text_atomic,
)
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
    grade_in_place: bool = False,
    allow_host_grading: bool = False,
) -> tuple[TaskDefinition, str]:
    """Rebuild the executed task from the run's own recorded config.

    Rebuilding from ``task_config.resolved`` rather than re-reading the YAML is
    what makes the grade describe the run that happened: ``resolved`` is
    post-merge, so variant overrides, ``-D`` flags and dataset expansion are
    already baked in. Falls back to the source YAML only when ``resolved`` will not
    validate, and says so LOUDLY — a quiet fallback reintroduces that drift.

    ``allow_recorded_commands`` gates the shell half (see
    :func:`check_embedded_commands`); ``grade_in_place`` is the ONE lever selecting
    which capability families the gate discloses.

    Rationale: .claude/notes/orchestration.md § What the gate covers, and why each part is in scope
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
            record,
            run_dir,
            e,
            allow_recorded_commands=allow_recorded_commands,
            grade_in_place=grade_in_place,
            allow_host_grading=allow_host_grading,
        )
    check_embedded_commands(
        task,
        run_dir,
        allow_recorded_commands=allow_recorded_commands,
        # The recorded source path, so the prompt can name the task DIRECTORY the
        # dispatch copies in. The same untrusted value the image resolves from.
        task_file=Path(record.source_file) if record.source_file else None,
        **_gate_scope_for_grade(task, grade_in_place=grade_in_place, allow_host_grading=allow_host_grading),
    )
    return task, record.source_yaml


class _GateScope(TypedDict):
    include_setup_phase: bool
    include_container_dispatch: bool


def _gate_scope_for_grade(task: TaskDefinition, *, grade_in_place: bool, allow_host_grading: bool) -> _GateScope:
    """Which capability families the untrusted-config gate must disclose.

    Both answers follow from ``grade_in_place``, which is why this is one
    function rather than two arguments threaded past each other:

    * ``include_setup_phase`` is ``not grade_in_place``. ``pre_run`` and
      ``template_sources`` (a ``git clone``) exist only on the ``--copy`` path;
      the orchestrator skips ``pre_run`` in place and ``adopt`` never stages a
      template, so in place neither is a capability the run dir has.
      ``env_packages`` installs are NOT part of this flag -- ``adopt`` can now
      re-provision them too (a captured workspace missing ``.venv`` /
      ``node_modules``), so :func:`embedded_commands` discloses them
      unconditionally regardless of ``grade_in_place``.
    * ``include_container_dispatch`` needs ``grade_in_place`` too, since ``--copy``
      is refused by ``grading_sandbox_config`` before it could dispatch anything
      -- naming the image there would be a refusal for something that never runs.

    The pair is computed at ONE site so the two consumers (``task_from_prior``
    and its source-YAML fallback) cannot drift.
    """
    return {
        "include_setup_phase": not grade_in_place,
        "include_container_dispatch": grade_in_place
        and _should_grade_in_container(task, allow_host_grading=allow_host_grading),
    }


@cache
def _operator_baseline_post_run() -> frozenset[str]:
    """``post_run`` commands the GRADER's own default experiment gives every task.

    Read from the grading host's ``DEFAULT_EXPERIMENT_PATH``, never from the run
    record — the whole point is to compare what arrived against what this
    operator's own config does. A record cannot widen this set by claiming
    membership in it; an exact string match is the only way in, and a match means
    the command is one this host already runs on every task of its own.

    Fails CLOSED. Any problem loading or parsing the baseline yields an empty set,
    so every recorded command is scanned and the operator is asked. Cached because
    it is a YAML parse on a path that cannot change within a process.
    """
    from coder_eval.orchestration.experiment import DEFAULT_EXPERIMENT_PATH, load_experiment

    try:
        defaults = load_experiment(DEFAULT_EXPERIMENT_PATH).defaults
    # Broad by intent: a missing or invalid baseline must narrow the exemption, never block a grade.
    except Exception as e:
        logger.debug(
            "No baseline post_run exemption (%s: %s); every recorded command will be scanned.", type(e).__name__, e
        )
        return frozenset()
    if defaults is None or not defaults.post_run:
        return frozenset()
    return frozenset(c.command for c in defaults.post_run)


def embedded_commands(
    task: TaskDefinition,
    *,
    include_setup_phase: bool = True,
    include_container_dispatch: bool = False,
    task_file: Path | None = None,
) -> list[str]:
    """Every shell command a rebuilt task definition would run on this host.

    ``include_setup_phase`` covers only ``pre_run`` and a ``template_sources``
    repo's ``git clone`` -- the two families that exist solely on the ``--copy``
    path. ``env_packages`` installs, ``post_run`` and
    ``include_container_dispatch`` are disclosed regardless of it: all three are
    capabilities of the IN-PLACE path too, which is the DEFAULT for a run
    directory.

    ``isinstance`` narrowing, never ``getattr(c, "command", None)``: an untyped
    probe over a discriminated union is invisible to pyright, so a renamed field
    would silently degrade the only guard on this path to a no-op.

    Rationale: .claude/notes/orchestration.md § What the gate covers, and why each part is in scope
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
            # No command string of its own: it spawns a tool-using agent under
            # the grader's credentials, which is WIDER than one shell line.
            commands.append(f"<agent_judge: spawns a tool-using agent — {c.description}>")
        elif isinstance(c, LLMJudgeCriterion):
            # No shell, but it spends the grader's budget and ships the graded
            # artifacts to a provider the recorded config chose.
            commands.append(f"<llm_judge: sends artifacts to {c.model} on your credentials>")
        elif isinstance(c, UiPathEvalCriterion):
            # Every argument is shlex-quoted, so this is disclosure rather than
            # injection — but still a subprocess the record chose to start.
            commands.append(f"uv run uipath eval {c.agent_name} {c.eval_set}")
    # Unconditional: post_run runs on every path that grades (see the docstring).
    # Minus the operator's own universal baseline, which the record did not choose.
    baseline = _operator_baseline_post_run()
    commands += [c.command for c in task.post_run if c.command not in baseline]
    sandbox = task.sandbox
    if sandbox.python is not None and sandbox.python.env_packages:
        commands.append(f"uv pip install {' '.join(sandbox.python.env_packages)}")
    if sandbox.node is not None and sandbox.node.env_packages:
        commands.append(f"npm install {' '.join(sandbox.node.env_packages)}")
    if include_setup_phase:
        commands += [c.command for c in task.pre_run]
        for source in sandbox.template_sources or []:
            if isinstance(source, RepoSource):
                commands.append(f"git clone -- {source.url}")
    if include_container_dispatch:
        commands += _container_dispatch_commands(task, task_file)
    return commands


def _container_dispatch_commands(task: TaskDefinition, task_file: Path | None) -> list[str]:
    """The container dispatch, rendered as the ONE shell command it is.

    Every string returned is a command, because that is what the caller promises:
    :func:`check_embedded_commands` joins them with ``"; "`` and interpolates
    ``len(commands)`` into the consent prompt. The consent prompt is the one place
    this text has to be exact.

    It names every HOST PATH the dispatch exposes, not just the ones under
    ``sandbox.docker`` — the task directory, every auto-mounted plugin and
    template path, and a writable copy of ``~/.claude``.

    Rationale: .claude/notes/orchestration.md § Embedded commands
    """
    docker = task.sandbox.docker
    parts: list[str] = []
    if docker.dockerfile_path:
        # Runs every RUN step in the recorded Dockerfile on this host, and
        # expands recorded build args against the GRADER's environment, so a
        # `${ANTHROPIC_API_KEY}` arg is exfiltratable by a RUN step. `extra_args`
        # is spliced into the argv unfiltered.
        parts.append(f"docker build -f {docker.dockerfile_path}")
        parts += [f"--build-arg {key}={value}" for key, value in docker.build.args.items()]
        parts += [f"--secret {spec}" for spec in docker.build.secrets]
        parts += list(docker.build.extra_args)
        parts.append("&& docker run <the image just built>")
    else:
        parts.append(f"docker run {docker.image}")
    parts += [f"-v {mount}" for mount in docker.extra_mounts or []]
    parts += [f"-v {path}" for path in _dispatch_host_exposure(task, task_file)]
    parts += [f"--env {name}" for name in docker.env_passthrough_extra or []]
    parts.append("(with your credentials in its environment and a writable copy of ~/.claude)")
    return [" ".join(parts)]


def _dispatch_host_exposure(task: TaskDefinition, task_file: Path | None) -> list[str]:
    """Host paths the grading container receives that no ``docker`` field names.

    Mirrors ``DockerRunner._prepare_task_dir_mount`` and the ``_auto_mount``
    block of ``_build_argv``. Rendered as strings rather than resolved Paths:
    this is disclosure text, and an unresolvable entry is still worth naming.
    """
    from coder_eval.models import TemplateDirSource

    exposed: list[str] = []
    if task_file is not None:
        exposed.append(f"{task_file.parent} (the recorded task directory, copied in)")
    agent = task.agent
    for plugin in (agent.plugins if agent else None) or []:
        path = plugin.get("path") if isinstance(plugin, dict) else None
        if path:
            exposed.append(str(path))
    for source in task.sandbox.template_sources or []:
        if isinstance(source, TemplateDirSource):
            exposed.append(source.path)
    if agent is not None and agent.system_prompt_file:
        exposed.append(str(agent.system_prompt_file))
    return exposed


def check_embedded_commands(
    task: TaskDefinition,
    run_dir: Path,
    *,
    allow_recorded_commands: bool,
    include_setup_phase: bool = True,
    include_container_dispatch: bool = False,
    task_file: Path | None = None,
) -> None:
    """Refuse — or at minimum name — the shell a rebuilt config will run here.

    ``task_config.resolved`` travels inside a run directory, and a run directory is
    a SHAREABLE ARTIFACT — the detached-grading flow exists so one machine can
    execute and another can grade. Rebuilding from it means the RUN DIR decides
    what runs on the grader's host, with the grader's environment.

    A warning is not a control: it prints as the command is already being prepared.
    So a recorded config carrying shell is REFUSED unless the operator opted in.
    Passing the task file explicitly (``evaluate <task.yaml> <run_dir>``) bypasses
    this — that config came from the operator, not from the artifact.

    Rationale: .claude/notes/orchestration.md § Embedded commands
    """
    commands = embedded_commands(
        task,
        include_setup_phase=include_setup_phase,
        include_container_dispatch=include_container_dispatch,
        task_file=task_file,
    )
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
    grade_in_place: bool = False,
    allow_host_grading: bool = False,
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
        task,
        run_dir,
        allow_recorded_commands=allow_recorded_commands,
        task_file=Path(record.source_file),
        **_gate_scope_for_grade(task, grade_in_place=grade_in_place, allow_host_grading=allow_host_grading),
    )
    return task, source_yaml


def default_workspace(run_dir: Path, prior: EvaluationResult) -> Path:
    """Locate the workspace a finished run left behind.

    ``sandbox_path`` is authoritative when it still exists; otherwise the
    preserved artifacts tree, where preservation nests the workspace under the
    task id.

    RAISES rather than guessing when neither is conclusive — grading the WRONG
    directory makes every path-relative criterion fail as a locating artifact and
    reports that as an ordinary score.

    **Every** return goes through ``_contained``, checked against ``run_dir``.

    Rationale: .claude/notes/orchestration.md § Locating the workspace a finished run left behind
    """

    def _contained(candidate: Path, description: str) -> Path:
        # One chokepoint, one root. `run_dir` is the operator-supplied path; a
        # candidate is only ever derived from the untrusted record.
        if not _is_within(candidate, run_dir):
            raise RegradeError(
                f"{description} resolves outside the run directory ({run_dir}). "
                + "Pass --workspace explicitly to grade a directory outside the run."
            )
        return candidate

    if prior.sandbox_path:
        recorded = Path(prior.sandbox_path)
        if recorded.is_dir():
            # Untrusted input for a shared run dir, and criteria execute with cwd
            # there and may mutate it.
            return _contained(recorded, f"The recorded sandbox_path ({recorded})")

    artifacts = run_dir / ARTIFACTS_DIRNAME
    if not artifacts.is_dir():
        raise RegradeError(
            f"No workspace to grade: {artifacts} does not exist and the recorded sandbox_path "
            + f"({prior.sandbox_path or 'unset'}) is gone. The run was probably made with "
            + "--preservation-mode NONE."
        )
    # Checked before anything is derived from it: a symlinked `artifacts/` makes
    # every check rooted at `artifacts` tautological.
    _contained(artifacts, f"The artifacts directory ({artifacts})")

    # The EXACT path, not a heuristic: `task_id` may contain "/" (dataset rows are
    # "<suite>/<row>"), so "the single child of artifacts/" resolves one level too
    # high for every row task. It is also an unvalidated string out of the run's
    # own task.json.
    by_task_id = artifacts / prior.task_id
    if by_task_id.is_dir():
        return _contained(by_task_id, f"The recorded task_id ({prior.task_id!r})")

    children = [p for p in sorted(artifacts.iterdir()) if p.is_dir()]
    if not children:
        # A flat artifacts dir (no subdirectory) means the workspace IS artifacts/.
        return artifacts
    if len(children) == 1:
        # A symlinked child escapes just as well as a symlinked artifacts/.
        return _contained(children[0], f"The only directory under {artifacts} ({children[0].name})")
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

    This is the HOST-grading config, and reaching it with ``driver: docker`` means
    the container route was declined: what is left is the ``--copy`` path, which
    cannot adopt a container workspace, and the two-argument
    ``evaluate <task.yaml> <dir>`` form.

    Host-grading a container task is REFUSED, not downgraded.
    ``allow_host_grading`` is the operator's explicit acceptance, and such rows are
    stamped ``graded_on_host``.

    Re-validated rather than ``model_copy(update=...)``: ``update`` skips both
    pydantic validation and pyright, so a typo would produce a SandboxConfig
    violating its own ``Literal`` and surface much later.

    Rationale: .claude/notes/isolation.md § Grading a docker row inside a container
    """
    if task.sandbox.driver != "docker":
        return task.sandbox.model_copy(deep=True)
    if not allow_host_grading:
        raise RegradeError(
            f"Task {task.task_id!r} ran under `driver: docker`, so it must be graded in a container "
            + "of its own image — but this grading path cannot dispatch one. Grading on the host "
            + "would execute this task's criteria against a filesystem that lacks the container's "
            + "paths and toolchain, scoring a FAILURE for a run that passed, and would run its "
            + "shell commands unsandboxed here.\n"
            + "Grade a run directory in place (the default) to get a container, or pass "
            + "--allow-host-grading to accept host grading anyway.\n"
            + "(The two-argument `evaluate <task.yaml> <dir>` form has no container route at all, "
            + "so --allow-host-grading is the only way forward there.)"
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


def _should_grade_in_container(task: TaskDefinition, *, allow_host_grading: bool) -> bool:
    """Whether this grade belongs in a container of the task's own image.

    Three conditions, and each rules out a different wrong answer:

    * ``driver: docker`` — a tempdir task has no container to grade in.
    * NOT already inside one. Gated on ``CODER_EVAL_IN_CONTAINER``, never on the
      driver, for the same reason the reference-permission window is: the host
      stages a container's task with `driver: tempdir`, so a driver-based test
      would be reading a value that has already been resolved. Without this, a
      grading container would try to dispatch a grading container.
    * ``--allow-host-grading`` not passed. That flag is the operator saying
      "grade it here anyway" — the escape hatch for a machine with no docker, or
      for criteria known to be host-portable — and it must keep winning, since
      the row it produces is stamped ``graded_on_host`` and is therefore honest
      about what it is.
    """
    return task.sandbox.driver == "docker" and not allow_host_grading and os.environ.get(IN_CONTAINER_ENV) != "1"


def _fold_back_container_logs(container_run_dir: Path, run_dir: Path) -> None:
    """Rescue the grading container's logs from the scratch dir before it dies.

    Called on BOTH the success and the failure path, and the FAILURE path is what
    makes it necessary: the scratch dir is deleted the moment the dispatch's
    ``with`` exits, and everything explaining a failure lives in it.

    ``grade.log`` is the grading pass's OWN log, holding the per-criterion detail
    that is the only durable record of WHY a criterion scored what it did, and a
    documented part of the run-directory contract. A ``task.json.unhonored`` record
    the contract echo refused is rescued the same way, beside the row it was grading.

    Best-effort throughout: a side-car log is not the verdict, and this runs where
    an exception is already in flight.

    Rationale: .claude/notes/isolation.md § Grading a docker row inside a container
    """
    unhonored = f"{TASK_JSON_FILENAME}.unhonored"
    rescued = (
        (DOCKER_LOG_FILENAME, GRADE_DOCKER_LOG_FILENAME),
        (GRADE_LOG_FILENAME, GRADE_LOG_FILENAME),
        (unhonored, unhonored),
    )
    for name, dest_name in rescued:
        # Renamed for the PHASE: on the resume path that name is already taken by
        # the executed container's log. `grade.log` does not collide.
        source = container_run_dir / name
        if not source.is_file():
            continue
        # The destination may not exist yet on the FAILURE path -- the verdict
        # fold-back, which creates it, never ran.
        with contextlib.suppress(OSError):
            run_dir.mkdir(parents=True, exist_ok=True)
        dest = run_dir / dest_name
        if dest.is_symlink():
            # `shutil.copy2` opens the destination for writing and FOLLOWS a
            # symlink there — an arbitrary-file-overwrite primitive in a run
            # directory the grader did not create. The `suppress(OSError)` below
            # would have made the redirect leave no trace.
            logger.warning("Refusing to write %s: it is a symlink.", dest)
            continue
        with contextlib.suppress(OSError):
            shutil.copy2(source, dest)


def _fold_back_container_grade(container_run_dir: Path, run_dir: Path, task_id: str) -> None:
    """Copy the grading container's record into the row the caller asked about.

    The container writes into a scratch directory it alone owns (see
    :func:`_grade_in_container`), so the graded ``task.json`` has to be moved to
    where the caller expects it; its logs come along via
    :func:`_fold_back_container_logs`.

    Mandatory on the record: a grade that cannot write its verdict is a failure.
    But it must fail as a ``RegradeError``, not as a raw ``OSError``. This call
    sits outside the dispatch ``try``, so an unwrapped ``OSError`` reached the two
    callers differently and both outcomes were wrong: ``evaluate`` guards only
    ``RegradeError``, so it escaped into Typer as a stack trace *after* a grade
    that had already succeeded; ``run --resume`` catches ``OSError`` too, so it
    folded the row back with its ORIGINAL ungraded result and an error reading
    "Grading failed during --resume" -- reporting a computed, correct verdict as
    a grading failure and discarding it.
    """
    graded = container_run_dir / TASK_JSON_FILENAME
    if not graded.is_file():
        # `_parse_result_or_raise` already raised if the runner returned no
        # result, so guard rather than raise a confusing FileNotFoundError.
        return
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        write_text_atomic(run_dir / TASK_JSON_FILENAME, graded.read_text(encoding="utf-8"))
    except OSError as e:
        raise RegradeError(
            f"Graded {task_id!r} in a container, but could not write the verdict to "
            + f"{run_dir / TASK_JSON_FILENAME}: {e}. The grade itself succeeded; re-run once the "
            + "destination is writable."
        ) from e
    _fold_back_container_logs(container_run_dir, run_dir)


def _stamp_container_grading(result: EvaluationResult, task: TaskDefinition) -> None:
    """Record the container grade's known equivalence gaps ON the row.

    The sibling of :func:`stamp_host_grading`, and written for the reason that
    function's docstring gives: a console warning does not travel with
    ``task.json`` into ``run.json``, the reports or the evalboard, so a row it
    describes cannot be filtered out of a comparison by anything downstream.

    ``graded_without_pre_run`` counts ``pre_run`` commands that ran in the
    container which executed the agent and were not re-run here;
    ``graded_with_rebuilt_image`` marks that the pass re-ran ``docker build`` under
    the run's deterministic tag.

    Rationale: .claude/notes/isolation.md § Two known equivalence gaps, stamped rather than refused
    """
    if task.pre_run:
        result.environment_info["graded_without_pre_run"] = len(task.pre_run)
    if task.sandbox.docker.dockerfile_path:
        result.environment_info["graded_with_rebuilt_image"] = str(task.sandbox.docker.dockerfile_path)


def _emit_task_telemetry(result: EvaluationResult, *, variant_id: str) -> None:
    """Emit the ``Task.End`` event the grading container could not.

    Every container this repo starts is launched with ``TELEMETRY_ENABLED=false``
    (``DockerRunner._build_argv``), whose comment states the invariant verbatim:
    "container silent, host emits once". The RUN path supplies the host half in
    ``orchestration/batch.py`` right after parsing the container's result; the
    grading path inherited the silent half and had no counterpart, so a verdict
    published by ``evaluate <run_dir>`` or ``run --resume`` over a
    ``driver: docker`` row reached no usage telemetry at all -- while the
    byte-identical operation on a ``tempdir`` row, or the same row with
    ``--allow-host-grading``, did. A driver-dependent hole in the metric the
    split exists to keep at parity with ``run``.

    Non-fatal like every other emission site (CE019): telemetry must never be
    the reason a computed verdict is lost.
    """
    from coder_eval.orchestrator import build_task_event
    from coder_eval.telemetry import track_event

    name, props = build_task_event(result, driver="docker", variant_id=variant_id)
    track_event(name, props)


async def _grade_in_container(
    *,
    task: TaskDefinition,
    prior: EvaluationResult,
    workspace: Path,
    run_dir: Path,
    task_file: Path | None,
    source_yaml: str,
    variant_id: str,
    replicate_index: int,
) -> EvaluationResult:
    """Grade ``workspace`` inside a container built from ``task``'s own image.

    Two separate mounts, and keeping them separate is the point: ``run_dir`` (this
    GRADING pass's fresh directory) at the standard output location, and
    ``workspace`` (the ORIGINAL run's output) at ``CONTAINER_GRADE_WORKSPACE``.
    The grade writes its ``task.json`` into the former, which the caller folds back
    into the row; the latter is adopted, and criteria may still mutate it -- and
    ``Sandbox.adopt`` itself may write a re-provisioned ``.venv``/``node_modules``
    into it when the workspace is missing one -- but the write is confined to
    exactly that: this is not a template copy, and no unrelated file is replaced.

    ``task_file`` is required and must EXIST here — testing only for ``None`` was
    not enough, and failed on exactly the rows this guard was written for.

    Rationale: .claude/notes/orchestration.md § Why the container dispatch requires an EXISTING task file
    """
    from coder_eval.isolation.docker_runner import DockerRunError, DockerRunner

    if task_file is None or not task_file.is_file():
        recorded = "no task file" if task_file is None else f"a task file that is not on this host ({task_file})"
        raise RegradeError(
            f"Task {task.task_id!r} ran under `driver: docker`, so grading it needs a container built "
            + f"from its own image — but the run records {recorded} to resolve that image from. "
            + "A container run records the path it saw INSIDE the container, which does not exist "
            + "here. Pass the task file explicitly (`coder-eval evaluate <task.yaml> <run_dir>`), or "
            + "--allow-host-grading to grade here instead."
        )

    logger.info(
        "Grading %r in a container of its own image: its criteria address container paths and "
        + "toolchains, so the host cannot reproduce them.",
        task.task_id,
    )
    if task.pre_run:
        # KNOWN EQUIVALENCE GAP, made loud because it cannot be closed here: this
        # is a SECOND, fresh container, so everything the first one's `pre_run` did
        # OUTSIDE the workspace is gone. Re-running pre_run here would trade this
        # bug for the deliverable-clobbering one.
        # Rationale: .claude/notes/isolation.md § Two known equivalence gaps, stamped rather than refused
        logger.warning(
            "Task %r declares %d pre_run command(s). They ran in the container that executed the "
            + "agent and are NOT re-run here: this is a second container, and only the workspace "
            + "crosses. A criterion that depends on state pre_run put OUTSIDE the workspace "
            + "(a symlink in /root, an installed package, a started service) will score as a "
            + "failure. If that is this task, grade it with a single `coder-eval run` instead.",
            task.task_id,
            len(task.pre_run),
        )
    if task.sandbox.docker.dockerfile_path:
        # The SECOND known gap: `_build_image` re-runs `docker build` under the
        # deterministic tag, so the grading image REPLACES the run's under the same
        # name. Nothing pins image identity on either side yet, which is exactly
        # why it is said at dispatch.
        logger.warning(
            "Task %r builds its image from %s, so this grading pass re-runs `docker build`. If the "
            + "Dockerfile, its build context or its base image changed since the run, the criteria "
            + "read a different filesystem than the agent did and the score may differ for identical "
            + "agent output. Nothing records the image identity, so this cannot be detected "
            + "afterwards -- grade with a single `coder-eval run` if the image may have moved.",
            task.task_id,
            task.sandbox.docker.dockerfile_path,
        )
    # A SCRATCH output dir, never the caller's: the two callers disagree about what
    # `run_dir` is, and DockerRunner's result handling assumes an output dir it
    # alone populates.
    # Rationale: .claude/notes/isolation.md § Why the grading container gets a private scratch directory
    with tempfile.TemporaryDirectory(prefix="coder-eval-grade-") as scratch:
        container_run_dir = Path(scratch)
        rt = ResolvedTask(
            task=task,
            task_file=task_file,
            run_dir=container_run_dir,
            variant_id=variant_id,
            source_yaml=source_yaml,
            replicate_index=replicate_index,
        )
        try:
            result = await DockerRunner(
                rt,
                # The grading pass owns its scratch dir and nothing else: the
                # workspace is a bind mount of the ORIGINAL run's output.
                preservation_mode=PreservationMode.NONE,
                prior_result=prior,
                grade_workspace=workspace,
            ).run()
        except (DockerRunError, OSError) as e:
            # `orchestration/` must not leak an isolation-layer exception to the
            # CLI, and the actionable next step is the host-grading escape hatch.
            # OSError joins it because the staging copies raise it unwrapped.
            raise RegradeError(
                f"Grading {task.task_id!r} in a container failed: {e}. The container's own output was "
                + f"kept at {run_dir / GRADE_DOCKER_LOG_FILENAME}. If the image itself was refused (its "
                + "version or its contract echo), rebuild or pull a matching image. Otherwise, re-run with "
                + "--allow-host-grading to grade on this machine instead (path- and toolchain-dependent "
                + "criteria may then score differently, and the row is stamped graded_on_host)."
            ) from e
        finally:
            # ALWAYS, not only on success: the scratch dir dies with this `with`,
            # and everything that explains a failure lives in it.
            _fold_back_container_logs(container_run_dir, run_dir)
        # Fold the grade back into the row the caller asked about.
        # `back_up_pre_grade_record` already preserved task.execute.json.
        _fold_back_container_grade(container_run_dir, run_dir, task.task_id)
    # Outside the `with`: the scratch dir has served its purpose, and both of
    # these act on the returned result, which the caller writes back last.
    _stamp_container_grading(result, task)
    _emit_task_telemetry(result, variant_id=variant_id)
    return result


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
    recorded_task: TaskDefinition | None = None,
    recorded_task_file: Path | None = None,
    container_contract: dict[str, Any] | None = None,
) -> EvaluationResult:
    """Run ``task``'s criteria against an already-executed ``workspace``.

    The workspace is ADOPTED, never copied: it is the run's own output, and the
    template-copy path filters out ``node_modules`` / ``dist`` / ``build`` /
    ``.venv``, which would make a criterion reading those fail as a copying
    artifact rather than as a verdict.

    ``prior`` supplies the trajectory and execution facts, so criteria that read
    the agent's tool calls score as they would have during the run.

    ``recorded_task`` / ``recorded_task_file`` are two halves of one seam — what
    the row RECORDS, as distinct from what this process runs. Both matter only in
    the container, whose task the host stages with ``driver: tempdir``.

    ``container_contract`` is the echo the in-container caller forwards to the
    Orchestrator; the host never passes it.

    Rationale: .claude/notes/orchestration.md § Recording the task as authored
    """
    from coder_eval.orchestrator import Orchestrator

    # Every path through this function grades, so an empty `success_criteria` is
    # never legal here the way it is for `execute`. Checked at the single choke
    # point every re-grade entry point shares — a criteria-free task would
    # otherwise finalize SUCCESS at `weighted_score: 0.0`, since
    # `all_criteria_passed([])` is vacuously True.
    if not task.success_criteria:
        raise RegradeError(
            f"task {task.task_id!r} has no `success_criteria` and cannot be graded (it would silently "
            + "score SUCCESS at weighted_score 0.0). Add at least one criterion before re-grading it."
        )

    # Graded INSIDE a container of the same image, dispatched before anything else
    # here — the reference check included — so the container performs every step
    # against container paths.
    # Rationale: .claude/notes/isolation.md § Grading a docker row inside a container
    if _should_grade_in_container(task, allow_host_grading=allow_host_grading):
        # NOT forwarded, deliberately: the container re-derives it from the
        # staged task.yaml, which IS this `task`. Accepting a DIFFERENT one and
        # dropping it would leave no evidence, so say so instead.
        if recorded_task is not None and recorded_task != task:
            raise RegradeError(
                "recorded_task cannot be honored when grading in a container: the container rebuilds "
                + "the recorded task from the staged task.yaml. Pass the same task, or grade with "
                + "--allow-host-grading."
            )
        if recorded_task_file is not None and recorded_task_file != task_file:
            # Same rule: the container receives this over `context.json`, filled
            # from `task_file` here, so a different one could not be honored.
            raise RegradeError(
                "recorded_task_file cannot be honored when grading in a container: the container "
                + "records the host task file the dispatch forwards to it, which is `task_file`. "
                + "Pass the same path, or grade with --allow-host-grading."
            )
        return await _grade_in_container(
            task=task,
            prior=prior,
            workspace=workspace,
            run_dir=run_dir,
            task_file=task_file,
            source_yaml=source_yaml,
            variant_id=variant_id,
            replicate_index=replicate_index,
        )

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
        recorded_task=recorded_task,
        recorded_task_file=recorded_task_file,
        container_contract=container_contract,
    )
    result = await orchestrator.run()
    stamp_host_grading(result, task)
    return result
