"""GRADE-OUTSIDE: the docker host re-grade over copied-out artifacts.

Under ``--driver docker`` the container runs the AGENT ONLY; the host grades the
copied-out artifacts afterwards via ``regrade_on_host`` (the evaluate-only
re-grade seam). These tests drive that path with NO docker daemon and NO LLM:
they build a real tempdir "artifacts" dir + a host task dir holding the grader,
and assert the host grade matches a direct evaluation — proving TASK_DIR resolves
to the real host task dir, not the agent's workspace.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

from coder_eval.isolation.docker_runner import REGRADE_STATUS_ALLOWLIST, regrade_on_host
from coder_eval.models import (
    EvaluationResult,
    FinalStatus,
    ResolvedTask,
    SandboxConfig,
    TaskDefinition,
)


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="docker driver is POSIX-only")


def _make_rt(tmp_path: Path, task: TaskDefinition) -> ResolvedTask:
    task_dir = tmp_path / "taskdir"
    task_dir.mkdir(exist_ok=True)
    task_file = task_dir / "task.yaml"
    task_file.write_text("x", encoding="utf-8")
    return ResolvedTask(
        task=task,
        task_file=task_file,
        run_dir=tmp_path / "run",
        variant_id="v",
        source_yaml="raw",
    )


def _docker_result(sandbox_path: Path, status: FinalStatus = FinalStatus.FAILURE) -> EvaluationResult:
    return EvaluationResult(
        task_id="t",
        task_description="d",
        variant_id="v",
        agent_type="claude-code",
        started_at=datetime.now(),
        final_status=status,
        # A non-trivial trajectory marker (the container ran the agent for 3
        # turns) that the host re-grade must PRESERVE on disk — the container
        # holds the trajectory, the host grade has none.
        iteration_count=3,
        weighted_score=0.0,  # container graded stripped [] criteria → vacuous
        environment_info={},
        sandbox_path=str(sandbox_path),
        success_criteria_results=[],  # container produced no real grades
    )


async def test_regrade_works_with_docker_driver_task(tmp_path):
    """Regression: in production ``rt.task.sandbox.driver == 'docker'`` (that is the
    whole point of the run). ``regrade_on_host`` must switch the driver off 'docker'
    for the in-process host grade — otherwise ``Sandbox.setup()`` raises
    "Sandbox.setup() called with driver='docker' -- must be dispatched via
    DockerRunner", the re-grade crashes, and (via the fail-safe) EVERY docker task is
    stamped ERROR instead of graded. The prior tests used the default (tempdir)
    SandboxConfig and never exercised this, so grade-outside was non-functional in
    production while the suite was green."""
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox=SandboxConfig(driver="docker"),  # the production reality
        agent={"type": "claude-code"},
        success_criteria=[{"type": "file_exists", "description": "c", "path": "app.py"}],
    )
    rt = _make_rt(tmp_path, task)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "app.py").write_text("print('hi')", encoding="utf-8")

    result = _docker_result(artifacts, status=FinalStatus.FAILURE)
    regraded = await regrade_on_host(result, rt)

    # Must actually grade (not crash → ERROR): the file_exists criterion passes.
    assert regraded.final_status == FinalStatus.SUCCESS
    assert len(regraded.success_criteria_results) == 1
    assert regraded.success_criteria_results[0].score == pytest.approx(1.0)


async def test_regrade_does_not_re_materialize_over_agent_artifacts(tmp_path):
    """Regression: the re-grade wraps the copied-out artifacts via
    Sandbox.setup(regrade=True) and must NOT re-materialize templates/starter files
    (which would overwrite the agent's edits with pristine content and corrupt the
    grade). Every other regrade test uses a bare SandboxConfig with no template, so a
    regression dropping the `if regrade:` early-return would pass 100% of them — this
    is the guard that a template is NOT re-applied over agent output."""
    from coder_eval.models import StarterFile, StarterFilesSource

    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox=SandboxConfig(
            driver="docker",
            template_sources=[StarterFilesSource(files=[StarterFile(path="app.py", content="PRISTINE_STARTER")])],
        ),
        agent={"type": "claude-code"},
        # file_contains gates on the AGENT's content, so a re-materialize (→ PRISTINE) fails it.
        success_criteria=[
            {
                "type": "file_contains",
                "description": "agent edit survived",
                "path": "app.py",
                "includes": ["AGENT_EDITED"],
            }
        ],
    )
    rt = _make_rt(tmp_path, task)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    # The agent's produced content — must survive the re-grade untouched.
    (artifacts / "app.py").write_text("AGENT_EDITED", encoding="utf-8")

    result = _docker_result(artifacts, status=FinalStatus.FAILURE)
    regraded = await regrade_on_host(result, rt)

    # (1) the agent's file on disk is untouched (not clobbered to PRISTINE_STARTER)
    assert (artifacts / "app.py").read_text(encoding="utf-8") == "AGENT_EDITED"
    # (2) the criterion, graded against the agent content, passes
    assert regraded.success_criteria_results[0].score == pytest.approx(1.0)


async def test_regrade_requires_target_dir(tmp_path):
    """Sandbox.setup(regrade=True) with no target_dir is a hard error (nothing to wrap)."""
    from coder_eval.sandbox import Sandbox

    sb = Sandbox(SandboxConfig(driver="tempdir"), task_id="t", task_dir=tmp_path)
    with pytest.raises(ValueError, match="requires target_dir"):
        sb.setup(target_dir=None, regrade=True)


async def test_regrade_seeds_trajectory_for_skill_triggered(tmp_path):
    """Regression: trajectory-based criteria (skill_triggered / command_executed /
    agent_judge / llm_judge capture_transcript) must grade against the CONTAINER's
    turns. regrade_on_host seeds result.iterations via ``existing_turns``; WITHOUT it
    the host re-grade runs agent-less with an EMPTY trajectory, so skill_triggered
    reports "not triggered" for every docker task — silently zeroing the activation
    metric the whole OSS-models effort measures."""
    from datetime import datetime

    from coder_eval.models import CommandTelemetry, TurnRecord

    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox=SandboxConfig(driver="docker"),
        agent={"type": "claude-code"},
        success_criteria=[
            {"type": "skill_triggered", "description": "engaged foo", "skill_name": "foo", "expected_skill": "foo"}
        ],
    )
    rt = _make_rt(tmp_path, task)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "x.txt").write_text("x", encoding="utf-8")

    result = _docker_result(artifacts, status=FinalStatus.FAILURE)
    # The container's trajectory: the agent engaged skill "foo" via a Skill tool call.
    result.iterations = [
        TurnRecord(
            iteration=1,
            user_input="p",
            agent_output="ok",
            commands=[
                CommandTelemetry(tool_name="Skill", tool_id="s1", timestamp=datetime.now(), parameters={"skill": "foo"})
            ],
        )
    ]

    regraded = await regrade_on_host(result, rt)

    # Trajectory seeded → "foo" observed as engaged → criterion passes (score 1.0).
    # Without the existing_turns seed this is 0.0 (empty trajectory → "not triggered").
    assert len(regraded.success_criteria_results) == 1
    assert regraded.success_criteria_results[0].score == pytest.approx(1.0)


async def test_regrade_grades_copied_out_artifacts(tmp_path):
    """file_exists over the agent's artifacts is re-graded on the host."""
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox=SandboxConfig(driver="docker"),
        agent={"type": "claude-code"},
        success_criteria=[{"type": "file_exists", "description": "c", "path": "app.py"}],
    )
    rt = _make_rt(tmp_path, task)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "app.py").write_text("print('hi')", encoding="utf-8")

    result = _docker_result(artifacts, status=FinalStatus.FAILURE)
    regraded = await regrade_on_host(result, rt)

    assert regraded.final_status == FinalStatus.SUCCESS
    assert len(regraded.success_criteria_results) == 1
    assert regraded.success_criteria_results[0].score == pytest.approx(1.0)
    # The in-memory weighted_score must reflect the host grade, not the
    # container's vacuous 0.0 (telemetry + in-memory reports read this).
    assert regraded.weighted_score == pytest.approx(1.0)
    # The container's trajectory marker survives the merge (host grade has none).
    assert regraded.iteration_count == 3

    # On disk: the merged task.json carries BOTH the trajectory AND the real
    # grade — the host re-grade must NOT clobber rt.run_dir with an
    # empty-trajectory task.json.
    disk = EvaluationResult.model_validate_json((rt.run_dir / "task.json").read_text(encoding="utf-8"))
    assert disk.iteration_count == 3  # trajectory preserved
    assert disk.final_status == FinalStatus.SUCCESS  # real grade
    assert disk.weighted_score == pytest.approx(1.0)
    assert len(disk.success_criteria_results) == 1


