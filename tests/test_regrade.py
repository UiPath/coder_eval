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
    RegradeError,
    back_up_pre_grade_record,
    default_workspace,
    load_prior_result,
    task_from_prior,
    verify_reference_unchanged,
)
from coder_eval.path_utils import PRE_GRADE_JSON_FILENAME, TASK_JSON_FILENAME, digest_tree


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
    (tmp_path / TASK_JSON_FILENAME).write_text("{not json", encoding="utf-8")
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


def test_shell_commands_from_a_run_dir_config_are_refused_by_default(tmp_path: Path) -> None:
    """A run dir is a shareable artifact, and rebuilding from it decides what the
    grader executes with the grader's credentials.

    A warning is not a control — it is printed as the command is already being
    prepared. So the default is REFUSAL, and the message names both the commands
    and the way to accept them."""
    resolved = _task(command="echo surprising").model_dump(mode="json")
    prior = _result(task_config=TaskConfigRecord(resolved=resolved, source_yaml="raw", source_file=None))

    with pytest.raises(RegradeError) as exc:
        task_from_prior(prior, tmp_path)

    assert "echo surprising" in str(exc.value)
    assert "--allow-recorded-commands" in str(exc.value)


def test_the_opt_in_accepts_them_and_still_names_them(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Accepted is not the same as invisible: the opt-in still logs what will run."""
    resolved = _task(command="echo surprising").model_dump(mode="json")
    prior = _result(task_config=TaskConfigRecord(resolved=resolved, source_yaml="raw", source_file=None))

    with caplog.at_level(logging.WARNING):
        task, _ = task_from_prior(prior, tmp_path, allow_recorded_commands=True)

    assert task.task_id
    assert "echo surprising" in caplog.text


def test_a_config_with_no_shell_needs_no_opt_in(tmp_path: Path) -> None:
    """The common case — execute then evaluate your own file/JSON criteria — is
    unaffected, or the gate would just be turned off."""
    resolved = _task().model_dump(mode="json")
    prior = _result(task_config=TaskConfigRecord(resolved=resolved, source_yaml="raw", source_file=None))

    task, _ = task_from_prior(prior, tmp_path)
    assert task.task_id


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
    (tmp_path / TASK_JSON_FILENAME).write_text('{"round": 1}', encoding="utf-8")
    back_up_pre_grade_record(tmp_path)
    (tmp_path / TASK_JSON_FILENAME).write_text('{"round": 2}', encoding="utf-8")
    back_up_pre_grade_record(tmp_path)

    assert json.loads((tmp_path / PRE_GRADE_JSON_FILENAME).read_text(encoding="utf-8")) == {"round": 1}


def test_backup_is_a_no_op_with_nothing_to_back_up(tmp_path: Path) -> None:
    back_up_pre_grade_record(tmp_path)
    assert not (tmp_path / PRE_GRADE_JSON_FILENAME).exists()


def test_a_failed_backup_never_fails_the_grade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The audit copy is a convenience; the verdict is the deliverable."""
    (tmp_path / TASK_JSON_FILENAME).write_text("{}", encoding="utf-8")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "write_text", _boom)
    back_up_pre_grade_record(tmp_path)  # must not raise


# --------------------------------------------------------------------------
# Dataset-row ids contain "/"
# --------------------------------------------------------------------------


def test_a_dataset_row_workspace_resolves_by_task_id_not_by_child_count(tmp_path: Path) -> None:
    """Preservation writes artifacts/<task_id>, and a dataset row's task_id is
    "<suite>/<row>". "The single child of artifacts/" therefore resolves to
    artifacts/<suite> — one level too high — and every path-relative criterion
    then fails as a locating artifact rather than as a verdict."""
    workspace = tmp_path / "artifacts" / "suite" / "row-1"
    workspace.mkdir(parents=True)

    resolved = default_workspace(tmp_path, _result(task_id="suite/row-1"))

    assert resolved == workspace


