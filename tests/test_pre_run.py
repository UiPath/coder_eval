"""Tests for pre-run command model and orchestrator integration."""

import logging
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from coder_eval.models import (
    AgentKind,
    EvaluationResult,
    FileExistsCriterion,
    FinalStatus,
    PostRunResult,
    PreRunCommand,
    SandboxConfig,
    TaskDefinition,
    parse_agent_config,
)
from coder_eval.orchestrator import Orchestrator


DUMMY_CRITERION = FileExistsCriterion(type="file_exists", path="dummy.txt", description="dummy")


# ── Model tests ──────────────────────────────────────────────────────────────


class TestPreRunCommandModel:
    def test_defaults(self):
        cmd = PreRunCommand(command="python3 seed.py")
        assert cmd.command == "python3 seed.py"
        assert cmd.timeout == 30
        assert cmd.fail_on_error is True

    def test_with_timeout(self):
        cmd = PreRunCommand(command="bash setup.sh", timeout=60)
        assert cmd.timeout == 60

    def test_fail_on_error_false(self):
        cmd = PreRunCommand(command="echo hi", fail_on_error=False)
        assert cmd.fail_on_error is False

    def test_timeout_bounds(self):
        with pytest.raises(ValueError):
            PreRunCommand(command="echo hi", timeout=0)
        with pytest.raises(ValueError):
            PreRunCommand(command="echo hi", timeout=301)

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValueError):
            PreRunCommand(command="echo hi", unknown_field="bad")


class TestPreRunResultReuse:
    def test_success_result(self):
        result = PostRunResult(command="echo ok", exit_code=0, stdout="ok\n", duration_seconds=1.0)
        assert result.exit_code == 0
        assert result.error is None

    def test_error_result(self):
        result = PostRunResult(command="false", error="Timed out after 30s")
        assert result.exit_code is None
        assert result.error == "Timed out after 30s"


class TestTaskDefinitionPreRun:
    def test_default_empty(self):
        task = TaskDefinition(
            task_id="test",
            description="test",
            initial_prompt="do something",
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[DUMMY_CRITERION],
        )
        assert task.pre_run == []

    def test_with_pre_run(self):
        task = TaskDefinition(
            task_id="test",
            description="test",
            initial_prompt="do something",
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[DUMMY_CRITERION],
            pre_run=[PreRunCommand(command="python3 seed.py")],
        )
        assert len(task.pre_run) == 1
        assert task.pre_run[0].command == "python3 seed.py"

    def test_pre_run_before_post_run(self):
        from coder_eval.models import PostRunCommand

        task = TaskDefinition(
            task_id="test",
            description="test",
            initial_prompt="do something",
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[DUMMY_CRITERION],
            pre_run=[PreRunCommand(command="echo pre")],
            post_run=[PostRunCommand(command="echo post")],
        )
        assert task.pre_run[0].command == "echo pre"
        assert task.post_run[0].command == "echo post"

    def test_fail_on_error_default_true(self):
        task = TaskDefinition(
            task_id="test",
            description="test",
            initial_prompt="do something",
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[DUMMY_CRITERION],
            pre_run=[PreRunCommand(command="echo hi")],
        )
        assert task.pre_run[0].fail_on_error is True


# ── Orchestrator integration tests ───────────────────────────────────────────


def _make_task(pre_run: list[PreRunCommand] | None = None) -> TaskDefinition:
    return TaskDefinition(
        task_id="pre_run_test",
        description="Test pre-run commands",
        initial_prompt="do something",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[DUMMY_CRITERION],
        pre_run=pre_run or [],
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
        iteration_count=0,
    )
    return orch


@pytest.mark.asyncio
async def test_pre_run_skipped_when_empty(tmp_path):
    task = _make_task(pre_run=[])
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    await orch._run_pre_run_commands()

    assert orch.result.pre_run_results == []