async def test_regrade_task_dir_resolves_to_host_grader(tmp_path):
    """A run_command grader reading $TASK_DIR must resolve to the HOST task dir
    (holding the grader), NOT the agent's throwaway artifacts workspace."""
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox=SandboxConfig(),
        agent={"type": "claude-code"},
        success_criteria=[
            {
                "type": "run_command",
                "description": "grader",
                # Reads the host grader script via $TASK_DIR — proves task_dir
                # points at the real host task dir, never the agent workspace.
                "command": 'test "$(cat "$TASK_DIR/expected.txt")" = "$(cat out.txt)"',
                "timeout": 10,
            }
        ],
    )
    rt = _make_rt(tmp_path, task)
    # Host grader material lives in the task dir (never mounted into the agent).
    (rt.task_file.parent / "expected.txt").write_text("MATCH", encoding="utf-8")

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "out.txt").write_text("MATCH", encoding="utf-8")

    result = _docker_result(artifacts, status=FinalStatus.SUCCESS)
    regraded = await regrade_on_host(result, rt)
    assert regraded.final_status == FinalStatus.SUCCESS
    assert regraded.success_criteria_results[0].score == pytest.approx(1.0)

    # Now the agent output does NOT match: the host grade must FAIL.
    (artifacts / "out.txt").write_text("WRONG", encoding="utf-8")
    result2 = _docker_result(artifacts, status=FinalStatus.SUCCESS)
    regraded2 = await regrade_on_host(result2, rt)
    assert regraded2.final_status == FinalStatus.FAILURE


