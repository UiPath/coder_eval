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
from coder_eval.path_utils import PRE_GRADE_JSON_FILENAME, TASK_JSON_FILENAME
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


def task_from_prior(prior: EvaluationResult, run_dir: Path) -> tuple[TaskDefinition, str]:
    """Rebuild the executed task from the run's own recorded config.

    Rebuilding from ``task_config.resolved`` rather than re-reading the YAML is
    what makes the grade describe the run that happened: ``resolved`` is the
    post-merge definition, so variant overrides, ``-D`` flags and dataset row
    expansion are all already baked in. Re-loading the source YAML would silently
    grade a DIFFERENT task whenever any of those were used.

    Falls back to the source YAML only when ``resolved`` will not validate (a
    schema change since the run), and says so loudly — a quiet fallback would
    reintroduce exactly the drift above.
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
        return _fall_back_to_source(record, run_dir, e)
    warn_on_embedded_commands(task, run_dir)
    return task, record.source_yaml


def warn_on_embedded_commands(task: TaskDefinition, run_dir: Path) -> None:
    """Name the shell commands a rebuilt config will execute on this host.

    ``task_config.resolved`` is data that travels inside a run directory, and a
    run directory is a shareable artifact — the detached-grading flow exists so
    one machine can execute and another can grade. Rebuilding the task from it
    means the *run dir* decides what ``run_command`` criteria the grader runs,
    with the grader's environment. That is the intended behavior (it is how the
    grade reproduces the executed config), but it must not be invisible: print
    what will run so an unexpected command is noticed before it executes.
    """
    commands = [cmd for c in task.success_criteria if isinstance(cmd := getattr(c, "command", None), str)]
    commands += [c.command for c in task.pre_run] + [c.command for c in task.post_run]
    if not commands:
        return
    logger.warning(
        "Grading %s runs %d shell command(s) taken from that run's own recorded config: %s",
        run_dir,
        len(commands),
        "; ".join(commands),
    )


def _fall_back_to_source(record: TaskConfigRecord, run_dir: Path, e: ValueError) -> tuple[TaskDefinition, str]:
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
    warn_on_embedded_commands(task, run_dir)
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
    by_task_id = artifacts / prior.task_id
    if by_task_id.is_dir():
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
    from coder_eval.path_utils import digest_tree

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
    if digest_tree(resolved) != recorded:
        raise RegradeError(
            f"The reference directory {resolved} changed since this run was executed "
            + "(digest mismatch). Grading now would score the agent's work against a "
            + "different answer key. Restore the reference, or re-run the task."
        )


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


def grading_sandbox_config(task: TaskDefinition) -> SandboxConfig:
    """The sandbox config a grading pass runs under.

    Grading never runs a container: the docker driver dispatches through
    DockerRunner, which needs an agent. Forcing ``tempdir`` keeps a task whose
    YAML says ``driver: docker`` gradeable on the host.

    Re-validated rather than ``model_copy(update=...)``: ``update`` skips both
    pydantic validation and pyright, so a typo would produce a SandboxConfig
    violating its own ``Literal`` and surface much later at an unrelated
    ``if driver == "docker"`` branch.
    """
    return SandboxConfig.model_validate({**task.sandbox.model_dump(), "driver": "tempdir"})


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
        grading_sandbox_config(task),
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
    return await orchestrator.run()
