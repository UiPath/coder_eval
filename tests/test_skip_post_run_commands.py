"""Pre/post suppression for the in-container docker orchestrator.

Under ``--driver docker`` BOTH ``pre_run`` and ``post_run`` run HOST-side, not
in the container — ``pre_run`` before the container (seeding the workspace),
``post_run`` after the container exits (over the copied-out workspace). The
in-container orchestrator is therefore told to suppress BOTH via the single
blanket ``skip_pre_post_commands=True`` flag (also used by the host re-grade).
The tempdir / ``coder-eval evaluate`` paths leave the flag False and run both
in-process as before.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from coder_eval.models import (
    AgentKind,
    EvaluationResult,
    FileExistsCriterion,
    FinalStatus,
    PostRunCommand,
    PreRunCommand,
    SandboxConfig,
    TaskDefinition,
    parse_agent_config,
)
from coder_eval.orchestrator import Orchestrator


DUMMY_CRITERION = FileExistsCriterion(type="file_exists", path="dummy.txt", description="dummy")


def _make_task() -> TaskDefinition:
    return TaskDefinition(
        task_id="skip_post_test",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[DUMMY_CRITERION],
        pre_run=[PreRunCommand(command="echo pre-ran")],
        post_run=[PostRunCommand(command="echo post-ran")],
    )


def _make_orchestrator(tmp_path: Path, **kwargs) -> Orchestrator:
    task = _make_task()
    run_dir = tmp_path / "run" / task.task_id
    run_dir.mkdir(parents=True)
    orch = Orchestrator(task=task, run_dir=run_dir, variant_id="test", **kwargs)
    orch.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        variant_id="test",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status=FinalStatus.SUCCESS,
        iteration_count=1,
    )
    orch.sandbox = AsyncMock()
    orch.sandbox.sandbox_dir = tmp_path
    return orch


async def test_docker_container_skips_both_via_skip_pre_post(tmp_path):
    """Under docker the in-container orchestrator suppresses BOTH pre_run and post_run.

    Both hooks run host-side (pre before the container, post after it exits), so
    the container must run neither — expressed via the single blanket flag.
    """
    orch = _make_orchestrator(tmp_path, skip_pre_post_commands=True)

    await orch._run_pre_run_commands()
    await orch._run_post_run_commands()

    assert orch.result.pre_run_results == []
    assert orch.result.post_run_results == []


async def test_tempdir_runs_both(tmp_path):
    """Tempdir / evaluate path (flag False) runs both pre_run and post_run in-process."""
    orch = _make_orchestrator(tmp_path)  # flag defaults False

    await orch._run_pre_run_commands()
    await orch._run_post_run_commands()

    assert orch.result.pre_run_results[0].stdout.strip() == "pre-ran"
    assert orch.result.post_run_results[0].stdout.strip() == "post-ran"


async def test_skip_pre_post_defaults_false():
    """skip_pre_post_commands defaults False so non-docker paths are unaffected."""
    task = _make_task()
    orch = Orchestrator(task=task, run_dir=Path("/nonexistent"), variant_id="test")
    assert orch.skip_pre_post_commands is False