async def test_no_gating_criteria_skips_regrade(tmp_path):
    """A task whose only criterion is weight=0 (non-gating) skips the re-grade."""
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox=SandboxConfig(),
        agent={"type": "claude-code"},
        success_criteria=[{"type": "file_exists", "description": "c", "path": "x", "weight": 0.0}],
    )
    rt = _make_rt(tmp_path, task)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    result = _docker_result(artifacts, status=FinalStatus.SUCCESS)
    out = await regrade_on_host(result, rt)
    # Unchanged: container result stands (empty criteria results, same status object).
    assert out is result
    assert out.success_criteria_results == []


@pytest.mark.parametrize(
    "status",
    [
        FinalStatus.ERROR,
        FinalStatus.BUILD_FAILED,
        FinalStatus.TIMEOUT,
        FinalStatus.TOKEN_BUDGET_EXCEEDED,
        FinalStatus.COST_BUDGET_EXCEEDED,
    ],
)
async def test_terminal_failure_not_regraded(tmp_path, status):
    """A terminal agent-side failure must stand — never re-graded/clobbered."""
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox=SandboxConfig(),
        agent={"type": "claude-code"},
        success_criteria=[{"type": "file_exists", "description": "c", "path": "app.py"}],
    )
    rt = _make_rt(tmp_path, task)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "app.py").write_text("x", encoding="utf-8")  # would PASS if re-graded
    result = _docker_result(artifacts, status=status)
    out = await regrade_on_host(result, rt)
    assert out is result
    assert out.final_status == status  # untouched
    assert out.success_criteria_results == []


def test_regrade_allowlist_is_exactly_the_gradable_statuses():
    assert (
        frozenset({FinalStatus.SUCCESS, FinalStatus.FAILURE, FinalStatus.MAX_TURNS_EXHAUSTED})
        == REGRADE_STATUS_ALLOWLIST
    )