def test_an_ambiguous_artifacts_dir_refuses_rather_than_guessing(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    (artifacts / "a").mkdir(parents=True)
    (artifacts / "b").mkdir()

    with pytest.raises(RegradeError, match="Pass --workspace"):
        default_workspace(tmp_path, _result(task_id="neither"))


def test_a_sandbox_path_outside_the_run_dir_is_refused(tmp_path: Path) -> None:
    """`sandbox_path` is an unvalidated absolute path out of the run's own
    task.json, and criteria execute with cwd there and may mutate it."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(RegradeError, match="outside the run directory"):
        default_workspace(run_dir, _result(sandbox_path=str(outside)))


# --------------------------------------------------------------------------
# Grading a `driver: docker` row — inside a container of its own image
#
# Such a task's criteria address the IMAGE's paths and toolchain, so grading
# them on the host answers a different question. Demonstrated on
# `tasks/byod_smoke_test.yaml`, whose criterion is `test -f /opt/byod_marker`
# (baked into the BYOD image): the identical row scores SUCCESS 1.000 graded in
# a container and FAILURE 0.000 graded on the host. That is not a flaky
# difference — it is the host answering "is the marker on THIS machine", which
# nobody asked.
#
# So the docker row is now DISPATCHED to a grading container rather than
# refused. `--allow-host-grading` keeps its old meaning: grade here anyway (no
# docker available, or criteria known to be host-portable), and wear the
# `graded_on_host` stamp.
# --------------------------------------------------------------------------


def _docker_task() -> TaskDefinition:
    from coder_eval.models import SandboxConfig

    task = _task()
    return task.model_copy(update={"sandbox": SandboxConfig(driver="docker")})


class TestShouldGradeInContainer:
    """The routing decision, as a truth table.

    Each row rules out a different wrong answer, so they are asserted
    separately rather than as one compound expression.
    """

    def test_a_docker_row_on_the_host_goes_to_a_container(self) -> None:
        from coder_eval.orchestration.regrade import _should_grade_in_container

        assert _should_grade_in_container(_docker_task(), allow_host_grading=False) is True

    def test_allow_host_grading_still_wins(self) -> None:
        """The escape hatch must keep working — it is the only option on a
        machine without docker, and the row it produces is stamped."""
        from coder_eval.orchestration.regrade import _should_grade_in_container

        assert _should_grade_in_container(_docker_task(), allow_host_grading=False) is True
        assert _should_grade_in_container(_docker_task(), allow_host_grading=True) is False

    def test_a_tempdir_row_never_starts_a_container(self) -> None:
        from coder_eval.orchestration.regrade import _should_grade_in_container

        assert _should_grade_in_container(_task(), allow_host_grading=False) is False

    def test_inside_a_container_it_does_not_recurse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Gated on CODER_EVAL_IN_CONTAINER, never on the driver.

        The in-container entry point rewrites `docker` -> `tempdir` before
        building its Orchestrator, so a driver-based test would read a value
        that has already been changed — the same trap the reference-permission
        window documents. Without this gate a grading container dispatches a
        grading container.
        """
        from coder_eval.models import IN_CONTAINER_ENV
        from coder_eval.orchestration.regrade import _should_grade_in_container

        monkeypatch.setenv(IN_CONTAINER_ENV, "1")
        assert _should_grade_in_container(_docker_task(), allow_host_grading=False) is False


class TestGradeInContainerDispatch:
    async def test_it_dispatches_with_the_prior_row_and_the_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both must reach DockerRunner, and they are what make the grade real:
        the prior row supplies the trajectory a judge or `command_executed`
        criterion reads, and the workspace is the tree under evaluation."""
        import coder_eval.isolation.docker_runner as dr

        captured: dict[str, object] = {}
        graded = _result(final_status=FinalStatus.SUCCESS, weighted_score=1.0)

        class _FakeRunner:
            def __init__(self, rt, **kw):
                captured.update(kw)
                captured["run_dir"] = rt.run_dir
                captured["task_id"] = rt.task.task_id

            async def run(self):
                return graded

        monkeypatch.setattr(dr, "DockerRunner", _FakeRunner)

        from coder_eval.models import PreservationMode
        from coder_eval.orchestration.regrade import regrade_in_place

        workspace = tmp_path / "ws"
        workspace.mkdir()
        task_file = tmp_path / "t.yaml"
        task_file.write_text("task_id: t\n", encoding="utf-8")
        prior = _result()

        out = await regrade_in_place(
            task=_docker_task(),
            prior=prior,
            workspace=workspace,
            run_dir=tmp_path / "grade-run",
            task_file=task_file,
            source_yaml="",
            variant_id="v",
        )

        assert out is graded
        assert captured["prior_result"] is prior
        assert captured["grade_workspace"] == workspace
        # The workspace belongs to the ORIGINAL run; a grading pass must never
        # move or delete it.
        assert captured["preservation_mode"] is PreservationMode.NONE
        # The grade writes into its OWN run dir, which the caller then folds back
        # into the row (preserving task.execute.json) — not into the row directly.
        assert captured["run_dir"] == tmp_path / "grade-run"

    async def test_without_a_task_file_it_refuses_and_names_the_escape_hatch(self, tmp_path: Path) -> None:
        """The image is resolved relative to the task file; with none there is
        nothing to build from. A real limitation, so it says so rather than
        silently grading on the host."""
        from coder_eval.orchestration.regrade import regrade_in_place

        workspace = tmp_path / "ws"
        workspace.mkdir()
        with pytest.raises(RegradeError, match="allow-host-grading"):
            await regrade_in_place(
                task=_docker_task(),
                prior=_result(),
                workspace=workspace,
                run_dir=tmp_path / "r",
                task_file=None,
                source_yaml="",
                variant_id="v",
            )

    async def test_a_container_failure_becomes_a_regrade_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`orchestration/` must not leak an isolation-layer exception to the
        CLI, and the actionable next step is the escape hatch, not a docker
        stack trace."""
        import coder_eval.isolation.docker_runner as dr

        class _Boom:
            def __init__(self, rt, **kw):
                pass

            async def run(self):
                raise dr.DockerRunError("image pull failed")

        monkeypatch.setattr(dr, "DockerRunner", _Boom)

        from coder_eval.orchestration.regrade import regrade_in_place

        workspace = tmp_path / "ws"
        workspace.mkdir()
        task_file = tmp_path / "t.yaml"
        task_file.write_text("task_id: t\n", encoding="utf-8")

        with pytest.raises(RegradeError, match="image pull failed"):
            await regrade_in_place(
                task=_docker_task(),
                prior=_result(),
                workspace=workspace,
                run_dir=tmp_path / "r",
                task_file=task_file,
                source_yaml="",
                variant_id="v",
            )


class TestDockerRunnerGradingWiring:
    """The three things the container needs, asserted on the wire format.

    A grading container differs from a running one only in what it is handed, so
    these are the contract with `run_task_internal_command`'s regrade branch.
    """

    @staticmethod
    def _runner(tmp_path: Path, **kw):
        from coder_eval.isolation.docker_runner import DockerRunner
        from coder_eval.models import ResolvedTask

        task_file = tmp_path / "t.yaml"
        task_file.write_text("task_id: t\n", encoding="utf-8")
        rt = ResolvedTask(
            task=_docker_task(), task_file=task_file, run_dir=tmp_path / "run", variant_id="v", source_yaml=""
        )
        return DockerRunner(rt, **kw)

    def test_prior_result_and_workspace_must_be_passed_together(self, tmp_path: Path) -> None:
        """Either alone is a routing bug: a prior with no workspace has nothing
        to grade, a workspace with no prior loses the trajectory."""
        ws = tmp_path / "ws"
        ws.mkdir()
        with pytest.raises(ValueError, match="together"):
            self._runner(tmp_path, prior_result=_result())
        with pytest.raises(ValueError, match="together"):
            self._runner(tmp_path, grade_workspace=ws)

    async def test_a_grading_run_stages_the_prior_row_and_flags_the_regrade(self, tmp_path: Path) -> None:
        from coder_eval.path_utils import PRIOR_RESULT_FILENAME

        ws = tmp_path / "ws"
        ws.mkdir()
        prior = _result(weighted_score=None)
        runner = self._runner(tmp_path, prior_result=prior, grade_workspace=ws)

        staged = tmp_path / "input"
        staged.mkdir()
        await runner._stage_inputs(staged)

        context = json.loads((staged / "context.json").read_text(encoding="utf-8"))
        assert context["regrade"] is True
        # A bool, not a string: the container coerces and rejects non-bools,
        # because a truthy `"false"` here would re-RUN the agent over the very
        # workspace the operator asked only to grade.
        assert isinstance(context["regrade"], bool)

        recovered = EvaluationResult.model_validate_json((staged / PRIOR_RESULT_FILENAME).read_text(encoding="utf-8"))
        assert recovered.task_id == prior.task_id
        assert recovered.final_status is FinalStatus.NOT_GRADED

    async def test_an_ordinary_run_stages_neither(self, tmp_path: Path) -> None:
        """The control: a normal `run` must be byte-identical to before, and in
        particular must not acquire a prior.json nobody asked for."""
        from coder_eval.path_utils import PRIOR_RESULT_FILENAME

        runner = self._runner(tmp_path)
        staged = tmp_path / "input"
        staged.mkdir()
        await runner._stage_inputs(staged)

        context = json.loads((staged / "context.json").read_text(encoding="utf-8"))
        assert context["regrade"] is False
        assert not (staged / PRIOR_RESULT_FILENAME).exists()

    def test_the_workspace_is_mounted_read_write_at_the_container_path(self, tmp_path: Path) -> None:
        """Read-WRITE and not a copy. Criteria legitimately mutate what they
        grade (a `run_command` that compiles, a post_run that cleans up), and
        copying is what the host path proved wrong: the template filter drops
        node_modules / dist / build / .venv, so a criterion reading those fails
        as a copying artifact rather than as a verdict.
        """
        from coder_eval.models import CONTAINER_GRADE_WORKSPACE

        ws = tmp_path / "ws"
        ws.mkdir()
        runner = self._runner(tmp_path, prior_result=_result(), grade_workspace=ws)
        argv = runner._build_argv(tmp_path / "input", tmp_path / "out", container_name="c", image="img")

        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        assert f"{ws.resolve()}:{CONTAINER_GRADE_WORKSPACE}" in mounts, mounts
        assert not any(m.endswith(f":{CONTAINER_GRADE_WORKSPACE}:ro") for m in mounts)

    def test_an_ordinary_run_mounts_no_grading_workspace(self, tmp_path: Path) -> None:
        from coder_eval.models import CONTAINER_GRADE_WORKSPACE

        runner = self._runner(tmp_path)
        argv = runner._build_argv(tmp_path / "input", tmp_path / "out", container_name="c", image="img")
        assert CONTAINER_GRADE_WORKSPACE not in " ".join(argv)
