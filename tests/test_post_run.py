"""Tests for post-run command model and orchestrator integration."""

import logging
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from coder_eval.models import (
    AgentKind,
    EvaluationResult,
    FileExistsCriterion,
    FinalStatus,
    PostRunCommand,
    PostRunResult,
    SandboxConfig,
    TaskDefinition,
    parse_agent_config,
)
from coder_eval.orchestrator import Orchestrator


DUMMY_CRITERION = FileExistsCriterion(type="file_exists", path="dummy.txt", description="dummy")


# ── Model tests ──────────────────────────────────────────────────────────────


class TestPostRunCommandModel:
    def test_defaults(self):
        cmd = PostRunCommand(command="python3 validate.py")
        assert cmd.command == "python3 validate.py"
        assert cmd.timeout == 30

    def test_with_timeout(self):
        cmd = PostRunCommand(command="bash check.sh --strict", timeout=60)
        assert cmd.timeout == 60

    def test_timeout_bounds(self):
        with pytest.raises(ValueError):
            PostRunCommand(command="echo hi", timeout=0)
        with pytest.raises(ValueError):
            PostRunCommand(command="echo hi", timeout=301)

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValueError):
            PostRunCommand(command="echo hi", unknown_field="bad")


class TestPostRunResultModel:
    def test_success_result(self):
        result = PostRunResult(command="echo ok", exit_code=0, stdout="ok\n", duration_seconds=1.5)
        assert result.exit_code == 0
        assert result.error is None

    def test_error_result(self):
        result = PostRunResult(command="false", error="Timed out after 30s")
        assert result.exit_code is None
        assert result.error == "Timed out after 30s"


class TestTaskDefinitionPostRun:
    def test_default_empty(self):
        task = TaskDefinition(
            task_id="test",
            description="test",
            initial_prompt="do something",
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[DUMMY_CRITERION],
        )
        assert task.post_run == []

    def test_with_post_run(self):
        task = TaskDefinition(
            task_id="test",
            description="test",
            initial_prompt="do something",
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[DUMMY_CRITERION],
            post_run=[PostRunCommand(command="python3 validate.py --strict")],
        )
        assert len(task.post_run) == 1
        assert task.post_run[0].command == "python3 validate.py --strict"


# ── Orchestrator integration tests ───────────────────────────────────────────


def _make_task(post_run: list[PostRunCommand] | None = None) -> TaskDefinition:
    return TaskDefinition(
        task_id="post_run_test",
        description="Test post-run commands",
        initial_prompt="do something",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[DUMMY_CRITERION],
        post_run=post_run or [],
    )


def _make_orchestrator(task: TaskDefinition, tmp_path: Path) -> Orchestrator:
    run_dir = tmp_path / "run" / task.task_id
    run_dir.mkdir(parents=True)
    orch = Orchestrator(task=task, run_dir=run_dir, variant_id="test")
    orch.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        variant_id="test",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status=FinalStatus.FAILURE,
        iteration_count=1,
    )
    return orch


@pytest.mark.asyncio
async def test_post_run_skipped_when_empty(tmp_path):
    task = _make_task(post_run=[])
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    await orch._run_post_run_commands()

    assert orch.result.post_run_results == []


@pytest.mark.asyncio
async def test_post_run_skipped_when_no_sandbox(tmp_path):
    task = _make_task(post_run=[PostRunCommand(command="echo hi")])
    orch = _make_orchestrator(task, tmp_path)
    # sandbox is None by default

    await orch._run_post_run_commands()

    assert orch.result.post_run_results == []


@pytest.mark.asyncio
async def test_post_run_command_success(tmp_path):
    task = _make_task(post_run=[PostRunCommand(command="echo '{\"ok\": true}'")])
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    await orch._run_post_run_commands()

    assert len(orch.result.post_run_results) == 1
    result = orch.result.post_run_results[0]
    assert result.exit_code == 0
    assert '"ok": true' in result.stdout
    assert result.error is None


