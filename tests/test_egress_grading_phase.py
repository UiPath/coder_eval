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


@pytest.mark.asyncio
async def test_failed_before_grading_hook_records_a_grading_incident(tmp_path: Path) -> None:
    task = TaskDefinition(
        task_id="grading-hook-failure",
        description="offline fixture",
        agent=parse_agent_config(type=AgentKind.NONE),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(path="proof.txt", description="proof")],
    )
    orch = Orchestrator(task=task, run_dir=tmp_path / "run", variant_id="v")

    async def refused() -> None:
        raise RuntimeError("grading egress unavailable")

    orch.before_grading = refused
    result = await orch.run()

    assert result.final_status is FinalStatus.ERROR
    assert result.error_phase == "grading"
    assert result.error_message == "grading egress unavailable"
    assert result.error_details is not None
    assert result.error_details["component"] == "orchestrator.before_grading"


@pytest.mark.asyncio
async def test_reference_tampering_remains_an_execution_fact_after_regrading(tmp_path: Path) -> None:
    from coder_eval.models import ReferenceSource
    from coder_eval.orchestration.regrade import regrade_in_place

    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "answer.txt").write_text("original answer")
    task_file = tmp_path / "task.yaml"
    task_file.write_text("task_id: anti-cheat-fixture\n")
    task = TaskDefinition(
        task_id="anti-cheat-fixture",
        description="offline reference integrity fixture",
        agent=parse_agent_config(type=AgentKind.NONE),
        sandbox=SandboxConfig(driver="tempdir"),
        reference=ReferenceSource(directory="reference"),
        success_criteria=[FileExistsCriterion(path="proof.txt", description="proof exists")],
    )
    orch = Orchestrator(task=task, task_file=task_file, run_dir=tmp_path / "run", variant_id="v")

    async def overwrite_private_reference() -> None:
        assert orch._reference_dir is not None
        (orch._reference_dir / "answer.txt").write_text("changed answer")

    orch.before_grading = overwrite_private_reference
    prior = await orch.run()
    assert prior.final_status is FinalStatus.ERROR
    assert prior.error_phase == "execution"
    assert prior.error_history == []
    workspace = tmp_path / "retained-workspace"
    workspace.mkdir()
    (workspace / "proof.txt").write_text("proof")

    repaired = await regrade_in_place(
        task=task,
        prior=prior,
        workspace=workspace,
        run_dir=tmp_path / "grade",
        task_file=task_file,
        source_yaml=task_file.read_text(),
        variant_id="v",
    )

    assert repaired.weighted_score == 1.0
    assert repaired.final_status is FinalStatus.ERROR
    assert repaired.error_phase == "execution"
    assert repaired.error_message == prior.error_message