async def test_regrade_translates_container_sandbox_path_to_host(tmp_path):
    """CRITICAL PATH: the in-container orchestrator records a CONTAINER-absolute
    sandbox_path (/work/output/artifacts/<id>) because the /work/output mount is
    not path-symmetric. regrade_on_host must re-root it onto the real host
    rt.run_dir where the artifacts physically live — NOT wrap the non-existent
    container path (which would grade an empty auto-created dir and flip a passing
    run to FAILURE)."""
    from coder_eval.models import CONTAINER_OUTPUT_DIR

    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox=SandboxConfig(),
        agent={"type": "claude-code"},
        success_criteria=[{"type": "file_exists", "description": "c", "path": "app.py"}],
    )
    rt = _make_rt(tmp_path, task)
    # Artifacts physically live under the HOST rt.run_dir (what /work/output binds to).
    host_artifacts = rt.run_dir / "artifacts" / "t"
    host_artifacts.mkdir(parents=True)
    (host_artifacts / "app.py").write_text("print('hi')", encoding="utf-8")

    # The container-emitted sandbox_path is the CONTAINER path, not the host path.
    container_sandbox_path = f"{CONTAINER_OUTPUT_DIR}/artifacts/t"
    result = _docker_result(Path(container_sandbox_path), status=FinalStatus.FAILURE)
    result.sandbox_path = container_sandbox_path  # override _docker_result's str()

    regraded = await regrade_on_host(result, rt)
    # The re-grade found the REAL host artifacts (app.py exists) → SUCCESS.
    assert regraded.final_status == FinalStatus.SUCCESS
    assert regraded.weighted_score == pytest.approx(1.0)
    # The bogus container path was NOT created on the host as an empty dir.
    assert not Path(container_sandbox_path).exists()


def _write_container_task_json(rt: ResolvedTask, result: EvaluationResult) -> Path:
    """Simulate the container writing its authoritative-looking task.json to
    rt.run_dir BEFORE the host re-grade runs. Because the container graded the
    stripped `[]` criteria, this on-disk file reads whatever status it carries
    (SUCCESS if the container recorded SUCCESS)."""
    rt.run_dir.mkdir(parents=True, exist_ok=True)
    target = rt.run_dir / "task.json"
    target.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return target


async def test_regrade_exception_never_leaves_false_success_on_disk(tmp_path, monkeypatch):
    """HIGH-1: if the re-grade body raises, the on-disk task.json must NOT keep the
    container's vacuous `[]`-criteria SUCCESS. Disk status must equal the in-memory
    ERROR the batch layer records — never a false SUCCESS."""
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox=SandboxConfig(),
        agent={"type": "claude-code"},
        success_criteria=[{"type": "file_exists", "description": "c", "path": "app.py"}],
    )
    rt = _make_rt(tmp_path, task)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "app.py").write_text("x", encoding="utf-8")

    # The container wrote an authoritative-looking SUCCESS task.json first (its
    # stripped [] criteria all pass vacuously).
    container_result = _docker_result(artifacts, status=FinalStatus.SUCCESS)
    disk_path = _write_container_task_json(rt, container_result)

    # Force the re-grade body to raise (mirrors a grader timeout / judge blip /
    # OSError inside sandbox.setup or orchestrator.run). Sandbox is late-imported
    # inside regrade_on_host, so patch it at its definition module.
    import coder_eval.sandbox as sandbox_mod

    def _boom(*_a, **_k):
        raise RuntimeError("grader network blip")

    monkeypatch.setattr(sandbox_mod.Sandbox, "setup", _boom, raising=True)

    result = _docker_result(artifacts, status=FinalStatus.SUCCESS)
    with pytest.raises(RuntimeError, match="grader network blip"):
        await regrade_on_host(result, rt)

    # In-memory result was stamped ERROR (what batch folds into run.json).
    assert result.final_status == FinalStatus.ERROR
    assert result.success_criteria_results == []
    # On disk: the vacuous SUCCESS was overwritten with the same ERROR — no divergence.
    disk = EvaluationResult.model_validate_json(disk_path.read_text(encoding="utf-8"))
    assert disk.final_status == FinalStatus.ERROR
    assert disk.final_status == result.final_status  # disk == memory


