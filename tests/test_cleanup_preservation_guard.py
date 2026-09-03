"""Orchestrator._cleanup: a preservation failure must never skip sandbox.cleanup().

Pins the sibling-try structure: preserve_to raising (e.g. disk full) logs a
warning, nulls result.sandbox_path (the artifacts were never moved), and the
tempdir is still removed via cleanup(preserve=False).
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from coder_eval.models import (
    AgentKind,
    ClaudeCodeAgentConfig,
    EvaluationResult,
    FileExistsCriterion,
    SandboxConfig,
    TaskDefinition,
)
from coder_eval.orchestrator import Orchestrator, PreservationMode


def _make_orchestrator(tmp_path) -> Orchestrator:
    agent = ClaudeCodeAgentConfig.model_construct(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        allowed_tools=None,
        model=None,
        ignore_patterns=[],
    )
    task = TaskDefinition.model_construct(
        task_id="cleanup_guard_test",
        description="Test task",
        initial_prompt="Do something",
        tags=[],
        agent=agent,
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(description="d", path="f.txt")],
        run_limits=None,
        reference=None,
    )
    run_dir = tmp_path / "run" / "cleanup_guard_test"
    run_dir.mkdir(parents=True)
    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")
    orchestrator.result = EvaluationResult(
        task_id="cleanup_guard_test",
        task_description="Test",
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )
    orchestrator.agent = None
    return orchestrator


@pytest.mark.asyncio
async def test_preserve_failure_does_not_skip_cleanup(tmp_path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    orchestrator.preservation_mode = PreservationMode.MOVE_ON_WRITE
    mock_sandbox = MagicMock()
    mock_sandbox.preserve_to = MagicMock(side_effect=OSError("No space left on device"))
    orchestrator.sandbox = mock_sandbox

    await orchestrator._cleanup()

    mock_sandbox.cleanup.assert_called_once_with(preserve=False)
    assert orchestrator.result.sandbox_path is None


@pytest.mark.asyncio
async def test_preserve_success_sets_path_and_cleanup_runs(tmp_path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    orchestrator.preservation_mode = PreservationMode.MOVE_ON_WRITE
    preserved = tmp_path / "run" / "cleanup_guard_test" / "artifacts"
    mock_sandbox = MagicMock()
    mock_sandbox.preserve_to = MagicMock(return_value=preserved)
    orchestrator.sandbox = mock_sandbox

    await orchestrator._cleanup()

    assert orchestrator.result.sandbox_path == str(preserved)
    mock_sandbox.cleanup.assert_called_once_with(preserve=False)


@pytest.mark.asyncio
async def test_cleanup_failure_is_swallowed_with_warning(tmp_path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    orchestrator.preservation_mode = PreservationMode.NONE
    mock_sandbox = MagicMock()
    mock_sandbox.cleanup = MagicMock(side_effect=OSError("cleanup blew up"))
    orchestrator.sandbox = mock_sandbox

    await orchestrator._cleanup()  # must not raise

    mock_sandbox.cleanup.assert_called_once_with(preserve=False)


@pytest.mark.asyncio
async def test_agent_stop_failure_does_not_skip_sandbox_cleanup(tmp_path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    orchestrator.preservation_mode = PreservationMode.NONE
    mock_agent = AsyncMock()
    mock_agent.stop = AsyncMock(side_effect=RuntimeError("stop failed"))
    orchestrator.agent = mock_agent
    mock_sandbox = MagicMock()
    orchestrator.sandbox = mock_sandbox

    await orchestrator._cleanup()

    mock_sandbox.cleanup.assert_called_once_with(preserve=False)


@pytest.mark.asyncio
async def test_direct_write_grant_failure_keeps_path_and_cleanup_runs(tmp_path) -> None:
    """DIRECT_WRITE: the sandbox is persistent — a chmod failure must not discard
    the still-valid artifacts pointer, and cleanup() (a no-op there) still runs."""
    orchestrator = _make_orchestrator(tmp_path)
    orchestrator.preservation_mode = PreservationMode.DIRECT_WRITE
    sandbox_dir = tmp_path / "run" / "cleanup_guard_test" / "artifacts"
    mock_sandbox = MagicMock()
    mock_sandbox.sandbox_dir = sandbox_dir
    mock_sandbox.grant_read_access = MagicMock(side_effect=OSError("chmod failed"))
    orchestrator.sandbox = mock_sandbox

    await orchestrator._cleanup()

    assert orchestrator.result.sandbox_path == str(sandbox_dir)
    mock_sandbox.cleanup.assert_called_once_with(preserve=False)


@pytest.mark.asyncio
async def test_direct_write_happy_path_sets_path(tmp_path) -> None:
    orchestrator = _make_orchestrator(tmp_path)
    orchestrator.preservation_mode = PreservationMode.DIRECT_WRITE
    sandbox_dir = tmp_path / "run" / "cleanup_guard_test" / "artifacts"
    mock_sandbox = MagicMock()
    mock_sandbox.sandbox_dir = sandbox_dir
    orchestrator.sandbox = mock_sandbox

    await orchestrator._cleanup()

    assert orchestrator.result.sandbox_path == str(sandbox_dir)
    mock_sandbox.grant_read_access.assert_called_once_with()
    mock_sandbox.cleanup.assert_called_once_with(preserve=False)


@pytest.mark.asyncio
async def test_workspace_dir_captures_out_even_with_preservation_none(tmp_path) -> None:
    """workspace_dir (docker WORKDIR alignment) takes precedence over preservation_mode:
    even NONE must capture the workspace out to run_dir/artifacts via capture_to, NOT
    discard it. This is the documented "takes precedence over preservation_mode" contract."""
    orchestrator = _make_orchestrator(tmp_path)
    orchestrator.preservation_mode = PreservationMode.NONE
    orchestrator.workspace_dir = Path("/root")
    captured = tmp_path / "run" / "cleanup_guard_test" / "artifacts" / "cleanup_guard_test"
    mock_sandbox = MagicMock()
    mock_sandbox.capture_to = MagicMock(return_value=captured)
    orchestrator.sandbox = mock_sandbox

    await orchestrator._cleanup()

    # workspace_dir wins: capture_to is invoked with the artifacts dir and its
    # returned path is recorded (NONE would otherwise have nulled sandbox_path).
    mock_sandbox.capture_to.assert_called_once_with(orchestrator.run_dir / "artifacts")
    assert orchestrator.result.sandbox_path == str(captured)
    # The NONE / MOVE_ON_WRITE arms must not run when workspace_dir is set.
    mock_sandbox.preserve_to.assert_not_called()
    mock_sandbox.cleanup.assert_called_once_with(preserve=False)


@pytest.mark.asyncio
async def test_workspace_capture_failure_with_none_does_not_skip_cleanup(tmp_path) -> None:
    """A capture_to failure under NONE + workspace_dir logs a warning, leaves
    sandbox_path unset, and still runs cleanup() (sibling-try guarantee)."""
    orchestrator = _make_orchestrator(tmp_path)
    orchestrator.preservation_mode = PreservationMode.NONE
    orchestrator.workspace_dir = Path("/root")
    mock_sandbox = MagicMock()
    mock_sandbox.capture_to = MagicMock(side_effect=OSError("No space left on device"))
    orchestrator.sandbox = mock_sandbox

    await orchestrator._cleanup()  # must not raise

    mock_sandbox.capture_to.assert_called_once_with(orchestrator.run_dir / "artifacts")
    # capture_to raised before assigning the path, so it stays at its default (None).
    assert orchestrator.result.sandbox_path is None
    mock_sandbox.cleanup.assert_called_once_with(preserve=False)


@pytest.mark.asyncio
async def test_none_without_workspace_dir_discards_path(tmp_path) -> None:
    """Contrast case: NONE with no workspace_dir takes the discard arm — no
    capture_to / preserve_to, sandbox_path nulled, tempdir cleaned up."""
    orchestrator = _make_orchestrator(tmp_path)
    orchestrator.preservation_mode = PreservationMode.NONE
    orchestrator.workspace_dir = None
    orchestrator.result.sandbox_path = "/stale/path"  # must be cleared
    mock_sandbox = MagicMock()
    # A real Sandbox that was not adopted. Explicit because a bare MagicMock
    # attribute is truthy, which would take the adopted arm (that one KEEPS the
    # path, since an adopted directory survives cleanup) and hide the discard.
    mock_sandbox.was_adopted = False
    orchestrator.sandbox = mock_sandbox

    await orchestrator._cleanup()

    mock_sandbox.capture_to.assert_not_called()
    mock_sandbox.preserve_to.assert_not_called()
    assert orchestrator.result.sandbox_path is None
    mock_sandbox.cleanup.assert_called_once_with(preserve=False)
