"""Tests for the orchestrator's error_log_tail capture path."""

import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from coder_eval.errors import TaskTimeoutError
from coder_eval.models import (
    AgentKind,
    FileExistsCriterion,
    FinalStatus,
    SandboxConfig,
    TaskDefinition,
    parse_agent_config,
)
from coder_eval.orchestrator import Orchestrator


def _build_orchestrator(tmp_path: Path) -> Orchestrator:
    task = TaskDefinition(
        task_id="tail_capture_task",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(description="x", path="x.py")],
    )
    run_dir = tmp_path / "run" / "tail_capture_task"
    return Orchestrator(task=task, run_dir=run_dir, variant_id="v1")


def _patch_finalize_persistence():
    """Skip the on-disk persistence side-effects of _finalize_result."""
    return patch("coder_eval.reports_html.write_task_html", return_value=None)


@pytest.mark.asyncio
async def test_error_log_tail_populated_on_setup_error(tmp_path):
    orch = _build_orchestrator(tmp_path)

    async def boom() -> None:
        logging.getLogger("coder_eval.orchestrator").error("setup blew up: synthetic failure marker")
        raise RuntimeError("synthetic setup failure")

    with _patch_finalize_persistence(), patch.object(orch, "_setup", side_effect=boom):
        result = await orch.run()

    assert result.final_status == FinalStatus.ERROR
    assert result.error_log_tail is not None
    assert "synthetic failure marker" in result.error_log_tail


@pytest.mark.asyncio
async def test_error_log_tail_includes_teardown_logs(tmp_path):
    orch = _build_orchestrator(tmp_path)

    async def boom() -> None:
        raise RuntimeError("setup failure")

    async def noisy_post_run() -> None:
        logging.getLogger("coder_eval.orchestrator").error("post-run cleanup error: teardown marker")

    with (
        _patch_finalize_persistence(),
        patch.object(orch, "_setup", side_effect=boom),
        patch.object(orch, "_run_post_run_commands", side_effect=noisy_post_run),
        patch.object(orch, "_cleanup", AsyncMock()),
    ):
        result = await orch.run()

    assert result.final_status == FinalStatus.ERROR
    assert result.error_log_tail is not None
    assert "teardown marker" in result.error_log_tail


@pytest.mark.asyncio
async def test_error_log_tail_populated_on_task_timeout(tmp_path):
    orch = _build_orchestrator(tmp_path)

    async def boom() -> None:
        logging.getLogger("coder_eval.orchestrator").error("about to time out: timeout marker")
        raise TaskTimeoutError(1.0, task_id="tail_capture_task", elapsed_seconds=1.5)

    with _patch_finalize_persistence(), patch.object(orch, "_setup", side_effect=boom):
        result = await orch.run()

    assert result.final_status == FinalStatus.TIMEOUT
    assert result.error_log_tail is not None
    assert "timeout marker" in result.error_log_tail


@pytest.mark.asyncio
async def test_error_log_tail_none_on_success(tmp_path):
    orch = _build_orchestrator(tmp_path)

    async def fake_setup() -> None:
        return None

    fake_loop = AsyncMock(return_value=True)

    with (
        _patch_finalize_persistence(),
        patch.object(orch, "_setup", side_effect=fake_setup),
        patch.object(orch, "_evaluation_loop", fake_loop),
        patch.object(orch, "_run_post_run_commands", AsyncMock()),
        patch.object(orch, "_cleanup", AsyncMock()),
    ):
        result = await orch.run()

    assert result.final_status == FinalStatus.SUCCESS
    assert result.error_log_tail is None


@pytest.mark.asyncio
async def test_error_log_tail_populated_on_failure(tmp_path):
    orch = _build_orchestrator(tmp_path)

    async def fake_loop() -> bool:
        logging.getLogger("coder_eval.orchestrator").error("eval-loop log: failure marker")
        return False

    with (
        _patch_finalize_persistence(),
        patch.object(orch, "_setup", AsyncMock()),
        patch.object(orch, "_evaluation_loop", side_effect=fake_loop),
        patch.object(orch, "_run_post_run_commands", AsyncMock()),
        patch.object(orch, "_cleanup", AsyncMock()),
    ):
        result = await orch.run()

    assert result.final_status == FinalStatus.FAILURE
    assert result.error_log_tail is not None
    assert "failure marker" in result.error_log_tail


@pytest.mark.asyncio
async def test_error_log_tail_none_on_max_turns_exhausted(tmp_path):
    orch = _build_orchestrator(tmp_path)

    async def fake_loop() -> bool:
        assert orch.result is not None
        orch.result.max_turns_exhausted = True
        return False

    with (
        _patch_finalize_persistence(),
        patch.object(orch, "_setup", AsyncMock()),
        patch.object(orch, "_evaluation_loop", side_effect=fake_loop),
        patch.object(orch, "_run_post_run_commands", AsyncMock()),
        patch.object(orch, "_cleanup", AsyncMock()),
    ):
        result = await orch.run()

    assert result.final_status == FinalStatus.MAX_TURNS_EXHAUSTED
    assert result.error_log_tail is None


def test_evaluation_result_error_log_tail_default():
    from datetime import datetime

    from coder_eval.models import EvaluationResult

    result = EvaluationResult(
        task_id="t",
        task_description="d",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status=FinalStatus.SUCCESS,
        iteration_count=1,
    )
    assert result.error_log_tail is None


def test_evaluation_result_error_log_tail_round_trip():
    from datetime import datetime

    from coder_eval.models import EvaluationResult

    result = EvaluationResult(
        task_id="t",
        task_description="d",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status=FinalStatus.ERROR,
        iteration_count=1,
        error_log_tail="captured tail content",
    )
    payload = result.model_dump_json()
    restored = EvaluationResult.model_validate_json(payload)
    assert restored.error_log_tail == "captured tail content"