@pytest.mark.asyncio
async def test_pre_run_skipped_when_no_sandbox(tmp_path):
    task = _make_task(pre_run=[PreRunCommand(command="echo hi")])
    orch = _make_orchestrator(task, tmp_path)
    # sandbox is None by default

    await orch._run_pre_run_commands()

    assert orch.result.pre_run_results == []


@pytest.mark.asyncio
async def test_pre_run_command_success(tmp_path):
    task = _make_task(pre_run=[PreRunCommand(command="echo '{\"ok\": true}'")])
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    await orch._run_pre_run_commands()

    assert len(orch.result.pre_run_results) == 1
    result = orch.result.pre_run_results[0]
    assert result.exit_code == 0
    assert '"ok": true' in result.stdout
    assert result.error is None


@pytest.mark.asyncio
async def test_pre_run_command_with_pipes(tmp_path):
    task = _make_task(pre_run=[PreRunCommand(command="echo hello world | tr a-z A-Z")])
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    await orch._run_pre_run_commands()

    result = orch.result.pre_run_results[0]
    assert result.exit_code == 0
    assert "HELLO WORLD" in result.stdout


@pytest.mark.asyncio
async def test_pre_run_multiple_commands(tmp_path):
    task = _make_task(
        pre_run=[
            PreRunCommand(command="echo a"),
            PreRunCommand(command="echo b"),
        ]
    )
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    await orch._run_pre_run_commands()

    assert len(orch.result.pre_run_results) == 2
    assert orch.result.pre_run_results[0].stdout.strip() == "a"
    assert orch.result.pre_run_results[1].stdout.strip() == "b"


@pytest.mark.asyncio
async def test_pre_run_cwd_is_sandbox(tmp_path):
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()

    task = _make_task(pre_run=[PreRunCommand(command='python3 -c "import os; print(os.getcwd())"')])
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = sandbox_dir

    await orch._run_pre_run_commands()

    result = orch.result.pre_run_results[0]
    assert result.stdout.strip() == str(sandbox_dir)


@pytest.mark.asyncio
async def test_pre_run_streams_stdout_to_logger(tmp_path, caplog):
    task = _make_task(pre_run=[PreRunCommand(command="python3 -c \"print('line-one'); print('line-two')\"")])
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    with caplog.at_level(logging.INFO, logger="coder_eval.orchestrator"):
        await orch._run_pre_run_commands()

    messages = [r.getMessage() for r in caplog.records]
    assert any("[pre_run stdout] line-one" in m for m in messages)
    assert any("[pre_run stdout] line-two" in m for m in messages)
    result = orch.result.pre_run_results[0]
    assert "line-one" in result.stdout
    assert "line-two" in result.stdout


@pytest.mark.asyncio
async def test_pre_run_streams_stderr_as_warning(tmp_path, caplog):
    task = _make_task(
        pre_run=[
            PreRunCommand(
                command="python3 -c \"import sys; print('boom', file=sys.stderr)\"",
                fail_on_error=False,
            )
        ]
    )
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    with caplog.at_level(logging.WARNING, logger="coder_eval.orchestrator"):
        await orch._run_pre_run_commands()

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("[pre_run stderr] boom" in m for m in warnings)


@pytest.mark.asyncio
async def test_pre_run_output_truncated(tmp_path):
    task = _make_task(pre_run=[PreRunCommand(command="python3 -c \"print('x' * 200_000)\"")])
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    await orch._run_pre_run_commands()

    result = orch.result.pre_run_results[0]
    assert result.exit_code == 0
    assert len(result.stdout) <= Orchestrator._POST_RUN_MAX_OUTPUT


# ── Abort behavior (fail_on_error=True, the default) ─────────────────────────


@pytest.mark.asyncio
async def test_pre_run_failure_raises_when_fail_on_error_true(tmp_path):
    task = _make_task(
        pre_run=[PreRunCommand(command='python3 -c "import sys; sys.exit(1)"')],
    )
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    with pytest.raises(RuntimeError, match="Pre-run command failed"):
        await orch._run_pre_run_commands()


