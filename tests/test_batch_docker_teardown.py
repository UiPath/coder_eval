"""Docker grade-outside teardown ordering: host post_run must run even when the
host re-grade raises.

Under ``--driver docker`` the batch layer calls ``regrade_on_host`` then
``run_post_run_on_host`` (cloud teardown). A re-grade exception must NOT skip
teardown — a pre_run-provisioned cloud resource has to be torn down regardless of
whether grading blew up — while the task is still recorded as ERROR. These tests
are daemon-less and model-less: ``DockerRunner.run`` / ``regrade_on_host`` /
``run_post_run_on_host`` are monkeypatched on the ``docker_runner`` module (they
are imported function-local inside ``run_batch.run_single``, so the patched
module attributes are what the call resolves).
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

from coder_eval.models import (
    AgentKind,
    EvaluationResult,
    FinalStatus,
    PostRunCommand,
    ResolvedTask,
    SandboxConfig,
    TaskDefinition,
)
from coder_eval.orchestration.batch import run_batch
from coder_eval.orchestration.config import BatchRunConfig


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="docker driver is POSIX-only")


def _docker_task(tmp_path: Path) -> ResolvedTask:
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox=SandboxConfig(driver="docker"),
        agent={"type": "claude-code"},
        success_criteria=[{"type": "file_exists", "description": "c", "path": "o.txt"}],
        post_run=[PostRunCommand(command="echo teardown")],
    )
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


def _minimal_result() -> EvaluationResult:
    return EvaluationResult(
        task_id="t",
        task_description="d",
        variant_id="v",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status=FinalStatus.SUCCESS,
        iteration_count=1,
        environment_info={},
    )


class _FakeDockerRunner:
    """Stand-in for DockerRunner: its ``run`` returns a minimal result, no daemon."""

    def __init__(self, *args, **kwargs):
        pass

    async def run(self) -> EvaluationResult:
        return _minimal_result()


def _patch_docker(monkeypatch, *, regrade_raises: bool, spy: dict) -> None:
    import coder_eval.isolation.docker_runner as dr

    async def _fake_regrade(result, rt):
        if regrade_raises:
            raise RuntimeError("boom")
        return result

    async def _fake_post_run(result, rt):
        spy["post_run_calls"] = spy.get("post_run_calls", 0) + 1

    monkeypatch.setattr(dr, "DockerRunner", _FakeDockerRunner)
    monkeypatch.setattr(dr, "regrade_on_host", _fake_regrade)
    monkeypatch.setattr(dr, "run_post_run_on_host", _fake_post_run)
    # build_task_event + track_event are also imported function-local; stub the
    # telemetry emit so the test needs no telemetry backend.
    import coder_eval.orchestrator as orch
    import coder_eval.telemetry as tel

    monkeypatch.setattr(orch, "build_task_event", lambda result, driver, variant_id: ("Task.End", {}))
    monkeypatch.setattr(tel, "track_event", lambda name, props: None)


async def test_post_run_runs_even_when_regrade_raises(tmp_path, monkeypatch):
    """regrade_on_host raises → run_post_run_on_host STILL runs once, task is ERROR.

    FAILS on pre-fix code: the raise jumps to the outer except, skipping teardown.
    """
    spy: dict = {}
    _patch_docker(monkeypatch, regrade_raises=True, spy=spy)
    rt = _docker_task(tmp_path)
    config = BatchRunConfig(run_dir=tmp_path / "run", max_parallel=1)

    _summary, results = await run_batch([rt], config)

    assert spy.get("post_run_calls") == 1, "host post_run teardown was skipped on the re-grade error path"
    assert len(results) == 1
    assert results[0].result.final_status == FinalStatus.ERROR, "re-grade failure was not recorded as ERROR"


async def test_post_run_runs_on_successful_regrade(tmp_path, monkeypatch):
    """Happy path: regrade succeeds → teardown runs once, no error is forced."""
    spy: dict = {}
    _patch_docker(monkeypatch, regrade_raises=False, spy=spy)
    rt = _docker_task(tmp_path)
    config = BatchRunConfig(run_dir=tmp_path / "run", max_parallel=1)

    _summary, results = await run_batch([rt], config)

    assert spy.get("post_run_calls") == 1
    assert len(results) == 1
    assert results[0].result.final_status == FinalStatus.SUCCESS