@pytest.mark.asyncio
async def test_post_run_command_failure_does_not_affect_result(tmp_path):
    task = _make_task(
        post_run=[PostRunCommand(command="python3 -c \"import sys; print('bad', file=sys.stderr); sys.exit(1)\"")]
    )
    orch = _make_orchestrator(task, tmp_path)
    orch.result.final_status = FinalStatus.SUCCESS
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    await orch._run_post_run_commands()

    # The task result stays SUCCESS even though the post-run command failed
    assert orch.result.final_status == FinalStatus.SUCCESS
    result = orch.result.post_run_results[0]
    assert result.exit_code == 1
    assert "bad" in result.stderr


@pytest.mark.asyncio
async def test_post_run_command_with_pipes(tmp_path):
    """Shell commands support pipes and redirects."""
    task = _make_task(post_run=[PostRunCommand(command="echo hello world | tr a-z A-Z")])
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    await orch._run_post_run_commands()

    result = orch.result.post_run_results[0]
    assert result.exit_code == 0
    assert "HELLO WORLD" in result.stdout


@pytest.mark.asyncio
async def test_post_run_command_timeout(tmp_path):
    task = _make_task(post_run=[PostRunCommand(command="sleep 10", timeout=1)])
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    await orch._run_post_run_commands()

    result = orch.result.post_run_results[0]
    assert result.exit_code is None
    assert "Timed out" in result.error


@pytest.mark.asyncio
async def test_post_run_multiple_commands(tmp_path):
    task = _make_task(
        post_run=[
            PostRunCommand(command="echo a"),
            PostRunCommand(command="echo b"),
        ]
    )
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    await orch._run_post_run_commands()

    assert len(orch.result.post_run_results) == 2
    assert orch.result.post_run_results[0].stdout.strip() == "a"
    assert orch.result.post_run_results[1].stdout.strip() == "b"


@pytest.mark.asyncio
async def test_post_run_cwd_is_sandbox(tmp_path):
    """Post-run commands should execute with cwd set to the sandbox directory."""
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()

    task = _make_task(post_run=[PostRunCommand(command='python3 -c "import os; print(os.getcwd())"')])
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = sandbox_dir

    await orch._run_post_run_commands()

    result = orch.result.post_run_results[0]
    assert result.stdout.strip() == str(sandbox_dir)


@pytest.mark.asyncio
async def test_post_run_streams_stdout_to_logger(tmp_path, caplog):
    """Each line of stdout is forwarded to the orchestrator logger as it is read."""
    task = _make_task(post_run=[PostRunCommand(command="python3 -c \"print('line-one'); print('line-two')\"")])
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    with caplog.at_level(logging.INFO, logger="coder_eval.orchestrator"):
        await orch._run_post_run_commands()

    messages = [r.getMessage() for r in caplog.records]
    assert any("[post_run stdout] line-one" in m for m in messages)
    assert any("[post_run stdout] line-two" in m for m in messages)
    # And the final result still captures the full stdout.
    result = orch.result.post_run_results[0]
    assert "line-one" in result.stdout
    assert "line-two" in result.stdout


@pytest.mark.asyncio
async def test_post_run_streams_stderr_as_warning(tmp_path, caplog):
    """Stderr lines are forwarded at WARNING level (separate from stdout)."""
    task = _make_task(post_run=[PostRunCommand(command="python3 -c \"import sys; print('boom', file=sys.stderr)\"")])
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    with caplog.at_level(logging.WARNING, logger="coder_eval.orchestrator"):
        await orch._run_post_run_commands()

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("[post_run stderr] boom" in m for m in warnings)


@pytest.mark.asyncio
async def test_post_run_output_truncated(tmp_path):
    """Large output is truncated to _POST_RUN_MAX_OUTPUT."""
    # Generate output larger than the limit
    task = _make_task(post_run=[PostRunCommand(command="python3 -c \"print('x' * 200_000)\"")])
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    await orch._run_post_run_commands()

    result = orch.result.post_run_results[0]
    assert result.exit_code == 0
    assert len(result.stdout) <= Orchestrator._POST_RUN_MAX_OUTPUT
