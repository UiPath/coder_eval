"""The grading half of ``network: llm_only``: the in-container request and the orchestrator hook."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from coder_eval.cli.run_task_internal_command import request_grading_egress
from coder_eval.config import settings
from coder_eval.models import (
    AgentKind,
    ApiBackend,
    DockerDriverConfig,
    FileExistsCriterion,
    FinalStatus,
    RunCommandCriterion,
    SandboxConfig,
    TaskDefinition,
    parse_agent_config,
)
from coder_eval.models.tasks import SimulationConfig
from coder_eval.orchestrator import Orchestrator
from coder_eval.path_utils import EGRESS_GRADING_ACK_FILENAME, EGRESS_GRADING_REQUEST_FILENAME


async def _answer(output_dir: Path, reply: str) -> None:
    request = output_dir / EGRESS_GRADING_REQUEST_FILENAME
    while not request.is_file():
        await asyncio.sleep(0.01)
    (output_dir / EGRESS_GRADING_ACK_FILENAME).write_text(reply, encoding="utf-8")


async def test_request_returns_once_the_host_confirms(tmp_path: Path) -> None:
    await asyncio.gather(request_grading_egress(tmp_path, timeout=5), _answer(tmp_path, "ok\n"))
    assert (tmp_path / EGRESS_GRADING_REQUEST_FILENAME).read_text(encoding="utf-8") == "grading"


async def test_request_raises_on_a_refusal_or_no_answer(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r"could not switch.*error: probe"):
        await asyncio.gather(request_grading_egress(tmp_path, timeout=5), _answer(tmp_path, "error: probe"))
    (tmp_path / EGRESS_GRADING_ACK_FILENAME).unlink()
    with pytest.raises(RuntimeError, match="did not switch"):
        await request_grading_egress(tmp_path, timeout=0.2)


async def test_a_stale_ack_does_not_answer_a_new_request(tmp_path: Path) -> None:
    (tmp_path / EGRESS_GRADING_ACK_FILENAME).write_text("ok\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="did not switch"):
        await request_grading_egress(tmp_path, timeout=0.2)


async def test_a_switch_that_only_marks_the_phase_does_not_stop_grading(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    await asyncio.gather(request_grading_egress(tmp_path, timeout=5, widens=False), _answer(tmp_path, "error: x"))
    (tmp_path / EGRESS_GRADING_ACK_FILENAME).unlink()
    await request_grading_egress(tmp_path, timeout=0.2, widens=False)
    assert caplog.text.count("Refused grading hosts will not be reported") == 2


def test_a_simulated_task_cannot_widen_grading() -> None:
    with pytest.raises(ValidationError, match="between dialog turns"):
        TaskDefinition(
            task_id="t",
            description="d",
            agent=parse_agent_config(type="claude-code"),
            sandbox=SandboxConfig(
                driver="docker", docker=DockerDriverConfig(network="llm_only", grading_network="bridge")
            ),
            simulation=SimulationConfig(enabled=True, persona="a user", goal="get help"),
            success_criteria=[FileExistsCriterion(description="c", path="x.txt")],
        )


async def test_the_hook_runs_once_before_the_criteria(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "api_backend", ApiBackend.DIRECT)
    marker = tmp_path / "grading-started"
    task = TaskDefinition(
        task_id="noop",
        description="d",
        agent=parse_agent_config(type=AgentKind.NONE),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[RunCommandCriterion(description="after the hook", command=f"test -f {marker}")],
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    orch = Orchestrator(task=task, run_dir=run_dir, variant_id="v")
    calls: list[int] = []

    async def _hook() -> None:
        calls.append(1)
        marker.touch()

    orch.before_grading = _hook
    await orch.run()
    await orch._start_grading()
    assert calls == [1]
    assert orch.result is not None and orch.result.final_status == FinalStatus.SUCCESS
