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

from coder_eval.models import EvaluationResult, PreservationMode, TaskDefinition
from coder_eval.sandbox import Sandbox


logger = logging.getLogger(__name__)

TASK_JSON = "task.json"
PRE_GRADE_JSON = "task.execute.json"
ARTIFACTS_DIRNAME = "artifacts"


class RegradeError(Exception):
    """A finished run cannot be re-graded as asked."""


def load_prior_result(run_dir: Path) -> EvaluationResult:
    """Read a finished run's ``task.json``."""
    path = run_dir / TASK_JSON
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
    from .task_loader import load_task

    record = prior.task_config
    if record is None:
        raise RegradeError(
            f"{run_dir / TASK_JSON} carries no task_config, so the executed task cannot be "
            + "rebuilt. Pass the task file explicitly: coder-eval evaluate <task.yaml> <run_dir>"
        )
    try:
        return TaskDefinition.model_validate(record.resolved), record.source_yaml
    except ValueError as e:
        if not record.source_file or not Path(record.source_file).is_file():
            raise RegradeError(
                f"The resolved task config in {run_dir / TASK_JSON} no longer validates ({e}), and "
                + "its source YAML is unavailable. Pass the task file explicitly."
            ) from e
        logger.warning(
            "The recorded resolved config does not validate (%s); falling back to %s. Variant "
            + "overrides, -D flags and dataset expansion from the original run are NOT reapplied, "
            + "so this grade may not match what ran.",
            e,
            record.source_file,
        )
        return load_task(Path(record.source_file))


def default_workspace(run_dir: Path, prior: EvaluationResult) -> Path:
    """Locate the workspace a finished run left behind.

    ``sandbox_path`` is authoritative when it still exists — it is where the run
    actually worked. Otherwise fall back to the preserved artifacts tree, whose
    single child is named for the task.
    """
    if prior.sandbox_path:
        recorded = Path(prior.sandbox_path)
        if recorded.is_dir():
            return recorded

    artifacts = run_dir / ARTIFACTS_DIRNAME
    if not artifacts.is_dir():
        raise RegradeError(
            f"No workspace to grade: {artifacts} does not exist and the recorded sandbox_path "
            + f"({prior.sandbox_path or 'unset'}) is gone. The run was probably made with "
            + "--preservation-mode NONE."
        )
    children = [p for p in sorted(artifacts.iterdir()) if p.is_dir()]
    # Preservation nests the workspace under the task id; a flat artifacts dir
    # (no subdirectory) means the workspace IS artifacts/.
    return children[0] if len(children) == 1 else artifacts


def verify_reference_unchanged(prior: EvaluationResult, task: TaskDefinition) -> None:
    """Refuse to grade when the reference tree changed since the run.

    ``reference_comparison`` and reference-carrying judges score against
    ``task.reference.directory``. If it moved since the run, the re-grade would
    silently measure the agent's old work against a new answer key.
    """
    recorded = prior.environment_info.get("reference_digest")
    if not isinstance(recorded, str) or task.reference is None:
        return
    from coder_eval.path_utils import digest_tree

    from .evaluation import resolve_reference_dir

    resolved = resolve_reference_dir(task, None)
    if resolved is None or not resolved.is_dir():
        return
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
    source, backup = run_dir / TASK_JSON, run_dir / PRE_GRADE_JSON
    if backup.exists() or not source.is_file():
        return
    try:
        backup.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError as e:
        # Never fail a grade over the audit copy.
        logger.warning("Could not preserve the pre-grade record at %s: %s", backup, e)


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

    # Grading never runs a container: the docker driver dispatches through
    # DockerRunner, which needs an agent. Force tempdir so a task whose YAML says
    # `driver: docker` is still gradeable on the host.
    sandbox_config = task.sandbox.model_copy(deep=True).model_copy(update={"driver": "tempdir"})
    sandbox = Sandbox(
        sandbox_config,
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
