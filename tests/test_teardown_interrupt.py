"""``run()``'s teardown must be interrupt-proof.

The task-timeout watchdog cancels the asyncio task via
``loop.call_soon_threadsafe(task.cancel)``, delivering a ``CancelledError`` at
the next await. When that await is inside ``_run_post_run_commands`` (post-run
commands do awaited I/O), the cancellation used to land in ``run()``'s
``finally`` block and abort it wholesale — skipping ``_cleanup()`` (tempdir
leaked; workspace never preserved) AND ``_finalize_result()`` (task.json lost,
so the task silently vanished from the run). These tests pin that a post-run
interrupt — cancellation or a plain exception — still runs the full teardown
(cleanup + finalize) and then re-raises the original exception unchanged.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from coder_eval.models import (
    AgentKind,
    FileExistsCriterion,
    SandboxConfig,
    TaskDefinition,
    parse_agent_config,
)
from coder_eval.orchestrator import Orchestrator


def _build_orchestrator(tmp_path: Path) -> Orchestrator:
    task = TaskDefinition(
        task_id="teardown_interrupt_task",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(description="x", path="x.py")],
    )
    run_dir = tmp_path / "run" / "teardown_interrupt_task"
    return Orchestrator(task=task, run_dir=run_dir, variant_id="v1")


def _patch_finalize_persistence():
    """Skip the on-disk persistence side-effects of _finalize_result."""
    return patch("coder_eval.reports_html.write_task_html", return_value=None)


@pytest.mark.asyncio
async def test_post_run_cancellation_still_runs_cleanup_and_finalize(tmp_path):
    """The task-timeout watchdog's CancelledError lands during post-run: teardown
    must still complete (cleanup + finalize) and the cancellation still propagate."""
    orch = _build_orchestrator(tmp_path)
    cleanup = AsyncMock()

    async def boom_setup() -> None:
        raise RuntimeError("synthetic setup failure")

    with (
        _patch_finalize_persistence(),
        patch.object(orch, "_setup", side_effect=boom_setup),
        patch.object(orch, "_run_post_run_commands", AsyncMock(side_effect=asyncio.CancelledError())),
        patch.object(orch, "_cleanup", cleanup),
        pytest.raises(asyncio.CancelledError),
    ):
        await orch.run()

    cleanup.assert_awaited_once()
    # _finalize_result ran: the result was completed and scored despite the interrupt.
    assert orch.result is not None
    assert orch.result.completed_at is not None


@pytest.mark.asyncio
async def test_post_run_plain_exception_still_runs_cleanup_and_reraises(tmp_path):
    orch = _build_orchestrator(tmp_path)
    cleanup = AsyncMock()

    with (
        _patch_finalize_persistence(),
        patch.object(orch, "_setup", AsyncMock()),
        patch.object(orch, "_evaluation_loop", AsyncMock(return_value=True)),
        patch.object(orch, "_run_post_run_commands", AsyncMock(side_effect=OSError("post-run blew up"))),
        patch.object(orch, "_cleanup", cleanup),
        pytest.raises(OSError, match="post-run blew up"),
    ):
        await orch.run()

    cleanup.assert_awaited_once()
    assert orch.result is not None
    assert orch.result.completed_at is not None
