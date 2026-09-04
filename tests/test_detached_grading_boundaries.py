"""The guards that decide WHERE and WHETHER a detached grade runs.

Three boundaries, each shipped without a behavioural test:

* the docker ``grade`` boundary — the only thing standing between
  ``execute --driver docker`` against a stale image and a run that silently
  publishes real verdicts;
* ``grading_sandbox_config`` — which decides whether a container task's criteria
  may run on the grading host at all;
* the crash-recovery arm — the one a REAL grading failure takes, which is not the
  one the existing test exercises (see ``TestGradingCrashLeavesTheRowRegradeable``).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from coder_eval.cli import app
from coder_eval.models import (
    AgentKind,
    EvaluationResult,
    FileExistsCriterion,
    FinalStatus,
    ResolvedTask,
    SandboxConfig,
    TaskDefinition,
    parse_agent_config,
)
from coder_eval.orchestration.regrade import (
    RegradeError,
    grading_sandbox_config,
    restore_pre_grade_record,
    stamp_host_grading,
)
from coder_eval.path_utils import PRE_GRADE_JSON_FILENAME, TASK_JSON_FILENAME


runner = CliRunner()


def _task(driver: str = "tempdir") -> TaskDefinition:
    return TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(driver=driver),  # type: ignore[arg-type]
        success_criteria=[FileExistsCriterion(path="x.txt", description="x")],
    )


def _result(status: FinalStatus = FinalStatus.NOT_GRADED) -> EvaluationResult:
    return EvaluationResult(
        task_id="t",
        task_description="d",
        variant_id="v",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime(2026, 1, 1),
        final_status=status,
        iteration_count=0,
    )


# --------------------------------------------------------------------------
# grading_sandbox_config: a docker task may not be graded on the host by default
# --------------------------------------------------------------------------


class TestGradingSandboxConfig:
    def test_a_docker_task_is_refused_by_default(self) -> None:
        """Grading cannot start a container, and grading on the host runs the
        task's criteria against a filesystem without the container's paths or
        toolchain — scoring FAILURE for a trajectory `run` scored 1.0, and
        executing its shell unsandboxed here."""
        with pytest.raises(RegradeError) as exc:
            grading_sandbox_config(_task("docker"))
        assert "--allow-host-grading" in str(exc.value)

    def test_the_opt_in_downgrades_to_tempdir(self) -> None:
        cfg = grading_sandbox_config(_task("docker"), allow_host_grading=True)
        assert cfg.driver == "tempdir"

    def test_a_non_docker_task_is_carried_through_unchanged(self) -> None:
        """No rewrite at all on the ordinary path — the config the run used IS
        the config the grade uses."""
        task = _task("tempdir")
        cfg = grading_sandbox_config(task)
        assert cfg.driver == "tempdir"
        assert cfg == task.sandbox

    def test_a_host_graded_docker_row_is_stamped(self) -> None:
        """A console warning does not travel with task.json into run.json, the
        reports or the evalboard. The row must carry the caveat itself, or it is
        silently comparable with a container-graded one."""
        result = _result()
        stamp_host_grading(result, _task("docker"))
        assert result.environment_info["graded_on_host"] is True

    def test_a_normal_row_carries_no_stamp(self) -> None:
        result = _result()
        stamp_host_grading(result, _task("tempdir"))
        assert "graded_on_host" not in result.environment_info


# --------------------------------------------------------------------------
# The docker `grade` boundary
# --------------------------------------------------------------------------


def _docker_runner(*, grade: bool, tmp_path: Path):
    from coder_eval.isolation.docker_runner import DockerRunner

    rt = ResolvedTask(
        task=_task("docker"),
        task_file=tmp_path / "t.yaml",
        run_dir=tmp_path / "run",
        variant_id="default",
        original_task_id="t",
    )
    return DockerRunner(rt, grade=grade)


class TestDockerGradeBoundary:
    """`grade` crosses the container boundary only through context.json, so an
    image that predates `execute` ignores the key and grades anyway."""

    def test_a_graded_verdict_from_an_execute_run_is_refused(self, tmp_path: Path) -> None:
        from coder_eval.isolation.docker_runner import DockerRunError

        runner_ = _docker_runner(grade=False, tmp_path=tmp_path)
        with pytest.raises(DockerRunError, match="predates `execute`"):
            runner_._assert_grade_honored(_result(FinalStatus.SUCCESS))

    def test_an_ungraded_row_is_accepted(self, tmp_path: Path) -> None:
        _docker_runner(grade=False, tmp_path=tmp_path)._assert_grade_honored(_result())

    def test_an_execution_fact_is_exempt(self, tmp_path: Path) -> None:
        """TIMEOUT / ERROR describe the agent phase, not grading. `execute`
        reports them exactly as `run` does, so they are not evidence the image
        graded anything."""
        for status in (FinalStatus.TIMEOUT, FinalStatus.ERROR, FinalStatus.BUILD_FAILED):
            _docker_runner(grade=False, tmp_path=tmp_path)._assert_grade_honored(_result(status))

    def test_a_graded_run_short_circuits(self, tmp_path: Path) -> None:
        _docker_runner(grade=True, tmp_path=tmp_path)._assert_grade_honored(_result(FinalStatus.SUCCESS))


class TestInContainerGradeCoercion:
    """The container side of the same boundary."""

    @staticmethod
    def _run_with_context(tmp_path: Path, grade: object):
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        # Only the keys read BEFORE the grade coercion need real values; the
        # command must refuse before it ever builds an Orchestrator.
        context = {"variant_id": "default", "source_yaml": "task_id: t\n", "grade": grade}
        (input_dir / "context.json").write_text(json.dumps(context), encoding="utf-8")
        (input_dir / "task.yaml").write_text("task_id: t\n", encoding="utf-8")
        return runner.invoke(
            app,
            ["_run-task-internal", "--input", str(input_dir), "--output", str(tmp_path / "out")],
        )

    def test_invoking_the_command_here_arms_no_process_lethal_watchdog(self, tmp_path: Path) -> None:
        """The command's heartbeat watchdog reaps an ORPHANED CONTAINER by calling
        `os._exit(137)` on itself. This suite invokes the command in-process, so an
        unconditionally-armed thread exits the pytest WORKER instead — 40s later
        (20s grace + 20s stale), inside whatever unrelated test that worker has
        moved on to. It shipped that way: it killed a different test on each run
        and on each platform, with no traceback, and the dead worker's lost
        coverage data then failed the gate as `65.13 < 80.00`.

        Asserted on the live thread list rather than by patching `threading`, so
        the guard is proven at the only place that matters — whether a thread now
        exists in this process.
        """
        import threading

        before = {t.name for t in threading.enumerate()}
        self._run_with_context(tmp_path, True)
        leaked = [t for t in threading.enumerate() if t.name not in before and t.daemon]

        assert not leaked, f"the command armed a process-lethal daemon thread outside a container: {leaked}"

    def test_a_non_boolean_grade_is_a_hard_error(self, tmp_path: Path) -> None:
        """A hand-edited or older-format `"grade": "false"` is a truthy str typed
        as bool, which would silently grade a run that asked not to be."""
        result = self._run_with_context(tmp_path, "false")
        assert result.exit_code == 2
        assert "must be a boolean" in result.output

    # The in-container default is asserted BEHAVIOURALLY by
    # `TestGradePlumbedIntoTheContainerOrchestrator::test_an_absent_key_still_grades`.
    # It used to be a `assert 'context.get("grade", True)' in source` grep, which
    # is the same static check that already failed here once: it passes happily
    # while the line it describes is never executed.


# --------------------------------------------------------------------------
# The crash-recovery arm
# --------------------------------------------------------------------------


class TestGradingCrashLeavesTheRowRegradeable:
    """``Orchestrator.run()`` converts an internal failure into a populated
    ``FinalStatus.ERROR`` result rather than raising, so a REAL grading crash
    takes the ``else:`` arm — not the ``except`` arm the pre-existing test
    exercises by patching ``regrade_in_place`` to raise.

    Both arms must leave ``task.json`` ungraded. ERROR is "complete" for both
    commands, so an ERROR row written over a NOT_GRADED one can never be graded
    again without hand-restoring ``task.execute.json``.
    """

    @staticmethod
    def _run_dir_with_backup(tmp_path: Path) -> Path:
        run_dir = tmp_path / "00"
        run_dir.mkdir(parents=True)
        ungraded = _result().model_dump_json(indent=2)
        (run_dir / PRE_GRADE_JSON_FILENAME).write_text(ungraded, encoding="utf-8")
        # What _finalize_result already wrote before the caller saw the ERROR.
        (run_dir / TASK_JSON_FILENAME).write_text(
            _result(FinalStatus.ERROR).model_dump_json(indent=2), encoding="utf-8"
        )
        return run_dir

    def test_restore_puts_the_ungraded_record_back(self, tmp_path: Path) -> None:
        run_dir = self._run_dir_with_backup(tmp_path)
        assert restore_pre_grade_record(run_dir) is True
        on_disk = EvaluationResult.model_validate_json((run_dir / TASK_JSON_FILENAME).read_text(encoding="utf-8"))
        assert on_disk.final_status is FinalStatus.NOT_GRADED

    def test_restore_is_a_no_op_when_nothing_was_overwritten(self, tmp_path: Path) -> None:
        """The grade wrote into a fresh run dir, so the original is untouched."""
        run_dir = tmp_path / "00"
        run_dir.mkdir(parents=True)
        text = _result().model_dump_json(indent=2)
        (run_dir / PRE_GRADE_JSON_FILENAME).write_text(text, encoding="utf-8")
        (run_dir / TASK_JSON_FILENAME).write_text(text, encoding="utf-8")
        assert restore_pre_grade_record(run_dir) is False

    def test_restore_refuses_to_write_through_a_symlink(self, tmp_path: Path) -> None:
        run_dir = self._run_dir_with_backup(tmp_path)
        victim = tmp_path / "victim.json"
        victim.write_text("keep me", encoding="utf-8")
        (run_dir / TASK_JSON_FILENAME).unlink()
        (run_dir / TASK_JSON_FILENAME).symlink_to(victim)

        assert restore_pre_grade_record(run_dir) is False
        assert victim.read_text(encoding="utf-8") == "keep me"

    async def test_the_resume_error_arm_restores_and_folds_back_ungraded(self, tmp_path: Path) -> None:
        """The arm a real grading crash takes: `regrade_in_place` RETURNS an
        ERROR result instead of raising."""
        from coder_eval.cli.run_command import _grade_resumed_tasks

        # task.json starts UNGRADED, as `execute` left it. The ERROR lands on
        # disk during the grade, exactly as _finalize_result writes it before
        # returning — which is the whole reason the in-memory fix is not enough.
        run_dir = tmp_path / "00"
        run_dir.mkdir(parents=True)
        (run_dir / TASK_JSON_FILENAME).write_text(_result().model_dump_json(indent=2), encoding="utf-8")
        rt = ResolvedTask(
            task=_task(),
            task_file=tmp_path / "t.yaml",
            run_dir=run_dir,
            variant_id="v",
            original_task_id="t",
        )

        async def _crash(**_kwargs) -> EvaluationResult:
            errored = _result(FinalStatus.ERROR)
            (run_dir / TASK_JSON_FILENAME).write_text(errored.model_dump_json(indent=2), encoding="utf-8")
            return errored

        with (
            patch("coder_eval.orchestration.regrade.default_workspace", return_value=tmp_path),
            patch("coder_eval.orchestration.regrade.regrade_in_place", new=_crash),
        ):
            graded = await _grade_resumed_tasks([rt])

        assert len(graded) == 1
        folded = graded[0][1].result
        assert folded.final_status is FinalStatus.NOT_GRADED
        assert "Grading errored during --resume" in (folded.error_message or "")
        # And the on-disk row too — fixing only the in-memory result leaves
        # run.json disagreeing with task.json, and task.json is what a later
        # --resume reads.
        on_disk = EvaluationResult.model_validate_json((run_dir / TASK_JSON_FILENAME).read_text(encoding="utf-8"))
        assert on_disk.final_status is FinalStatus.NOT_GRADED

    async def test_an_unreadable_row_still_appears_as_ungraded(self, tmp_path: Path) -> None:
        """Dropping it removed it from run.json AND from `tasks_not_graded`,
        which is the counter the exit gate reads — so a resume whose rows were
        all unreadable reported success."""
        from coder_eval.cli.run_command import _grade_resumed_tasks

        run_dir = tmp_path / "00"
        run_dir.mkdir(parents=True)
        (run_dir / TASK_JSON_FILENAME).write_text("{not json", encoding="utf-8")
        rt = ResolvedTask(
            task=_task(),
            task_file=tmp_path / "t.yaml",
            run_dir=run_dir,
            variant_id="v",
            original_task_id="t",
        )

        graded = await _grade_resumed_tasks([rt])

        assert len(graded) == 1
        assert graded[0][1].result.final_status is FinalStatus.NOT_GRADED
        assert "could not be read" in (graded[0][1].result.error_message or "")


# --------------------------------------------------------------------------
# write_text_atomic hardening
# --------------------------------------------------------------------------


class TestAtomicWriteIsNotAnOverwritePrimitive:
    """``evaluate``'s write-back guards its DESTINATION against a symlink, but the
    truncation happens through the temp name — so a pre-planted temp symlink
    bypassed the guard entirely. The name is now unpredictable AND ``O_NOFOLLOW``,
    so both halves are closed."""

    def test_a_symlink_at_the_temp_name_is_never_followed(self, tmp_path: Path) -> None:
        """Pinned by pinning the random half, since a real attacker cannot guess
        it — the point is that O_NOFOLLOW still refuses even if they could."""
        from coder_eval import path_utils

        victim = tmp_path / "victim"
        victim.write_text("keep me", encoding="utf-8")
        target = tmp_path / "task.json"
        planted = tmp_path / f"task.json.{os.getpid()}.deadbeef.tmp"
        planted.symlink_to(victim)

        with (
            patch.object(path_utils.secrets, "token_hex", return_value="deadbeef"),
            pytest.raises(OSError),
        ):
            path_utils.write_text_atomic(target, "attacker content")
        assert victim.read_text(encoding="utf-8") == "keep me"

    def test_a_stale_temp_file_does_not_wedge_the_write(self, tmp_path: Path) -> None:
        """The regression that mattered most. Under a FIXED temp name, `O_EXCL`
        turned a leftover from a SIGKILL into a permanent refusal to persist the
        record — so the row reported ERROR, `--resume` saw no task.json, re-ran
        the task into the same run dir, and hit the same file again, re-paying
        for the agent on every pass."""
        from coder_eval.path_utils import write_text_atomic

        target = tmp_path / "task.json"
        (tmp_path / "task.json.tmp").write_text("leftover from a hard kill", encoding="utf-8")

        write_text_atomic(target, "hello")

        assert target.read_text(encoding="utf-8") == "hello"

    def test_the_record_is_readable_by_other_uids(self, tmp_path: Path) -> None:
        """Under `driver: docker` the in-container orchestrator writes this file
        as root straight into the bind-mounted host run dir, and the host reads it
        back as the invoking uid through an UNGUARDED `read_text`. Creating it
        0600 made that raise PermissionError for every docker-driver task on
        Linux — invisible on macOS, where Docker Desktop remaps ownership."""
        from coder_eval.path_utils import write_text_atomic

        target = tmp_path / "task.json"
        write_text_atomic(target, "hello")

        mode = target.stat().st_mode & 0o777
        assert mode & 0o044, f"task.json is not group/other-readable: {mode:#o}"

    def test_an_ordinary_write_still_works(self, tmp_path: Path) -> None:
        from coder_eval.path_utils import write_text_atomic

        target = tmp_path / "task.json"
        write_text_atomic(target, "hello")
        assert target.read_text(encoding="utf-8") == "hello"
        assert not list(tmp_path.glob("*.tmp"))

    def test_a_failed_write_leaves_no_temp_file(self, tmp_path: Path) -> None:
        from coder_eval.path_utils import write_text_atomic

        target = tmp_path / "task.json"
        with patch("os.replace", side_effect=OSError("boom")), pytest.raises(OSError):
            write_text_atomic(target, "hello")
        assert not list(tmp_path.glob("*.tmp"))


# --------------------------------------------------------------------------
# default_workspace traversal
# --------------------------------------------------------------------------


def test_a_task_id_that_escapes_the_artifacts_tree_is_refused(tmp_path: Path) -> None:
    """`task_id` is an unvalidated string out of the run's own task.json, and
    `artifacts / "../../.."` joins to a real directory that `is_dir()` confirms.
    Every run_command criterion would then execute with that as its cwd. The
    sibling `sandbox_path` branch was containment-checked; this one was not."""
    from coder_eval.orchestration.regrade import default_workspace

    (tmp_path / "run" / "artifacts").mkdir(parents=True)
    (tmp_path / "outside").mkdir()

    prior = _result()
    prior.task_id = "../../outside"

    with pytest.raises(RegradeError, match="resolves outside"):
        default_workspace(tmp_path / "run", prior)


# --------------------------------------------------------------------------
# The reference digest
# --------------------------------------------------------------------------


def test_the_reference_digest_is_computed_over_a_staged_copy(tmp_path: Path) -> None:
    """The recorded digest is taken over the per-run STAGED copy, which strips
    `.git`. Digesting the raw source instead compares two differently-filtered
    trees, so any reference that is a git checkout — the case the ignore list
    exists for — reports a permanent false mismatch and un-grades the row.
    """
    from coder_eval.orchestration.evaluation import stage_reference_dir
    from coder_eval.orchestration.regrade import _staged_digest
    from coder_eval.path_utils import digest_tree

    source = tmp_path / "reference"
    (source / ".git").mkdir(parents=True)
    (source / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (source / "solution.py").write_text("print('hi')\n", encoding="utf-8")

    staged = stage_reference_dir(source, tmp_path / "staged")
    recorded = digest_tree(staged)  # exactly what Orchestrator._stage_reference records

    assert _staged_digest(source) == recorded
    # And the naive comparison this replaced would have failed.
    assert digest_tree(source) != recorded


def test_a_reference_edited_since_the_run_is_still_caught(tmp_path: Path) -> None:
    """The guard must not become permissive: only the FILTER changed, not what
    counts as a change."""
    from coder_eval.orchestration.regrade import _staged_digest

    source = tmp_path / "reference"
    source.mkdir()
    (source / "solution.py").write_text("print('hi')\n", encoding="utf-8")
    before = _staged_digest(source)

    (source / "solution.py").write_text("print('tampered')\n", encoding="utf-8")
    assert _staged_digest(source) != before


def test_a_reference_that_vanished_is_refused_not_skipped(tmp_path: Path) -> None:
    """A missing answer key must raise, not return silently — grading against
    nothing produces an ordinary-looking score."""
    from coder_eval.orchestration.regrade import verify_reference_unchanged

    task = _task()
    task.reference = MagicMock()
    prior = _result()
    prior.environment_info["reference_digest"] = "deadbeef"

    with (
        patch("coder_eval.orchestration.evaluation.resolve_reference_dir", return_value=tmp_path / "gone"),
        pytest.raises(RegradeError, match="is gone"),
    ):
        verify_reference_unchanged(prior, task, tmp_path / "t.yaml")


# --------------------------------------------------------------------------
# The recorded-config gate must cover PROVISIONING, not just criteria
# --------------------------------------------------------------------------


class TestRecordedProvisioningIsGatedToo:
    """`grading_sandbox_config` carries the recorded `sandbox` block through
    untouched, and the `--copy` branch then calls `Sandbox.setup` — which reaches
    `uv pip install <recorded packages>`, `npm install <recorded packages>` and
    `git clone <recorded url>`. A package name is arbitrary code at install time.

    The gate walked only `success_criteria` + hooks, so a shared run directory
    whose criteria were all `file_exists` passed it and still ran installers.
    """

    @staticmethod
    def _task_with(**sandbox_kwargs) -> TaskDefinition:
        return TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="p",
            agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
            sandbox=SandboxConfig(**sandbox_kwargs),
            success_criteria=[FileExistsCriterion(path="x.txt", description="x")],
        )

    def test_python_packages_are_named(self) -> None:
        from coder_eval.models import PythonEnvConfig
        from coder_eval.orchestration.regrade import embedded_commands

        task = self._task_with(python=PythonEnvConfig(env_packages=["attacker-pkg"]))
        assert any("attacker-pkg" in c for c in embedded_commands(task))

    def test_node_packages_are_named(self) -> None:
        from coder_eval.models import NodeEnvConfig
        from coder_eval.orchestration.regrade import embedded_commands

        task = self._task_with(node=NodeEnvConfig(env_packages=["evil-npm"]))
        assert any("evil-npm" in c for c in embedded_commands(task))

    def test_a_repo_source_url_is_named(self) -> None:
        from coder_eval.models import RepoSource
        from coder_eval.orchestration.regrade import embedded_commands

        task = self._task_with(template_sources=[RepoSource(url="https://evil.example/repo.git")])
        assert any("evil.example" in c for c in embedded_commands(task))

    def test_provisioning_alone_triggers_the_refusal(self, tmp_path: Path) -> None:
        """The exact repro: every criterion is a file_exists, so the old scan
        found nothing and the installers ran with no opt-in."""
        from coder_eval.models import PythonEnvConfig
        from coder_eval.orchestration.regrade import check_embedded_commands

        task = self._task_with(python=PythonEnvConfig(env_packages=["attacker-pkg"]))
        with pytest.raises(RegradeError, match="--allow-recorded-commands"):
            check_embedded_commands(task, tmp_path, allow_recorded_commands=False)

    def test_the_in_place_path_is_exempt(self, tmp_path: Path) -> None:
        """`adopt()` installs nothing, so in place these are not capabilities the
        run dir has — and refusing there would break the headline flow."""
        from coder_eval.models import PythonEnvConfig
        from coder_eval.orchestration.regrade import check_embedded_commands

        task = self._task_with(python=PythonEnvConfig(env_packages=["attacker-pkg"]))
        check_embedded_commands(task, tmp_path, allow_recorded_commands=False, include_setup_phase=False)

    def test_an_llm_judge_is_named(self) -> None:
        """No shell, but it spends the grader's model budget and ships the graded
        artifacts to a provider the recorded config chose."""
        from coder_eval.models import LLMJudgeCriterion
        from coder_eval.orchestration.regrade import embedded_commands

        task = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="p",
            agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
            success_criteria=[LLMJudgeCriterion(description="x", prompt="grade it")],
        )
        assert any("llm_judge" in c for c in embedded_commands(task))


class TestGradePlumbedIntoTheContainerOrchestrator:
    """The `grade` value reaching the in-container Orchestrator was asserted only
    by grepping the module's own source text, which passes while the line it
    describes is never executed — deleting `grade=grade` left every test green."""

    @staticmethod
    def _invoke(tmp_path: Path, context: dict) -> object:
        captured: dict[str, object] = {}

        class _FakeOrchestrator:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def run(self):
                return _result()

        input_dir = tmp_path / "input"
        input_dir.mkdir()
        task_yaml = (
            "task_id: t\ndescription: d\nagent:\n  type: none\n"
            "success_criteria:\n  - type: file_exists\n    path: p.txt\n    description: x\n"
        )
        (input_dir / "task.yaml").write_text(task_yaml, encoding="utf-8")
        (input_dir / "context.json").write_text(
            json.dumps({"variant_id": "default", "source_yaml": task_yaml, **context}), encoding="utf-8"
        )
        with patch("coder_eval.orchestrator.Orchestrator", _FakeOrchestrator):
            runner.invoke(
                app,
                ["_run-task-internal", "--input", str(input_dir), "--output", str(tmp_path / "out")],
            )
        return captured.get("grade")

    def test_execute_forwards_grade_false(self, tmp_path: Path) -> None:
        assert self._invoke(tmp_path, {"grade": False}) is False

    def test_an_absent_key_still_grades(self, tmp_path: Path) -> None:
        """A host predating `execute` writes no key; the container must keep its
        original behaviour rather than silently withholding verdicts."""
        assert self._invoke(tmp_path, {}) is True


class TestContainerContextIsValidated:
    """`json.loads` returns Any, so an annotation at this boundary reads like a
    guarantee and enforces nothing."""

    @staticmethod
    def _run(tmp_path: Path, context: dict):
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        (input_dir / "task.yaml").write_text("task_id: t\n", encoding="utf-8")
        (input_dir / "context.json").write_text(json.dumps(context), encoding="utf-8")
        return runner.invoke(app, ["_run-task-internal", "--input", str(input_dir), "--output", str(tmp_path / "out")])

    def test_a_non_string_variant_id_is_refused(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, {"variant_id": 7, "source_yaml": "task_id: t\n"})
        assert result.exit_code == 2
        assert "variant_id" in result.output

    def test_a_string_replicate_index_is_refused(self, tmp_path: Path) -> None:
        """`"00"` would reach build_task_run_dir typed as int."""
        result = self._run(tmp_path, {"variant_id": "default", "replicate_index": "00", "source_yaml": "task_id: t\n"})
        assert result.exit_code == 2
        assert "replicate_index" in result.output


class TestCriterionPathsCannotEscapeTheSandbox:
    """`Path('/tmp/sandbox') / '/etc/passwd'` is `/etc/passwd` — pathlib discards
    the prefix on an absolute right operand — and criterion paths were the one
    task-authored path in sandbox.py that skipped `_resolve_within_sandbox`.

    Defensible while a task YAML was operator-supplied; not once
    `evaluate <run_dir>` began rebuilding the criteria list from a shareable run
    directory, which turns `file_contains` / `file_check` / `file_matches_regex`
    into a pass-fail oracle over any file the grading user can read.
    """

    @staticmethod
    def _sandbox(tmp_path: Path):
        from coder_eval.sandbox import Sandbox

        sandbox = Sandbox(SandboxConfig(driver="tempdir"), task_id="t")
        work = tmp_path / "work"
        work.mkdir()
        sandbox.sandbox_dir = work
        return sandbox, work

    def test_an_absolute_path_resolves_to_nothing(self, tmp_path: Path) -> None:
        sandbox, _ = self._sandbox(tmp_path)
        secret = tmp_path / "secret.txt"
        secret.write_text("token", encoding="utf-8")

        assert sandbox.resolve_files(str(secret)) == []

    def test_a_dotdot_traversal_resolves_to_nothing(self, tmp_path: Path) -> None:
        sandbox, _ = self._sandbox(tmp_path)
        (tmp_path / "secret.txt").write_text("token", encoding="utf-8")

        assert sandbox.resolve_files("../secret.txt") == []

    def test_a_glob_cannot_escape_either(self, tmp_path: Path) -> None:
        sandbox, _ = self._sandbox(tmp_path)
        (tmp_path / "secret.txt").write_text("token", encoding="utf-8")

        assert sandbox.resolve_files("../*.txt") == []

    def test_an_ordinary_in_sandbox_path_still_resolves(self, tmp_path: Path) -> None:
        """The control — the guard must not break normal grading."""
        sandbox, work = self._sandbox(tmp_path)
        (work / "proof.txt").write_text("ok", encoding="utf-8")

        assert sandbox.resolve_files("proof.txt") == [work / "proof.txt"]
        assert sandbox.resolve_files("*.txt") == [work / "proof.txt"]