@pytest.mark.asyncio
async def test_pre_run_timeout_raises_when_fail_on_error_true(tmp_path):
    task = _make_task(pre_run=[PreRunCommand(command="sleep 10", timeout=1)])
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    with pytest.raises(RuntimeError, match="timed out after 1s"):
        await orch._run_pre_run_commands()


@pytest.mark.asyncio
async def test_pre_run_failure_result_captured_before_raise(tmp_path):
    task = _make_task(
        pre_run=[PreRunCommand(command='python3 -c "import sys; sys.exit(2)"')],
    )
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    with pytest.raises(RuntimeError):
        await orch._run_pre_run_commands()

    assert len(orch.result.pre_run_results) == 1
    assert orch.result.pre_run_results[0].exit_code == 2


@pytest.mark.asyncio
async def test_pre_run_subsequent_commands_skipped_after_abort(tmp_path):
    task = _make_task(
        pre_run=[
            PreRunCommand(command='python3 -c "import sys; sys.exit(1)"'),
            PreRunCommand(command="echo should-not-run"),
        ],
    )
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    with pytest.raises(RuntimeError):
        await orch._run_pre_run_commands()

    assert len(orch.result.pre_run_results) == 1


# ── Non-abort behavior (fail_on_error=False) ─────────────────────────────────


@pytest.mark.asyncio
async def test_pre_run_failure_does_not_raise_when_fail_on_error_false(tmp_path):
    task = _make_task(
        pre_run=[
            PreRunCommand(
                command='python3 -c "import sys; sys.exit(1)"',
                fail_on_error=False,
            )
        ],
    )
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    await orch._run_pre_run_commands()

    assert len(orch.result.pre_run_results) == 1
    assert orch.result.pre_run_results[0].exit_code == 1


@pytest.mark.asyncio
async def test_pre_run_spawn_exception_raises_when_fail_on_error_true(tmp_path):
    """Generic exceptions from create_subprocess_shell propagate as RuntimeError when fail_on_error=True."""
    task = _make_task(pre_run=[PreRunCommand(command="echo hi")])
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    with (
        patch("coder_eval.orchestrator.asyncio.create_subprocess_shell", side_effect=OSError("spawn failed")),
        pytest.raises(RuntimeError, match="Pre-run command failed"),
    ):
        await orch._run_pre_run_commands()

    assert len(orch.result.pre_run_results) == 1
    assert "spawn failed" in (orch.result.pre_run_results[0].error or "")


@pytest.mark.asyncio
async def test_pre_run_spawn_exception_does_not_raise_when_fail_on_error_false(tmp_path):
    """Spawn exceptions are swallowed and recorded when fail_on_error=False."""
    task = _make_task(
        pre_run=[
            PreRunCommand(command="echo hi", fail_on_error=False),
            PreRunCommand(command="echo after"),
        ]
    )
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    real_shell = __import__("asyncio").create_subprocess_shell
    call_count = {"n": 0}

    async def flaky_shell(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("spawn failed")
        return await real_shell(*args, **kwargs)

    with patch("coder_eval.orchestrator.asyncio.create_subprocess_shell", side_effect=flaky_shell):
        await orch._run_pre_run_commands()

    assert len(orch.result.pre_run_results) == 2
    assert "spawn failed" in (orch.result.pre_run_results[0].error or "")
    assert orch.result.pre_run_results[1].stdout.strip() == "after"


@pytest.mark.asyncio
async def test_pre_run_timeout_does_not_raise_when_fail_on_error_false(tmp_path):
    task = _make_task(
        pre_run=[
            PreRunCommand(command="sleep 10", timeout=1, fail_on_error=False),
            PreRunCommand(command="echo after"),
        ],
    )
    orch = _make_orchestrator(task, tmp_path)
    orch.sandbox = AsyncMock(was_adopted=False)
    orch.sandbox.sandbox_dir = tmp_path

    await orch._run_pre_run_commands()

    assert len(orch.result.pre_run_results) == 2
    assert "Timed out" in (orch.result.pre_run_results[0].error or "")
    assert orch.result.pre_run_results[1].stdout.strip() == "after"