async def test_regrade_preserves_max_turns_exhausted_on_failing_grade(tmp_path):
    """LOW-1: a container run that hit the turn cap and then fails the host grade
    keeps MAX_TURNS_EXHAUSTED (the diagnostic), not a generic FAILURE."""
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox=SandboxConfig(),
        agent={"type": "claude-code"},
        success_criteria=[{"type": "file_exists", "description": "c", "path": "app.py"}],
    )
    rt = _make_rt(tmp_path, task)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    # app.py does NOT exist -> host grade fails.
    result = _docker_result(artifacts, status=FinalStatus.MAX_TURNS_EXHAUSTED)
    out = await regrade_on_host(result, rt)
    # Failing grade must preserve the more-specific container status.
    assert out.final_status == FinalStatus.MAX_TURNS_EXHAUSTED

    # But a PASSING host grade still promotes to SUCCESS.
    (artifacts / "app.py").write_text("x", encoding="utf-8")
    result2 = _docker_result(artifacts, status=FinalStatus.MAX_TURNS_EXHAUSTED)
    out2 = await regrade_on_host(result2, rt)
    assert out2.final_status == FinalStatus.SUCCESS


async def test_regrade_skips_pre_post_commands(tmp_path):
    """HIGH-2: the host re-grade must NOT re-run pre_run/post_run (the container
    already ran them). A pre_run that writes a marker must leave NO marker after
    the re-grade — proving the command ran zero times on the host re-grade path."""
    marker = tmp_path / "pre_run_marker"
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox=SandboxConfig(),
        agent={"type": "claude-code"},
        pre_run=[{"command": f"touch {marker}", "fail_on_error": True}],
        success_criteria=[{"type": "file_exists", "description": "c", "path": "app.py"}],
    )
    rt = _make_rt(tmp_path, task)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "app.py").write_text("x", encoding="utf-8")

    result = _docker_result(artifacts, status=FinalStatus.SUCCESS)
    out = await regrade_on_host(result, rt)
    assert out.final_status == FinalStatus.SUCCESS  # grade still ran
    assert not marker.exists(), "pre_run re-ran on the host re-grade path (should be skipped)"


async def test_regrade_missing_artifacts_dir_degrades_to_error(tmp_path):
    """Fail-safe: if the translated artifacts dir doesn't exist, the full criteria
    could not be graded — the container's vacuous `[]`-criteria SUCCESS must NOT
    stand. Degrade to ERROR (and never auto-create the empty grade dir)."""
    from coder_eval.models import CONTAINER_OUTPUT_DIR

    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox=SandboxConfig(),
        agent={"type": "claude-code"},
        success_criteria=[{"type": "file_exists", "description": "c", "path": "app.py"}],
    )
    rt = _make_rt(tmp_path, task)  # rt.run_dir/artifacts/t does NOT exist
    container_sandbox_path = f"{CONTAINER_OUTPUT_DIR}/artifacts/t"
    result = _docker_result(Path(container_sandbox_path), status=FinalStatus.SUCCESS)
    result.sandbox_path = container_sandbox_path
    result.success_criteria_results = []

    out = await regrade_on_host(result, rt)
    assert out is result
    # A task with a real gating criterion that could NOT be graded is an ERROR,
    # not a pass — the vacuous container SUCCESS must never survive.
    assert out.final_status == FinalStatus.ERROR
    assert out.success_criteria_results == []
    assert out.weighted_score == 0.0
    assert out.error_message and "artifacts dir" in out.error_message
    # The translated host path must not have been auto-created.
    assert not (rt.run_dir / "artifacts" / "t").exists()
    # The ERROR must be persisted so disk and the batch layer agree.
    import json

    persisted = json.loads((rt.run_dir / "task.json").read_text(encoding="utf-8"))
    assert persisted["final_status"] == FinalStatus.ERROR.value


async def test_regrade_missing_sandbox_path_degrades_to_error(tmp_path):
    """Fail-safe: a result with no sandbox_path cannot be located for grading. The
    container's vacuous SUCCESS must degrade to ERROR, not stand as a false pass."""
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox=SandboxConfig(),
        agent={"type": "claude-code"},
        success_criteria=[{"type": "file_exists", "description": "c", "path": "app.py"}],
    )
    rt = _make_rt(tmp_path, task)
    result = _docker_result(Path("/work/output/artifacts/t"), status=FinalStatus.SUCCESS)
    result.sandbox_path = ""  # nothing to locate

    out = await regrade_on_host(result, rt)
    assert out is result
    assert out.final_status == FinalStatus.ERROR
    assert out.success_criteria_results == []
    assert out.error_message and "sandbox_path" in out.error_message
