"""HARNESS-OUTSIDE: docker ``post_run`` teardown runs on the HOST.

Under ``--driver docker`` the container runs the agent + ``pre_run`` only; the
skills-repo ``tests/`` helper scripts a ``post_run`` command invokes are never
mounted into the agent container, so teardown moves to the host after the
container exits (over the copied-out workspace). These tests drive
``run_post_run_on_host`` with NO docker daemon: a real tempdir "artifacts" dir
stands in for the copied-out workspace.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

from coder_eval.isolation.docker_runner import run_post_run_on_host
from coder_eval.models import (
    EvaluationResult,
    FinalStatus,
    PostRunCommand,
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


def _task(post_run: list[PostRunCommand], criteria: list[dict] | None = None) -> TaskDefinition:
    return TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox=SandboxConfig(driver="docker"),
        agent={"type": "claude-code"},
        success_criteria=criteria
        if criteria is not None
        else [{"type": "file_exists", "description": "informational", "path": "x.txt", "weight": 0.0}],
        post_run=post_run,
    )


def _docker_result(sandbox_path: Path | None, status: FinalStatus = FinalStatus.SUCCESS) -> EvaluationResult:
    return EvaluationResult(
        task_id="t",
        task_description="d",
        variant_id="v",
        agent_type="claude-code",
        started_at=datetime.now(),
        final_status=status,
        iteration_count=1,
        sandbox_path=str(sandbox_path) if sandbox_path is not None else None,
        success_criteria_results=[],
    )


async def test_post_run_runs_with_cwd_artifacts_and_populates_results(tmp_path):
    """post_run runs in the copied-out workspace and records its output."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "seed.json").write_text('{"id": 42}', encoding="utf-8")

    task = _task([PostRunCommand(command='python3 -c "import os; print(os.getcwd())"')])
    rt = _make_rt(tmp_path, task)
    result = _docker_result(artifacts)

    await run_post_run_on_host(result, rt)

    assert len(result.post_run_results) == 1
    assert result.post_run_results[0].stdout.strip() == str(artifacts)
    # Re-persisted to task.json.
    persisted = rt.run_dir / "task.json"
    assert persisted.is_file()
    assert '"post_run_results"' in persisted.read_text(encoding="utf-8")


async def test_post_run_sees_copied_out_seed_file(tmp_path):
    """Round-trip: a seed file in the copied-out workspace is visible to teardown."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "seed.json").write_text('{"resource": "abc"}', encoding="utf-8")

    task = _task([PostRunCommand(command="cat seed.json")])
    rt = _make_rt(tmp_path, task)
    result = _docker_result(artifacts)

    await run_post_run_on_host(result, rt)
    assert '"resource": "abc"' in result.post_run_results[0].stdout


async def test_post_run_non_fatal_on_failing_command(tmp_path):
    """A failing post_run command is recorded but never raises / flips status."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    task = _task([PostRunCommand(command="exit 5")])
    rt = _make_rt(tmp_path, task)
    result = _docker_result(artifacts, status=FinalStatus.SUCCESS)

    await run_post_run_on_host(result, rt)  # must not raise

    assert result.final_status == FinalStatus.SUCCESS
    assert result.post_run_results[0].exit_code == 5


async def test_post_run_runs_when_no_gating_criteria(tmp_path):
    """ALWAYS-RUN: teardown runs even for an ungraded task (only non-gating,
    weight-0 criteria), where ``regrade_on_host`` short-circuits and never
    touches post_run."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    task = _task([PostRunCommand(command="echo teardown-ran")])  # default: weight-0 (non-gating)
    assert not any(c.is_gating for c in task.success_criteria)
    rt = _make_rt(tmp_path, task)
    result = _docker_result(artifacts)

    await run_post_run_on_host(result, rt)
    assert result.post_run_results[0].stdout.strip() == "teardown-ran"


async def test_post_run_runs_on_terminal_failure_with_artifacts(tmp_path):
    """ALWAYS-RUN: teardown runs even for a terminal agent-side failure
    (ERROR/TIMEOUT) — cloud resources must be cleaned up regardless of grade —
    as long as an artifacts dir exists."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    task = _task(
        [PostRunCommand(command="echo cleaned-up")],
        criteria=[{"type": "file_exists", "description": "c", "path": "app.py"}],
    )
    rt = _make_rt(tmp_path, task)
    result = _docker_result(artifacts, status=FinalStatus.ERROR)

    await run_post_run_on_host(result, rt)
    assert result.post_run_results[0].stdout.strip() == "cleaned-up"


async def test_post_run_skipped_with_warning_when_no_artifacts_dir(tmp_path, caplog):
    """No sandbox_path → no artifacts dir → skip with a warning, no results."""
    import logging

    task = _task([PostRunCommand(command="echo never")])
    rt = _make_rt(tmp_path, task)
    result = _docker_result(None)  # no sandbox_path

    with caplog.at_level(logging.WARNING, logger="coder_eval.isolation.docker_runner"):
        await run_post_run_on_host(result, rt)

    assert result.post_run_results == []
    assert any("skipping teardown" in r.getMessage() for r in caplog.records)


async def test_post_run_skipped_with_warning_when_artifacts_dir_missing(tmp_path, caplog):
    """sandbox_path points at a nonexistent dir → skip with a warning."""
    import logging

    task = _task([PostRunCommand(command="echo never")])
    rt = _make_rt(tmp_path, task)
    result = _docker_result(tmp_path / "does_not_exist")

    with caplog.at_level(logging.WARNING, logger="coder_eval.isolation.docker_runner"):
        await run_post_run_on_host(result, rt)

    assert result.post_run_results == []
    assert any("skipping teardown" in r.getMessage() for r in caplog.records)


async def test_post_run_noop_when_no_post_run_commands(tmp_path):
    """A task without post_run does nothing (no persist, no results)."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    task = _task([])
    rt = _make_rt(tmp_path, task)
    result = _docker_result(artifacts)

    await run_post_run_on_host(result, rt)
    assert result.post_run_results == []
    # No re-persist happened for an empty post_run.
    assert not (rt.run_dir / "task.json").exists()
