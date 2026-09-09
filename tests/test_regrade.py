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
from datetime import timedelta
from pathlib import Path
from typing import ClassVar

import pytest

from coder_eval.models import (
    AgentKind,
    EvaluationResult,
    FileExistsCriterion,
    FinalStatus,
    PreservationMode,
    ResolvedTask,
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


class _RunnerDouble:
    """A DockerRunner stand-in bound to the REAL constructor signature.

    The first version took ``(rt, **kw)``, which swallowed every keyword and made
    a signature drift on the grading seam invisible: renaming or adding a
    required kwarg on `DockerRunner.__init__` left the doubles accepting it and
    the suite green. Tests are outside pyright's `include`, so nothing else
    catches it either. Spelling the parameters out turns such a rename into a
    TypeError here, which is the whole point of a double.
    """

    captured: ClassVar[dict[str, object]] = {}

    def __init__(
        self,
        rt: ResolvedTask,
        preservation_mode: PreservationMode = PreservationMode.DIRECT_WRITE,
        stream_callback: object = None,
        verbose: bool = False,
        grade: bool = True,
        prior_result: EvaluationResult | None = None,
        grade_workspace: Path | None = None,
    ) -> None:
        self.rt = rt
        type(self).captured = {
            "run_dir": rt.run_dir,
            "task_id": rt.task.task_id,
            "preservation_mode": preservation_mode,
            "prior_result": prior_result,
            "grade_workspace": grade_workspace,
        }

    async def run(self) -> EvaluationResult:  # pragma: no cover - overridden
        raise NotImplementedError


def _docker_task() -> TaskDefinition:
    from coder_eval.models import SandboxConfig

    task = _task()
    return task.model_copy(update={"sandbox": SandboxConfig(driver="docker")})


class TestShouldGradeInContainer:
    """The routing decision, as a truth table.

    Each row rules out a different wrong answer, so they are asserted
    separately rather than as one compound expression.
    """

    @pytest.fixture(autouse=True)
    def _on_the_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Three of these rows assert the HOST answer, so the ambient value of the
        gate variable must not decide the test. This repo's own container harness
        sets it on every task container, so inheriting it would flip two of them
        to a silent pass-for-the-wrong-reason."""
        from coder_eval.models import IN_CONTAINER_ENV

        monkeypatch.delenv(IN_CONTAINER_ENV, raising=False)

    def test_a_docker_row_on_the_host_goes_to_a_container(self) -> None:
        from coder_eval.orchestration.regrade import _should_grade_in_container

        assert _should_grade_in_container(_docker_task(), allow_host_grading=False) is True

    def test_allow_host_grading_still_wins(self) -> None:
        """The escape hatch must keep working — it is the only option on a
        machine without docker, and the row it produces is stamped."""
        from coder_eval.orchestration.regrade import _should_grade_in_container

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

        graded = _result(final_status=FinalStatus.SUCCESS, weighted_score=1.0)

        class _FakeRunner(_RunnerDouble):
            async def run(self) -> EvaluationResult:
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

        captured = _FakeRunner.captured
        assert out is graded
        assert captured["prior_result"] is prior
        assert captured["grade_workspace"] == workspace
        # The workspace belongs to the ORIGINAL run; a grading pass must never
        # move or delete it.
        assert captured["preservation_mode"] is PreservationMode.NONE
        # The grade writes into a SCRATCH dir the container alone owns, never the
        # caller's run_dir. `run --resume` passes the executed row's own
        # directory, where a pre-existing task.json would be read back as a
        # successful grade if the container died (`_parse_result_or_raise` keys
        # on existence and discards returncode) and where `docker.log` would be
        # truncated. The verdict is folded back afterwards.
        container_run_dir = captured["run_dir"]
        assert isinstance(container_run_dir, Path)
        assert container_run_dir != tmp_path / "grade-run"
        assert container_run_dir.name.startswith("coder-eval-grade-")

    async def test_the_container_grade_is_folded_back_into_the_callers_run_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The scratch dir is an implementation detail: the caller must still find
        the graded task.json where it asked for it. The container's own
        `docker.log` lands beside it under a PHASE-specific name, because on the
        resume path `docker.log` is already the executed run's."""
        import coder_eval.isolation.docker_runner as dr

        graded = _result(final_status=FinalStatus.SUCCESS, weighted_score=1.0)

        class _FakeRunner(_RunnerDouble):
            async def run(self) -> EvaluationResult:
                self.rt.run_dir.mkdir(parents=True, exist_ok=True)
                (self.rt.run_dir / "task.json").write_text(graded.model_dump_json(), encoding="utf-8")
                (self.rt.run_dir / "docker.log").write_text("container output", encoding="utf-8")
                (self.rt.run_dir / "grade.log").write_text("why each criterion scored", encoding="utf-8")
                return graded

        monkeypatch.setattr(dr, "DockerRunner", _FakeRunner)

        from coder_eval.orchestration.regrade import regrade_in_place

        workspace = tmp_path / "ws"
        workspace.mkdir()
        task_file = tmp_path / "t.yaml"
        task_file.write_text("task_id: t\n", encoding="utf-8")
        run_dir = tmp_path / "row"
        run_dir.mkdir()
        # The executed run's own container log, which the grade must not clobber.
        (run_dir / "docker.log").write_text("the executed run's log", encoding="utf-8")

        await regrade_in_place(
            task=_docker_task(),
            prior=_result(),
            workspace=workspace,
            run_dir=run_dir,
            task_file=task_file,
            source_yaml="",
            variant_id="v",
        )

        folded = EvaluationResult.model_validate_json((run_dir / "task.json").read_text(encoding="utf-8"))
        assert folded.final_status is FinalStatus.SUCCESS
        assert (run_dir / "grade.docker.log").read_text(encoding="utf-8") == "container output"
        assert (run_dir / "docker.log").read_text(encoding="utf-8") == "the executed run's log"
        # The grading pass's OWN log — the per-criterion detail, and a documented
        # part of the run-directory contract. It lived in the scratch dir and was
        # deleted with it, so a `driver: docker` row was the one shape a detached
        # grade left without a `grade.log`. It does NOT collide: a detached grade
        # is the only thing that ever writes that name here.
        assert (run_dir / "grade.log").read_text(encoding="utf-8") == "why each criterion scored"

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

        class _Boom(_RunnerDouble):
            async def run(self) -> EvaluationResult:
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

    async def test_a_grading_run_forwards_the_hosts_own_task_file_for_the_record(self, tmp_path: Path) -> None:
        """`task.json` must record a path that exists on a HOST.

        The container resolves TASK_DIR against `/work/task_dir/task.yaml`, which
        is right in there and meaningless anywhere else. Recording THAT made a
        detached grade rebuild the task around an unresolvable path: the dispatch
        guard saw a non-None Path and let it through, and `_prepare_task_dir_mount`
        then silently mounted nothing, so every `$TASK_DIR` criterion resolved
        against the wrong tree.
        """
        runner = self._runner(tmp_path)
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        await runner._stage_inputs(input_dir)

        context = json.loads((input_dir / "context.json").read_text(encoding="utf-8"))
        assert context["host_task_file"] == str(tmp_path / "t.yaml")


class TestRegradeSkewGuard:
    """A stale image must not turn a GRADE into a fresh agent run.

    Exactly the sibling of the `grade` guard one release earlier: `regrade`
    crosses the boundary only through context.json, so an image that predates
    container-side grading ignores the key and runs the agent — and the host
    would fold that fabricated trajectory back as the recorded row's verdict.
    """

    @staticmethod
    def _runner(tmp_path: Path, prior):
        from coder_eval.isolation.docker_runner import DockerRunner
        from coder_eval.models import ResolvedTask

        task_file = tmp_path / "t.yaml"
        task_file.write_text("task_id: t\n", encoding="utf-8")
        rt = ResolvedTask(
            task=_docker_task(), task_file=task_file, run_dir=tmp_path / "run", variant_id="v", source_yaml=""
        )
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        return DockerRunner(rt, prior_result=prior, grade_workspace=ws)

    def test_a_row_carrying_the_recorded_trajectory_is_accepted(self, tmp_path: Path) -> None:
        prior = _result()
        graded = _result(final_status=FinalStatus.SUCCESS, weighted_score=1.0)
        graded.started_at = prior.started_at
        self._runner(tmp_path, prior)._assert_regrade_honored(graded)

    def test_a_freshly_run_trajectory_is_refused_and_quarantined(self, tmp_path: Path) -> None:
        from coder_eval.isolation.docker_runner import DockerRunError

        prior = _result()
        rerun = _result(final_status=FinalStatus.SUCCESS, weighted_score=1.0)
        rerun.started_at = prior.started_at + timedelta(hours=1)

        task_json = tmp_path / "task.json"
        task_json.write_text("{}", encoding="utf-8")
        with pytest.raises(DockerRunError, match="re-ran the agent"):
            self._runner(tmp_path, prior)._assert_regrade_honored(rerun, task_json)

        # Refusing in memory while leaving contradictory bytes on disk is not a
        # refusal: a later `aggregate` would publish exactly this record.
        assert not task_json.exists()
        assert task_json.with_suffix(".json.rerun").is_file()

    def test_an_ordinary_run_is_never_checked(self, tmp_path: Path) -> None:
        """`prior_result is None` means nobody asked for a grade, so a fresh
        trajectory is the expected outcome, not a skew symptom."""
        from coder_eval.isolation.docker_runner import DockerRunner
        from coder_eval.models import ResolvedTask

        task_file = tmp_path / "t.yaml"
        task_file.write_text("task_id: t\n", encoding="utf-8")
        rt = ResolvedTask(
            task=_docker_task(), task_file=task_file, run_dir=tmp_path / "run", variant_id="v", source_yaml=""
        )
        DockerRunner(rt)._assert_regrade_honored(_result(final_status=FinalStatus.SUCCESS))


class TestContainerDispatchIsInsideTheTrustGate:
    """A run directory is a shareable artifact, so the image it names is untrusted.

    Grading a `driver: docker` row DISPATCHES A CONTAINER built from the recorded
    sandbox block — the record chooses an image that runs on this host with the
    default credential allowlist forwarded into it and a copy of ~/.claude
    mounted. That is a strictly wider capability than the `run_command` strings
    this gate already refuses, and it reached the host unprompted because the
    scan walked only success_criteria and post_run.
    """

    def test_the_dispatch_is_named_in_the_inventory(self) -> None:
        from coder_eval.orchestration.regrade import embedded_commands

        commands = embedded_commands(_docker_task(), include_setup_phase=False, include_container_dispatch=True)
        assert any("docker run" in c for c in commands), commands

    def test_it_is_silent_when_no_container_will_be_dispatched(self) -> None:
        """The gate must not fire for something that never runs — `--copy` is
        refused before it could dispatch, and a refusal that always fires stops
        being read."""
        from coder_eval.orchestration.regrade import embedded_commands

        assert embedded_commands(_docker_task(), include_setup_phase=False) == []

    def test_an_all_file_exists_docker_row_no_longer_sails_through(self, tmp_path: Path) -> None:
        """The exact bypass: zero embedded shell, so the gate returned early and
        the container was started with no consent at all."""
        from coder_eval.orchestration.regrade import RegradeError, check_embedded_commands

        with pytest.raises(RegradeError, match="docker run"):
            check_embedded_commands(
                _docker_task(),
                tmp_path,
                allow_recorded_commands=False,
                include_setup_phase=False,
                include_container_dispatch=True,
            )

    def test_the_operator_can_still_opt_in(self, tmp_path: Path) -> None:
        from coder_eval.orchestration.regrade import check_embedded_commands

        check_embedded_commands(
            _docker_task(),
            tmp_path,
            allow_recorded_commands=True,
            include_setup_phase=False,
            include_container_dispatch=True,
        )


class TestOperatorBaselineFailsClosed:
    """A missing or invalid baseline must NARROW the exemption, never widen it.

    This is the direction that fails silently: a broken baseline returning the
    wrong sentinel would make the trust gate stop prompting for authored
    `post_run` commands, and nothing would notice.
    """

    @staticmethod
    def _clear():
        from coder_eval.orchestration.regrade import _operator_baseline_post_run

        _operator_baseline_post_run.cache_clear()

    def test_a_broken_baseline_exempts_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import coder_eval.orchestration.experiment as exp
        from coder_eval.orchestration.regrade import _operator_baseline_post_run

        def _boom(_path):
            raise OSError("no such file")

        monkeypatch.setattr(exp, "load_experiment", _boom)
        self._clear()
        try:
            assert _operator_baseline_post_run() == frozenset()
        finally:
            self._clear()

    def test_a_baseline_without_defaults_exempts_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import coder_eval.orchestration.experiment as exp
        from coder_eval.orchestration.regrade import _operator_baseline_post_run

        monkeypatch.setattr(exp, "load_experiment", lambda _p: type("E", (), {"defaults": None})())
        self._clear()
        try:
            assert _operator_baseline_post_run() == frozenset()
        finally:
            self._clear()

    def test_with_no_exemption_the_baseline_command_is_scanned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The consequence, asserted rather than assumed: an empty baseline means
        every recorded post_run reaches the gate."""
        import coder_eval.orchestration.experiment as exp
        from coder_eval.models import PostRunCommand
        from coder_eval.orchestration.regrade import embedded_commands

        monkeypatch.setattr(exp, "load_experiment", lambda _p: type("E", (), {"defaults": None})())
        self._clear()
        try:
            task = _task()
            task.post_run = [PostRunCommand(command="rm -rf node_modules .npm-prefix", timeout=30)]
            assert "rm -rf node_modules .npm-prefix" in embedded_commands(task, include_setup_phase=False)
        finally:
            self._clear()


class TestEvaluateDispatchesADockerRow:
    """The reordering in `evaluate_command` is the fix; nothing pinned it.

    `delegates_to_regrade` exists solely because `grading_sandbox_config` --
    whose job is to REFUSE `driver: docker` -- was being called BEFORE the branch
    that no longer needs it, so no docker row could ever reach the container
    dispatch. Revert the hoist and every docker detached grade becomes a hard
    refusal again, with the suite still green.
    """

    @staticmethod
    def _docker_run_dir(tmp_path: Path) -> Path:
        """A run directory whose recorded config says `driver: docker`."""
        from coder_eval.models import TaskConfigRecord

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        task = _docker_task()
        prior = _result(
            weighted_score=None,
            final_status=FinalStatus.NOT_GRADED,
            task_config=TaskConfigRecord(
                resolved=task.model_dump(warnings=False),
                source_yaml="raw",
                source_file=None,
            ),
        )
        (run_dir / "task.json").write_text(prior.model_dump_json(), encoding="utf-8")
        (run_dir / "artifacts").mkdir()
        return run_dir

    def test_the_default_path_reaches_the_container_dispatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not the host-grading refusal: the dispatch. Asserted by the message
        the refusal would have produced being absent from the failure."""
        from coder_eval.models import IN_CONTAINER_ENV
        from coder_eval.orchestration import regrade as rg

        monkeypatch.delenv(IN_CONTAINER_ENV, raising=False)
        run_dir = self._docker_run_dir(tmp_path)
        prior = rg.load_prior_result(run_dir)
        task, _ = rg.task_from_prior(
            prior,
            run_dir,
            allow_recorded_commands=True,
            grade_in_place=True,
            allow_host_grading=False,
        )
        # The routing predicate the CLI's reordering exists to let run.
        assert rg._should_grade_in_container(task, allow_host_grading=False) is True

    def test_allow_host_grading_still_takes_the_host_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from coder_eval.models import IN_CONTAINER_ENV
        from coder_eval.orchestration import regrade as rg

        monkeypatch.delenv(IN_CONTAINER_ENV, raising=False)
        run_dir = self._docker_run_dir(tmp_path)
        prior = rg.load_prior_result(run_dir)
        task, _ = rg.task_from_prior(
            prior,
            run_dir,
            allow_recorded_commands=True,
            grade_in_place=True,
            allow_host_grading=True,
        )
        assert rg._should_grade_in_container(task, allow_host_grading=True) is False
        # And the host config it then builds is the downgraded one, stamped.
        assert rg.grading_sandbox_config(task, allow_host_grading=True).driver == "tempdir"


class TestContainerFailureKeepsItsEvidence:
    """A failed grading container must not delete the log its error names.

    `_grade_in_container` runs the whole dispatch inside a `TemporaryDirectory`,
    and every DockerRunner diagnostic — the container's merged stdout+stderr, the
    captured build log, the in-container FATAL guards — is written into it.
    Folding out only on SUCCESS destroyed precisely the evidence, while
    DockerRunError's own text says `See {log_path} for container output`, naming
    a path that no longer existed by the time it was printed.
    """

    async def test_the_container_log_survives_a_failed_grade(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import coder_eval.isolation.docker_runner as dr

        class _BoomAfterLogging(_RunnerDouble):
            async def run(self) -> EvaluationResult:
                self.rt.run_dir.mkdir(parents=True, exist_ok=True)
                (self.rt.run_dir / "docker.log").write_text("OOM: exit 137", encoding="utf-8")
                raise dr.DockerRunError("Container exited with code 137 without producing task.json.")

        monkeypatch.setattr(dr, "DockerRunner", _BoomAfterLogging)

        from coder_eval.orchestration.regrade import regrade_in_place

        workspace = tmp_path / "ws"
        workspace.mkdir()
        task_file = tmp_path / "t.yaml"
        task_file.write_text("task_id: t\n", encoding="utf-8")
        run_dir = tmp_path / "row"

        with pytest.raises(RegradeError) as excinfo:
            await regrade_in_place(
                task=_docker_task(),
                prior=_result(),
                workspace=workspace,
                run_dir=run_dir,
                task_file=task_file,
                source_yaml="",
                variant_id="v",
            )

        rescued = run_dir / "grade.docker.log"
        assert rescued.read_text(encoding="utf-8") == "OOM: exit 137"
        # And the message points at the path that still exists, not the deleted one.
        assert str(rescued) in str(excinfo.value)

    async def test_it_refuses_to_follow_a_symlink_at_the_log_destination(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`shutil.copy2` opens the destination for writing, which FOLLOWS a
        symlink there — an arbitrary-file-overwrite primitive in a run directory
        the grader did not create. The sibling verdict write goes through
        `write_text_atomic` for exactly this reason."""
        import coder_eval.isolation.docker_runner as dr

        graded = _result(final_status=FinalStatus.SUCCESS, weighted_score=1.0)

        class _FakeRunner(_RunnerDouble):
            async def run(self) -> EvaluationResult:
                self.rt.run_dir.mkdir(parents=True, exist_ok=True)
                (self.rt.run_dir / "task.json").write_text(graded.model_dump_json(), encoding="utf-8")
                (self.rt.run_dir / "docker.log").write_text("container output", encoding="utf-8")
                return graded

        monkeypatch.setattr(dr, "DockerRunner", _FakeRunner)

        from coder_eval.orchestration.regrade import regrade_in_place

        workspace = tmp_path / "ws"
        workspace.mkdir()
        task_file = tmp_path / "t.yaml"
        task_file.write_text("task_id: t\n", encoding="utf-8")
        run_dir = tmp_path / "row"
        run_dir.mkdir()
        victim = tmp_path / "victim.txt"
        victim.write_text("do not overwrite me", encoding="utf-8")
        (run_dir / "grade.docker.log").symlink_to(victim)

        await regrade_in_place(
            task=_docker_task(),
            prior=_result(),
            workspace=workspace,
            run_dir=run_dir,
            task_file=task_file,
            source_yaml="",
            variant_id="v",
        )

        assert victim.read_text(encoding="utf-8") == "do not overwrite me"

    async def test_an_unwritable_destination_fails_as_a_regrade_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fold-back sits outside the dispatch `try`, so a raw OSError
        reached the two callers differently and both were wrong: `evaluate`
        guards only RegradeError and let it escape into Typer as a stack trace
        AFTER a successful grade, while `run --resume` caught it and reported a
        computed, correct verdict as a grading failure."""
        import coder_eval.isolation.docker_runner as dr
        import coder_eval.orchestration.regrade as rg

        graded = _result(final_status=FinalStatus.SUCCESS, weighted_score=1.0)

        class _FakeRunner(_RunnerDouble):
            async def run(self) -> EvaluationResult:
                self.rt.run_dir.mkdir(parents=True, exist_ok=True)
                (self.rt.run_dir / "task.json").write_text(graded.model_dump_json(), encoding="utf-8")
                return graded

        monkeypatch.setattr(dr, "DockerRunner", _FakeRunner)

        def _boom(path: Path, text: str) -> None:
            raise OSError("Read-only file system")

        monkeypatch.setattr(rg, "write_text_atomic", _boom)

        workspace = tmp_path / "ws"
        workspace.mkdir()
        task_file = tmp_path / "t.yaml"
        task_file.write_text("task_id: t\n", encoding="utf-8")

        with pytest.raises(RegradeError, match="could not write the verdict"):
            await rg.regrade_in_place(
                task=_docker_task(),
                prior=_result(),
                workspace=workspace,
                run_dir=tmp_path / "row",
                task_file=task_file,
                source_yaml="",
                variant_id="v",
            )


class TestContainerGradeIsStampedAndCounted:
    """The two known equivalence gaps must travel WITH the row.

    `stamp_host_grading`'s own docstring states the rule: a console warning does
    not travel with `task.json` into `run.json`, the reports or the evalboard.
    Both gaps shipped as `logger.warning` only, so nothing downstream could tell
    a row whose `pre_run` never re-ran from one graded at full fidelity — and
    three of the ten in-tree `driver: docker` tasks match that pattern.
    """

    @staticmethod
    def _dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task: TaskDefinition) -> EvaluationResult:
        import coder_eval.isolation.docker_runner as dr

        graded = _result(final_status=FinalStatus.SUCCESS, weighted_score=1.0)

        class _FakeRunner(_RunnerDouble):
            async def run(self) -> EvaluationResult:
                self.rt.run_dir.mkdir(parents=True, exist_ok=True)
                (self.rt.run_dir / "task.json").write_text(graded.model_dump_json(), encoding="utf-8")
                return graded

        monkeypatch.setattr(dr, "DockerRunner", _FakeRunner)

        from coder_eval.orchestration.regrade import regrade_in_place

        workspace = tmp_path / "ws"
        workspace.mkdir()
        task_file = tmp_path / "t.yaml"
        task_file.write_text("task_id: t\n", encoding="utf-8")
        import asyncio

        return asyncio.run(
            regrade_in_place(
                task=task,
                prior=_result(),
                workspace=workspace,
                run_dir=tmp_path / "row",
                task_file=task_file,
                source_yaml="",
                variant_id="v",
            )
        )

    def test_skipped_pre_run_is_recorded_on_the_row(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from coder_eval.models import PreRunCommand

        task = _docker_task()
        task = task.model_copy(update={"pre_run": [PreRunCommand(command='ln -sfn "$PWD/out.json" /root/out.json')]})
        result = self._dispatch(tmp_path, monkeypatch, task)
        assert result.environment_info["graded_without_pre_run"] == 1

    def test_a_task_without_pre_run_carries_no_stamp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The stamp means "this row has a known gap". Writing it on every row
        would make it noise, and a marker that is always present filters
        nothing."""
        result = self._dispatch(tmp_path, monkeypatch, _docker_task())
        assert "graded_without_pre_run" not in result.environment_info

    def test_a_rebuilt_image_is_recorded_on_the_row(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`_build_image` re-runs `docker build` under the run's deterministic
        tag, so the grading image REPLACES it. Nothing pins image identity on
        either side, which is exactly why the row has to say it happened."""
        from coder_eval.models import SandboxConfig

        task = _docker_task()
        sandbox = SandboxConfig.model_validate(
            {
                **task.sandbox.model_dump(),
                "docker": {**task.sandbox.docker.model_dump(), "dockerfile_path": "Dockerfile"},
            }
        )
        result = self._dispatch(tmp_path, monkeypatch, task.model_copy(update={"sandbox": sandbox}))
        assert result.environment_info["graded_with_rebuilt_image"] == "Dockerfile"


class TestContainerDispatchIsOneCommandInThePrompt:
    """The consent prompt is the one place this text has to be exact.

    `check_embedded_commands` joins the list with "; " and interpolates
    `len(commands)`, so an argv FRAGMENT appended as its own entry is reported to
    the operator as a standalone shell command. A `dockerfile_path` task with two
    build args and one mount asked for approval of "4 shell command(s)", three of
    which were not commands.
    """

    @staticmethod
    def _dockerfile_task() -> TaskDefinition:
        from coder_eval.models import SandboxConfig

        task = _docker_task()
        sandbox = SandboxConfig.model_validate(
            {
                **task.sandbox.model_dump(),
                "docker": {
                    **task.sandbox.docker.model_dump(),
                    "dockerfile_path": "Dockerfile",
                    "build": {"args": {"FOO": "bar", "BAZ": "qux"}},
                    "extra_mounts": ["/a:/b"],
                },
            }
        )
        return task.model_copy(update={"sandbox": sandbox})

    def test_a_dockerfile_dispatch_is_exactly_one_command(self) -> None:
        from coder_eval.orchestration.regrade import embedded_commands

        commands = embedded_commands(
            self._dockerfile_task(), include_setup_phase=False, include_container_dispatch=True
        )
        assert len(commands) == 1
        only = commands[0]
        assert only.startswith("docker build -f Dockerfile")
        assert "--build-arg FOO=bar" in only
        assert "--build-arg BAZ=qux" in only
        assert "-v /a:/b" in only

    def test_it_names_the_task_directory_the_dispatch_copies_in(self, tmp_path: Path) -> None:
        """Disclosing only `sandbox.docker.*` asked the operator to consent to a
        strict subset of what happens. The task DIRECTORY is copied wholesale
        from the recorded `source_file`'s parent, so a record naming
        `~/.ssh/config` copies all of `~/.ssh` into the record-named image."""
        from coder_eval.orchestration.regrade import embedded_commands

        (commands,) = embedded_commands(
            _docker_task(),
            include_setup_phase=False,
            include_container_dispatch=True,
            task_file=tmp_path / "secrets" / "task.yaml",
        )
        assert str(tmp_path / "secrets") in commands
        assert "~/.claude" in commands

    def test_the_dispatch_is_absent_without_the_flag(self) -> None:
        """The whole block is behind `include_container_dispatch`, which is False
        on the --copy path — where `grading_sandbox_config` refuses before a
        container could be dispatched."""
        from coder_eval.orchestration.regrade import embedded_commands

        assert embedded_commands(self._dockerfile_task(), include_setup_phase=False) == []


class TestContainerGradeEmitsTelemetryHostSide:
    """ "Container silent, host emits once" — the grading path had only the first half.

    Every container this repo starts is launched with `TELEMETRY_ENABLED=false`
    (`DockerRunner._build_argv`, whose comment states the invariant verbatim).
    The RUN path supplies the host half in `orchestration/batch.py` right after
    parsing the container's result. The GRADING path inherited the silent half
    with no counterpart, so a verdict published by `evaluate <run_dir>` or
    `run --resume` over a `driver: docker` row reached no usage telemetry at all
    — while the byte-identical operation on a `tempdir` row, or the same row with
    `--allow-host-grading`, did.
    """

    async def test_a_container_grade_emits_task_end(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import coder_eval.isolation.docker_runner as dr
        import coder_eval.telemetry as telemetry

        graded = _result(final_status=FinalStatus.SUCCESS, weighted_score=1.0)

        class _FakeRunner(_RunnerDouble):
            async def run(self) -> EvaluationResult:
                self.rt.run_dir.mkdir(parents=True, exist_ok=True)
                (self.rt.run_dir / "task.json").write_text(graded.model_dump_json(), encoding="utf-8")
                return graded

        monkeypatch.setattr(dr, "DockerRunner", _FakeRunner)
        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(telemetry, "track_event", lambda name, props: events.append((name, props)))

        from coder_eval.orchestration.regrade import regrade_in_place

        workspace = tmp_path / "ws"
        workspace.mkdir()
        task_file = tmp_path / "t.yaml"
        task_file.write_text("task_id: t\n", encoding="utf-8")

        await regrade_in_place(
            task=_docker_task(),
            prior=_result(),
            workspace=workspace,
            run_dir=tmp_path / "row",
            task_file=task_file,
            source_yaml="",
            variant_id="v",
        )

        assert [name for name, _ in events] == ["CoderEval.Task.End"]
        assert events[0][1]["Driver"] == "docker"
