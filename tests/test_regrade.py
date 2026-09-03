"""``orchestration/regrade.py`` — the refusals, not the happy path.

The end-to-end loop test covers a successful re-grade of the agentless task. What
it cannot cover is every branch that REFUSES to grade, and those are the ones that
matter: each exists because grading anyway would publish a plausible number that
is wrong. The reference-digest guard in particular shipped as dead code (nothing
wrote the key it read) precisely because the only test that reached it used a
fixture with no reference at all.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from coder_eval.models import (
    AgentKind,
    EvaluationResult,
    FileExistsCriterion,
    FinalStatus,
    RunCommandCriterion,
    TaskConfigRecord,
    TaskDefinition,
    parse_agent_config,
)
from coder_eval.orchestration.regrade import (
    PRE_GRADE_JSON,
    TASK_JSON,
    RegradeError,
    back_up_pre_grade_record,
    default_workspace,
    load_prior_result,
    task_from_prior,
    verify_reference_unchanged,
)
from coder_eval.path_utils import digest_tree


def _task(*, reference: dict[str, str] | None = None, command: str | None = None) -> TaskDefinition:
    criteria: list[object] = [FileExistsCriterion(path="x.txt", description="x")]
    if command is not None:
        criteria.append(RunCommandCriterion(command=command, description="run it"))
    return TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
        reference=reference,  # type: ignore[arg-type]
        success_criteria=criteria,  # type: ignore[arg-type]
    )


def _result(**kwargs: object) -> EvaluationResult:
    from datetime import datetime

    base: dict[str, object] = {
        "task_id": "t",
        "task_description": "d",
        "variant_id": "v",
        "agent_type": AgentKind.CLAUDE_CODE,
        "started_at": datetime(2020, 1, 1),
        "final_status": FinalStatus.NOT_GRADED,
        "iteration_count": 1,
    }
    base.update(kwargs)
    return EvaluationResult(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# load_prior_result
# --------------------------------------------------------------------------


def test_missing_task_json_is_a_regrade_error(tmp_path: Path) -> None:
    with pytest.raises(RegradeError, match="Cannot read"):
        load_prior_result(tmp_path)


def test_unparseable_task_json_is_a_regrade_error(tmp_path: Path) -> None:
    (tmp_path / TASK_JSON).write_text("{not json", encoding="utf-8")
    with pytest.raises(RegradeError, match="not a readable EvaluationResult"):
        load_prior_result(tmp_path)


# --------------------------------------------------------------------------
# task_from_prior — which task gets graded
# --------------------------------------------------------------------------


def test_no_task_config_refuses_rather_than_guessing(tmp_path: Path) -> None:
    with pytest.raises(RegradeError, match="carries no task_config"):
        task_from_prior(_result(), tmp_path)


def test_resolved_config_wins_over_the_source_yaml(tmp_path: Path) -> None:
    """`resolved` is post-merge, so it carries variant overrides / -D / dataset
    expansion. Re-reading the YAML would grade a DIFFERENT task."""
    source = tmp_path / "t.yaml"
    source.write_text("task_id: from-yaml\n", encoding="utf-8")
    resolved = _task().model_dump(mode="json")
    resolved["task_id"] = "from-resolved"
    prior = _result(task_config=TaskConfigRecord(resolved=resolved, source_yaml="raw", source_file=str(source)))

    task, _ = task_from_prior(prior, tmp_path)

    assert task.task_id == "from-resolved"


def test_unusable_resolved_config_with_no_source_refuses(tmp_path: Path) -> None:
    prior = _result(task_config=TaskConfigRecord(resolved={"nonsense": True}, source_yaml="raw", source_file=None))
    with pytest.raises(RegradeError, match="no longer validates"):
        task_from_prior(prior, tmp_path)


def test_source_fallback_is_loud(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A quiet fallback would silently grade a task other than the one that ran."""
    source = tmp_path / "t.yaml"
    source.write_text(
        "task_id: from-yaml\ndescription: d\ninitial_prompt: p\n"
        + "success_criteria:\n  - type: file_exists\n    path: x.txt\n    description: x\n",
        encoding="utf-8",
    )
    prior = _result(
        task_config=TaskConfigRecord(resolved={"nonsense": True}, source_yaml="raw", source_file=str(source))
    )

    with caplog.at_level(logging.WARNING):
        task, _ = task_from_prior(prior, tmp_path)

    assert task.task_id == "from-yaml"
    assert "NOT reapplied" in caplog.text


