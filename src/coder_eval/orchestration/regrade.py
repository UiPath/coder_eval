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
from typing import TypedDict

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
    what makes the grade describe the run that happened: ``resolved`` is the
    post-merge definition, so variant overrides, ``-D`` flags and dataset row
    expansion are all already baked in. Re-loading the source YAML would silently
    grade a DIFFERENT task whenever any of those were used.

    Falls back to the source YAML only when ``resolved`` will not validate (a
    schema change since the run), and says so loudly — a quiet fallback would
    reintroduce exactly the drift above.

    ``allow_recorded_commands`` gates the shell half. See
    :func:`check_embedded_commands`.

    ``grade_in_place`` is the ONE lever that selects which capability families
    the gate discloses, and it is deliberately one parameter rather than two.
    It shipped beside an ``include_setup_phase`` that every caller passed as its
    exact complement -- two names for one fact, with nothing rejecting the
    incoherent pairings. That is not cosmetic here: the flag gates a SECURITY
    disclosure, so a future caller that set one and forgot the other would drop
    the container-dispatch half of the untrusted-config gate with nothing
    failing. Both derived values are computed once, in
    :func:`_gate_scope_for_grade`.

    ``allow_host_grading`` participates only through that derivation: with the
    flag set, no container is dispatched, so naming one in the consent prompt
    would ask the operator to approve something that never runs.
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
        # The recorded source path, so the prompt can name the task DIRECTORY
        # the dispatch copies in. It is the same untrusted value
        # `_grade_in_container` resolves the image from.
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

    * ``include_setup_phase`` is ``not grade_in_place``. ``pre_run`` and the
      sandbox's own provisioning exist only on the ``--copy`` path; ``adopt``
      runs no installer and the orchestrator skips ``pre_run``, so in place they
      are not a capability the run dir has.
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

    ``include_setup_phase`` covers the two capability families that exist only on
    the ``--copy`` path: ``pre_run``, and the sandbox's own provisioning. Both are
    SKIPPED when grading in place (``Sandbox.adopt`` runs no installer, and
    re-running ``pre_run`` would overwrite the agent's deliverables before the
    criteria read them), so on that path they are not a capability the run dir
    has.

    ``post_run`` is deliberately NOT behind that flag, and this is the one place
    the distinction bites. It used to be, back when the hooks were skipped as a
    pair — but ``post_run`` belongs to the GRADING phase (``execute`` defers it),
    so it now runs on EVERY grading path, in place included. Leaving it inside
    ``include_setup_phase`` would have made the in-place path — the DEFAULT for a
    run directory — execute recorded shell with no consent prompt at all.

    It is filtered against ``_operator_baseline_post_run()`` for a reason worth
    stating precisely: this gate asks the operator to approve shell **the record
    chose**, and a single ``coder-eval run`` — the behaviour the split must
    reproduce — runs ``post_run`` with no prompt at all, because the config came
    from the operator. The grader's own default experiment appends the same
    ``post_run`` to every task it ever runs, so finding one of those commands in
    a record reveals no choice the record made and grants no capability the
    operator's own config does not already exercise on every run. Prompting on it
    would fire on 100% of run directories, and a refusal that always fires is
    read as a formality and waved through — which is how the gate would stop
    protecting the authored commands that DO represent a choice.

    Sandbox provisioning is the half this gate originally missed, and it was the
    worst one. ``grading_sandbox_config`` carries the recorded ``sandbox`` block
    through untouched, and the ``--copy`` branch then calls ``Sandbox.setup``,
    which reaches ``uv pip install <recorded packages>``, ``npm install <recorded
    packages>`` and ``git clone <recorded url>``. A package name is arbitrary
    code at install time. Because the scan walked only ``success_criteria``, a
    shared run directory whose criteria were all ``file_exists`` sailed through
    the gate and still ran installers of the attacker's choosing.

    ``include_container_dispatch`` is the same omission again, one layer up, and
    it was reintroduced by the very change that made a docker row gradable. When
    a ``driver: docker`` row is graded, the grade is DISPATCHED INTO A CONTAINER
    built from the recorded ``sandbox.docker`` block -- so the record chooses the
    image that runs on this host, with the default credential allowlist
    (``ANTHROPIC_API_KEY``, ``UIPATH_ACCESS_TOKEN``, ``AWS_BEARER_TOKEN_BEDROCK``
    ...) forwarded into it, a writable copy of ``~/.claude``, and a pinned
    ``--entrypoint`` the image itself supplies. That is arbitrary code execution
    from a shareable artifact, and it is a strictly WIDER capability than the
    ``run_command`` strings this gate already refuses. It reached the host
    unprompted because the scan walked only ``success_criteria`` and ``post_run``
    -- the identical blind spot described in the paragraph above, which is the
    argument for naming it here rather than trusting the next reader to notice.

    Like ``post_run``, it is a capability of the IN-PLACE path (the default for a
    run directory), so it cannot hide behind ``include_setup_phase``.

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
    # Unconditional: post_run runs on every path that grades (see the docstring).
    # Minus the operator's own universal baseline, which the record did not choose.
    baseline = _operator_baseline_post_run()
    commands += [c.command for c in task.post_run if c.command not in baseline]
    if include_setup_phase:
        commands += [c.command for c in task.pre_run]
        sandbox = task.sandbox
        if sandbox.python is not None and sandbox.python.env_packages:
            commands.append(f"uv pip install {' '.join(sandbox.python.env_packages)}")
        if sandbox.node is not None and sandbox.node.env_packages:
            commands.append(f"npm install {' '.join(sandbox.node.env_packages)}")
        for source in sandbox.template_sources or []:
            if isinstance(source, RepoSource):
                commands.append(f"git clone -- {source.url}")
    if include_container_dispatch:
        commands += _container_dispatch_commands(task, task_file)
    return commands