def test_shell_commands_from_a_run_dir_config_are_announced(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A run dir is a shareable artifact, and rebuilding from it decides what the
    grader executes. Intended, but never silent."""
    resolved = _task(command="echo surprising").model_dump(mode="json")
    prior = _result(task_config=TaskConfigRecord(resolved=resolved, source_yaml="raw", source_file=None))

    with caplog.at_level(logging.WARNING):
        task_from_prior(prior, tmp_path)

    assert "echo surprising" in caplog.text


# --------------------------------------------------------------------------
# default_workspace
# --------------------------------------------------------------------------


def test_recorded_sandbox_path_wins_when_it_still_exists(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert default_workspace(tmp_path, _result(sandbox_path=str(workspace))) == workspace


def test_falls_back_to_the_single_artifacts_child(tmp_path: Path) -> None:
    child = tmp_path / "artifacts" / "t"
    child.mkdir(parents=True)
    assert default_workspace(tmp_path, _result(sandbox_path="/gone")) == child


def test_flat_artifacts_dir_is_itself_the_workspace(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "file.txt").write_text("x", encoding="utf-8")
    assert default_workspace(tmp_path, _result()) == artifacts


def test_no_workspace_at_all_refuses(tmp_path: Path) -> None:
    with pytest.raises(RegradeError, match="No workspace to grade"):
        default_workspace(tmp_path, _result())


# --------------------------------------------------------------------------
# verify_reference_unchanged — the anti-cheat guard
# --------------------------------------------------------------------------


def _reference_task(tmp_path: Path) -> tuple[TaskDefinition, Path, Path]:
    task_file = tmp_path / "t.yaml"
    task_file.write_text("x", encoding="utf-8")
    reference = tmp_path / "ref"
    reference.mkdir()
    (reference / "answer.py").write_text("print('right')\n", encoding="utf-8")
    return _task(reference={"directory": "ref"}), task_file, reference


def test_an_edited_reference_refuses_the_grade(tmp_path: Path) -> None:
    """The headline guarantee. Without it, an answer key edited between execute
    and grade scores the agent's old work against a new one."""
    task, task_file, reference = _reference_task(tmp_path)
    prior = _result(environment_info={"reference_digest": digest_tree(reference)})
    verify_reference_unchanged(prior, task, task_file)  # unchanged: fine

    (reference / "answer.py").write_text("print('different')\n", encoding="utf-8")

    with pytest.raises(RegradeError, match="digest mismatch"):
        verify_reference_unchanged(prior, task, task_file)


def test_a_vanished_reference_refuses_rather_than_grading_without_one(tmp_path: Path) -> None:
    task, task_file, reference = _reference_task(tmp_path)
    prior = _result(environment_info={"reference_digest": digest_tree(reference)})
    for p in reference.iterdir():
        p.unlink()
    reference.rmdir()

    with pytest.raises(RegradeError):
        verify_reference_unchanged(prior, task, task_file)


def test_a_run_without_a_recorded_digest_says_so(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Silence here is what let the guard ship as dead code for a release."""
    task, task_file, _ = _reference_task(tmp_path)

    with caplog.at_level(logging.WARNING):
        verify_reference_unchanged(_result(), task, task_file)

    assert "cannot be verified" in caplog.text


def test_a_task_with_no_reference_is_not_checked(tmp_path: Path) -> None:
    verify_reference_unchanged(_result(), _task(), tmp_path / "t.yaml")


# --------------------------------------------------------------------------
# back_up_pre_grade_record
# --------------------------------------------------------------------------


def test_the_pre_grade_record_is_written_once(tmp_path: Path) -> None:
    """A second grade must not overwrite the ORIGINAL execute record with an
    already-graded one — that is the only evidence the run was ungraded."""
    (tmp_path / TASK_JSON).write_text('{"round": 1}', encoding="utf-8")
    back_up_pre_grade_record(tmp_path)
    (tmp_path / TASK_JSON).write_text('{"round": 2}', encoding="utf-8")
    back_up_pre_grade_record(tmp_path)

    assert json.loads((tmp_path / PRE_GRADE_JSON).read_text(encoding="utf-8")) == {"round": 1}


def test_backup_is_a_no_op_with_nothing_to_back_up(tmp_path: Path) -> None:
    back_up_pre_grade_record(tmp_path)
    assert not (tmp_path / PRE_GRADE_JSON).exists()


def test_a_failed_backup_never_fails_the_grade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The audit copy is a convenience; the verdict is the deliverable."""
    (tmp_path / TASK_JSON).write_text("{}", encoding="utf-8")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "write_text", _boom)
    back_up_pre_grade_record(tmp_path)  # must not raise