def _container_dispatch_commands(task: TaskDefinition, task_file: Path | None) -> list[str]:
    """The container dispatch, rendered as the ONE shell command it is.

    Every string this returns is a command, because that is what the caller
    promises: :func:`check_embedded_commands` joins the list with ``"; "`` and
    interpolates ``len(commands)`` into the consent prompt. The first version
    appended argv FRAGMENTS as separate entries, so a `dockerfile_path` task with
    two build args and one mount asked the operator to approve "4 shell
    command(s)" reading ``docker build -f Dockerfile;   --build-arg FOO=bar;
    --build-arg BAZ=qux;   -v /a:/b`` -- one docker invocation described as four
    commands, three of which are not commands. The consent prompt is the one
    place this text has to be exact.

    It also names every HOST PATH the dispatch exposes, not just the ones under
    ``sandbox.docker``. Three families reach the record-named image without the
    record ever mentioning them in a ``docker`` block:

    * the TASK DIRECTORY, copied wholesale from the recorded ``source_file``'s
      parent (``DockerRunner._prepare_task_dir_mount``) -- a record whose
      ``source_file`` is ``~/.ssh/config`` copies all of ``~/.ssh`` in;
    * every ``agent.plugins[].path``, every ``TemplateDirSource.path`` and
      ``agent.system_prompt_file``, auto-mounted read-only at their host paths by
      ``_build_argv``. ``_sensitive_source_paths`` only *warns* about a fixed
      list of these, and this module's own gate docstring states the governing
      principle: a warning is not a control, because it prints as the command is
      already being prepared;
    * a WRITABLE copy of ``~/.claude``, ``.credentials.json`` included.

    Networking defaults to ``--network bridge``, so anything the container can
    read it can also send. Disclosing only ``sandbox.docker.*`` would ask the
    operator to consent to a strict subset of what actually happens.
    """
    docker = task.sandbox.docker
    parts: list[str] = []
    if docker.dockerfile_path:
        # `docker build` runs every RUN step in the recorded Dockerfile on this
        # host, and expands recorded build args against the GRADER's environment,
        # so a `${ANTHROPIC_API_KEY}` arg is exfiltratable by a RUN step.
        # `extra_args` is spliced into the argv unfiltered.
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

    ``sandbox_path`` is authoritative when it still exists — it is where the run
    actually worked. Otherwise fall back to the preserved artifacts tree, where
    preservation nests the workspace under the task id.

    Raises rather than guessing when neither is conclusive. Guessing is worse
    than failing here: grading the WRONG directory makes every path-relative
    criterion fail as a locating artifact rather than as a verdict, and it
    reports that as an ordinary score.

    **Every** return goes through ``_contained``, checked against ``run_dir``.
    The containment check originally covered one branch of four and rooted the
    ``task_id`` case at ``artifacts/`` rather than at the run directory, which
    made it vacuous the moment ``artifacts`` was ITSELF a symlink — and
    ``artifacts/`` is attacker-supplied for a shared run dir just like
    ``sandbox_path`` and ``task_id``. The escaped tree then became the grading
    root via ``Sandbox.adopt``, `run_command` criteria ran with it as cwd, and
    the resulting verdict — criterion detail text included — was written back
    into the run's own ``task.json``.
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
            # An absolute path out of the run's own task.json, which is
            # untrusted input for a shared run dir. Criteria execute with cwd
            # there and may mutate it, so an out-of-tree location has to be the
            # operator's explicit choice.
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

    # The exact path, not a heuristic. `task_id` may contain "/" (dataset rows
    # are "<suite>/<row>"), so "the single child of artifacts/" resolves one
    # level too high for every row task.
    #
    # `task_id` is an unvalidated string out of the run's own task.json, so
    # `"../../../../home/victim"` joins to a real directory that `is_dir()`
    # happily confirms.
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
    the container route was declined. A docker row is normally graded IN a
    container of its own image (:func:`_should_grade_in_container`), which is
    dispatched before this function is called; what is left here is the ``--copy``
    path, which cannot adopt a container workspace, and the two-argument
    ``evaluate <task.yaml> <dir>`` form.

    (This docstring once opened "grading never runs a container: the docker
    driver dispatches through DockerRunner, which needs an agent". That premise
    was simply wrong — a grading pass needs no agent — and it is the reason the
    refusal below survived for a release after it stopped being the only answer.)

    Grading a container task on the host is refused rather than downgraded. A container task's criteria
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
      driver, for the same reason the reference-permission window is: the
      in-container entry point rewrites `docker` -> `tempdir` before building its
      Orchestrator, so a driver-based test would be reading a value that has
      already been changed. Without this, a grading container would try to
      dispatch a grading container.
    * ``--allow-host-grading`` not passed. That flag is the operator saying
      "grade it here anyway" — the escape hatch for a machine with no docker, or
      for criteria known to be host-portable — and it must keep winning, since
      the row it produces is stamped ``graded_on_host`` and is therefore honest
      about what it is.
    """
    return task.sandbox.driver == "docker" and not allow_host_grading and os.environ.get(IN_CONTAINER_ENV) != "1"


def _fold_back_container_logs(container_run_dir: Path, run_dir: Path) -> None:
    """Rescue the grading container's logs from the scratch dir before it dies.

    Called on BOTH the success and the failure path, and the failure path is the
    one that makes it necessary. ``_grade_in_container`` runs the whole dispatch
    inside a ``TemporaryDirectory``, and every ``DockerRunner`` diagnostic is
    written into it: ``docker.log`` (the container's merged stdout+stderr, where
    the in-container FATAL guards land, since ``_grade_in_container`` passes no
    ``stream_callback`` and nothing is echoed), the captured build log a failed
    ``docker build`` persists there, and the synthetic ``BUILD_FAILED`` /
    ``ERROR`` records. Folding out only on success deleted precisely the evidence
    -- and DockerRunError's own text says ``See {log_path} for container
    output``, naming a path that no longer existed by the time it was printed.

    ``grade.log`` is the grading pass's OWN log, written by the in-container
    orchestrator (``task_log_path(run_dir, regrade=True)``) and holding the
    per-criterion detail -- ``run_command`` stdout/stderr, judge prompts and
    verdicts -- which is the only durable record of WHY a criterion scored what
    it did. It is a documented part of the run-directory contract, so a
    ``driver: docker`` row must not be the one shape that silently lacks it.

    Best-effort throughout: a side-car log is not the verdict, and this runs
    where an exception is already in flight.
    """
    for name, dest_name in ((DOCKER_LOG_FILENAME, GRADE_DOCKER_LOG_FILENAME), (GRADE_LOG_FILENAME, GRADE_LOG_FILENAME)):
        # `docker.log` is renamed for the PHASE: on the resume path that name is
        # already taken by the executed container's log, and overwriting it would
        # repeat the task.log/grade.log truncation bug one layer down.
        # `grade.log` does not collide -- a detached grade is the only thing that
        # ever writes it into that directory -- so it keeps its name.
        source = container_run_dir / name
        if not source.is_file():
            continue
        # The destination may not exist yet on the FAILURE path -- the verdict
        # fold-back, which creates it, never ran.
        with contextlib.suppress(OSError):
            run_dir.mkdir(parents=True, exist_ok=True)
        dest = run_dir / dest_name
        if dest.is_symlink():
            # `shutil.copy2` opens the destination for writing, which FOLLOWS a
            # symlink there -- an arbitrary-file-overwrite primitive in a run
            # directory the grader did not create (`run --resume` passes the
            # executed row's own directory; `--run-dir` can point anywhere). The
            # sibling verdict write goes through `write_text_atomic` precisely
            # for this, and the `suppress(OSError)` below would have made the
            # redirect leave no trace at all.
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
        # `_parse_result_or_raise` already raised in this case; if we are here
        # the runner returned a result, so the file exists. Guard anyway rather
        # than raise a confusing FileNotFoundError from the copy.
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
    function's own docstring gives: a console warning does not travel with
    ``task.json`` into ``run.json``, the reports or the evalboard, so a row it
    describes cannot be filtered out of a comparison by anything downstream.

    ``graded_without_pre_run`` is the count of ``pre_run`` commands that ran in
    the container which executed the agent and were NOT re-run here. It is not a
    hypothetical: three of the ten in-tree ``driver: docker`` tasks seed state
    outside the workspace in ``pre_run`` (``3d-scan-calc`` symlinks
    ``/root/mass_report.json``, and its verifier's first assertion is that the
    path exists), so such a row scores 0.000 for a trajectory ``coder-eval run``
    scores 1.000. Refusing outright was considered and declined: it would make
    ``run --resume`` fail on rows it grades correctly today whenever ``pre_run``
    happens to touch only the workspace, which is the common case. A durable,
    machine-readable marker lets a consumer decide, which a refusal does not.

    ``graded_with_rebuilt_image`` marks the other gap: the grading pass re-ran
    ``docker build`` under the run's deterministic tag, so the image is only as
    stable as the Dockerfile and its base were between the two phases.
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

    The container gets two separate mounts, and keeping them separate is the
    point: ``run_dir`` (this GRADING pass's fresh directory) at the standard
    output location, and ``workspace`` (the ORIGINAL run's output) at
    ``CONTAINER_GRADE_WORKSPACE``. The grade writes its ``task.json`` into the
    former, which the caller then folds back into the row — preserving
    ``task.execute.json`` exactly as on the host path — while the latter is
    adopted and never written over.

    ``task_file`` is required, and must EXIST here. The image is built or named
    by the task's own sandbox config, and DockerRunner resolves the Dockerfile
    and reference directory relative to the task file; without one there is
    nothing to build from.

    Testing only for ``None`` was not enough, and failed on exactly the rows this
    guard was written for. A ``driver: docker`` run records its ``source_file``
    from the IN-CONTAINER orchestrator, so the value is
    ``/work/task_dir/task.yaml`` — a real, non-``None`` ``Path`` that does not
    exist on the grading host. The guard was skipped, and the failure then went
    QUIET where it matters: ``_prepare_task_dir_mount`` does ``if not
    source.is_dir(): return``, so the grading container got no ``TASK_DIR`` mount
    at all and any ``$TASK_DIR`` criterion silently resolved against a different
    tree than during the run — a wrong verdict with no error anywhere.

    Requiring the file to exist also closes a second hole: on the detached path
    ``task_file`` comes straight from the untrusted record, and its PARENT is
    what ``_prepare_task_dir_mount`` copies into the container. A recorded
    ``source_file`` of ``~/.ssh/config`` would copy the whole of ``~/.ssh``.
    Existence alone does not make the path trusted — that is what the
    ``--allow-recorded-commands`` gate is for, and it now names the container
    dispatch — but it removes the silent-wrong-verdict half.
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
        # KNOWN EQUIVALENCE GAP, made loud because it cannot be closed here.
        #
        # This is a SECOND, fresh container. Only `workspace` crosses from the
        # one that ran the agent; everything that container's `pre_run` did
        # OUTSIDE the workspace is gone -- and `pre_run` is not re-run, because
        # `Sandbox.adopt` sets `was_adopted` and the orchestrator skips it (it
        # would otherwise overwrite the agent's deliverables before the criteria
        # read them, which is the defect that skip exists for).
        #
        # It is not hypothetical: `tasks/samples/skillsbench/3d-scan-calc`'s
        # pre_run does `ln -sfn "$PWD/mass_report.json" /root/mass_report.json`
        # and its verifier's first assertion is that /root/mass_report.json
        # exists. In a fresh container /root is pristine, so the row scores 0.000
        # for a trajectory `run` scores 1.000.
        #
        # Re-running pre_run here would trade this bug for the deliverable-
        # clobbering one, so the honest move is to name it at dispatch and let
        # the operator use --allow-host-grading or a single `run`.
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
        # The SECOND known equivalence gap, and the one the user guide already
        # promised was warned about while nothing emitted it.
        #
        # `_build_image` re-runs `docker build` on every dispatch under the
        # deterministic tag `coder-eval-task-<id>:built`, so the grading image
        # REPLACES the run's under the same name. A Dockerfile, build context or
        # base image that moved between the two phases means the criteria read a
        # different filesystem than the agent did, and the score changes for
        # identical agent output.
        #
        # Nothing pins or records image identity on either side yet, so this
        # cannot be detected after the fact -- which is exactly why it is said
        # at dispatch. (A `reference_digest`-style pin on the resolved image is
        # the real fix and is deliberately NOT attempted here; it needs the
        # identity recorded on the RUN side too, which is a change to the run
        # path rather than to grading.)
        logger.warning(
            "Task %r builds its image from %s, so this grading pass re-runs `docker build`. If the "
            + "Dockerfile, its build context or its base image changed since the run, the criteria "
            + "read a different filesystem than the agent did and the score may differ for identical "
            + "agent output. Nothing records the image identity, so this cannot be detected "
            + "afterwards -- grade with a single `coder-eval run` if the image may have moved.",
            task.task_id,
            task.sandbox.docker.dockerfile_path,
        )
    # A SCRATCH output dir, never the caller's. The two callers disagree about
    # what `run_dir` is -- `evaluate` passes a freshly prepared directory, while
    # `run --resume` passes the executed row's OWN directory -- and every part of
    # DockerRunner's result handling assumes an output dir it alone populates:
    #
    #  * `_parse_result_or_raise` decides "did the container produce a result?"
    #    on `task_json.exists()` and discards `returncode`. Over the row's own
    #    directory the pre-grade `task.json` is already there, so a grading
    #    container that DIED (OOM, exit 137, or any of `_grade_recorded_run`'s
    #    own FATAL guards) was read back as a successful grade -- returning the
    #    stale ungraded row as the verdict, with the container's error discarded.
    #  * `run()` opens `run_dir/docker.log` with mode "w", truncating the
    #    executed container's log -- the same loss the task.log/grade.log split
    #    was introduced to prevent.
    #  * `grant_container_access(output_dir, writable=True)` would recursively
    #    widen the whole preserved artifacts tree.
    #
    # Giving the container a private directory makes both callers identical and
    # makes the docstring above true, rather than true of one caller.
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
                # The grading pass owns its scratch dir and nothing else. The
                # workspace is a bind mount of the ORIGINAL run's output and must
                # survive untouched.
                preservation_mode=PreservationMode.NONE,
                prior_result=prior,
                grade_workspace=workspace,
            ).run()
        except (DockerRunError, OSError) as e:
            # Wrapped, because `orchestration/` must not leak an isolation-layer
            # exception to the CLI, and because the actionable next step is the
            # host-grading escape hatch rather than a docker stack trace. OSError
            # joins it because the staging copies (`_prepare_task_dir_mount`,
            # `_prepare_reference_mount`) and the log open raise it unwrapped,
            # and a raw traceback would drop the guidance below.
            raise RegradeError(
                f"Grading {task.task_id!r} in a container failed: {e}. The container's own output was "
                + f"kept at {run_dir / GRADE_DOCKER_LOG_FILENAME}. Re-run with --allow-host-grading "
                + "to grade on this machine instead (path- and toolchain-dependent criteria may then "
                + "score differently, and the row is stamped graded_on_host)."
            ) from e
        finally:
            # ALWAYS, not only on success: the scratch dir is deleted the moment
            # this `with` exits, and everything that explains a failure lives in
            # it. See `_fold_back_container_logs`.
            _fold_back_container_logs(container_run_dir, run_dir)
        # Fold the grade back into the row the caller asked about, mirroring what
        # the host path does in place. `back_up_pre_grade_record` has already
        # preserved task.execute.json, so this write is the graded record.
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
) -> EvaluationResult:
    """Run ``task``'s criteria against an already-executed ``workspace``.

    The workspace is *adopted*, never copied: it is the run's own output, and the
    template-copy path filters out ``node_modules`` / ``dist`` / ``build`` /
    ``.venv``, which would make a criterion reading those fail as a copying
    artifact rather than as a verdict.

    ``prior`` supplies the trajectory and the run's execution facts (see
    ``Orchestrator._seed_from_prior_result``), so criteria that read the agent's
    tool calls score exactly as they would have during the run.

    ``recorded_task`` / ``recorded_task_file`` are the two halves of one seam:
    what the row RECORDS, as distinct from what this process runs and resolves
    paths against. Both matter only in the container, where the task is rewritten
    to ``driver: tempdir`` and every path is a container path. Omitting the file
    half made every container-graded row re-record ``/work/task_dir/task.yaml``
    as its ``source_file`` -- a path that exists on no host, which is the exact
    defect ``Orchestrator.recorded_task_file`` was added to fix, reintroduced one
    caller down. A later ``evaluate <run_dir>`` on such a row then hits
    ``_grade_in_container``'s own "a task file that is not on this host" refusal.
    """
    from coder_eval.orchestrator import Orchestrator

    # Every path through this function grades, so an empty `success_criteria`
    # is never legal here the way it is for `execute` (which never calls this
    # function at all). Checked once, at the single choke point every re-grade
    # entry point (`evaluate <run_dir>`, `run --resume`'s `to_grade` set, and
    # the container-dispatch branch below) shares -- a criteria-free task would
    # otherwise finalize as `FinalStatus.SUCCESS` at `weighted_score: 0.0`
    # (`all_criteria_passed([])` is vacuously `True`,
    # `calculate_weighted_score([])` writes `0.0`), an internally contradictory
    # "successful" result for what is actually a misconfigured task.
    if not task.success_criteria:
        raise RegradeError(
            f"task {task.task_id!r} has no `success_criteria` and cannot be graded (it would silently "
            + "score SUCCESS at weighted_score 0.0). Add at least one criterion before re-grading it."
        )

    # A `driver: docker` row is graded INSIDE a container of the same image,
    # which is the only place its criteria mean what they meant during the run.
    # Dispatched before anything else here, including the reference check, so the
    # container performs every step against container paths rather than having
    # half of it done against the host's.
    if _should_grade_in_container(task, allow_host_grading=allow_host_grading):
        # `recorded_task` is NOT forwarded, and that is deliberate rather than an
        # omission: the container re-derives it from the staged task.yaml (see
        # `run_task_internal_command`'s `authored_task`), which IS this `task`.
        # Accepting a DIFFERENT one and dropping it would leave no evidence, so
        # say so instead -- the whole point of the seam is that the record must
        # not quietly disagree with what was authored.
        if recorded_task is not None and recorded_task != task:
            raise RegradeError(
                "recorded_task cannot be honored when grading in a container: the container rebuilds "
                + "the recorded task from the staged task.yaml. Pass the same task, or grade with "
                + "--allow-host-grading."
            )
        if recorded_task_file is not None and recorded_task_file != task_file:
            # Same rule as `recorded_task` above. The container receives this
            # value over `context.json`'s `host_task_file`, which DockerRunner
            # fills from `rt.task_file` -- i.e. from `task_file` here. A
            # different one could not be honored, so say so rather than drop it.
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
    )
    result = await orchestrator.run()
    stamp_host_grading(result, task)
    return result
