"""Telemetry emission from Orchestrator._finalize_result (CoderEval.Task.End/.Failed).

track_event is patched — no real exporter, network, or model traffic.
"""

import time
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from coder_eval.models import (
    AgentKind,
    EvaluationResult,
    FileExistsCriterion,
    FinalStatus,
    PreservationMode,
    SandboxConfig,
    TaskDefinition,
    TokenUsage,
    parse_agent_config,
)
from coder_eval.orchestrator import Orchestrator
from coder_eval.telemetry import hash_identifier


def _bootstrap(tmp_path, *, final_status, duration=None, score=None, iterations=1, total_token_usage=None):
    """Build an Orchestrator primed to run _finalize_result without running the loop."""
    task = TaskDefinition(
        task_id="tele_task",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(),
        success_criteria=[FileExistsCriterion(description="x", path="x.py")],
    )
    run_dir = tmp_path / "tele_run"
    run_dir.mkdir(parents=True)

    orch = Orchestrator(task, run_dir, preservation_mode=PreservationMode.NONE, variant_id="v1")
    orch.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        variant_id="v1",
        agent_type=task.agent.type,
        model_used="claude-x",
        started_at=datetime.now(),
        final_status=final_status,
        iteration_count=iterations,
        environment_info={},
        total_token_usage=total_token_usage,
    )
    if duration is not None:
        orch.result.duration_seconds = duration
    if score is not None:
        orch.result.weighted_score = score
    orch.agent = None  # skip the sdk_options branch
    return orch


def _finalize_and_capture(orch):
    with (
        patch("coder_eval.telemetry.track_event") as mock_track,
        patch("coder_eval.reports_html.write_task_html", return_value=None),
    ):
        orch._finalize_result(start_time=time.time() - 1.0)
    return mock_track


def test_success_emits_task_end(tmp_path):
    tu = TokenUsage(uncached_input_tokens=100, output_tokens=50)
    orch = _bootstrap(tmp_path, final_status=FinalStatus.SUCCESS, iterations=2, total_token_usage=tu)
    mock_track = _finalize_and_capture(orch)

    mock_track.assert_called_once()
    name, props = mock_track.call_args.args
    assert name == "CoderEval.Task.End"
    # Task/variant ids are emitted as stable one-way hashes, never verbatim.
    assert props["TaskId"] == hash_identifier("tele_task")
    assert props["TaskId"] != "tele_task"
    assert props["VariantId"] == hash_identifier("v1")
    assert props["Status"] == "SUCCESS"
    # Score carries the score finalize computed (0.0 here — empty criteria results).
    assert props["Score"] == orch.result.weighted_score
    assert isinstance(props["Score"], float)
    assert props["Iterations"] == 2
    assert props["AgentType"] == AgentKind.CLAUDE_CODE.value
    assert props["Model"] == "claude-x"
    assert props["Driver"] == "tempdir"
    assert props["DurationMs"] >= 0


@pytest.mark.parametrize(
    "status",
    [
        FinalStatus.ERROR,
        FinalStatus.TIMEOUT,
        FinalStatus.TOKEN_BUDGET_EXCEEDED,
        FinalStatus.COST_BUDGET_EXCEEDED,
    ],
)
def test_failure_statuses_emit_task_failed(tmp_path, status):
    orch = _bootstrap(tmp_path, final_status=status, iterations=0)
    mock_track = _finalize_and_capture(orch)
    name, props = mock_track.call_args.args
    assert name == "CoderEval.Task.Failed"
    assert props["Status"] == status.value


def test_non_failure_status_emits_task_end(tmp_path):
    # MAX_TURNS_EXHAUSTED is not in the failed allowlist → Task.End.
    orch = _bootstrap(tmp_path, final_status=FinalStatus.MAX_TURNS_EXHAUSTED, iterations=3)
    mock_track = _finalize_and_capture(orch)
    name, _ = mock_track.call_args.args
    assert name == "CoderEval.Task.End"


def test_token_counts_are_not_emitted(tmp_path):
    # Usage telemetry, not eval analytics: per-task token counts must never be
    # emitted, even when a populated TokenUsage is present.
    tu = TokenUsage(uncached_input_tokens=100, output_tokens=50)
    orch = _bootstrap(tmp_path, final_status=FinalStatus.SUCCESS, total_token_usage=tu)
    mock_track = _finalize_and_capture(orch)
    _, props = mock_track.call_args.args
    assert "InputTokens" not in props
    assert "OutputTokens" not in props
    assert "TotalTokens" not in props


def test_none_score_and_duration_default_to_zero(tmp_path):
    orch = _bootstrap(tmp_path, final_status=FinalStatus.SUCCESS, score=None, duration=None)
    mock_track = _finalize_and_capture(orch)
    _, props = mock_track.call_args.args
    # calculate_weighted_score writes 0.0 when criteria didn't run.
    assert props["Score"] == 0.0
    assert isinstance(props["DurationMs"], int)


def test_every_final_status_is_explicitly_bucketed():
    # Guard: every FinalStatus must be classified into exactly one telemetry
    # bucket (End vs Failed). A newly-added status fails this (and the module
    # import assert) until it's placed, rather than silently defaulting to End.
    from coder_eval.orchestrator import _TELEMETRY_END_STATUSES, _TELEMETRY_FAILED_STATUSES

    assert _TELEMETRY_FAILED_STATUSES.isdisjoint(_TELEMETRY_END_STATUSES)
    assert not (set(FinalStatus) - _TELEMETRY_FAILED_STATUSES - _TELEMETRY_END_STATUSES)


# --- build_task_event helper (shared by in-process + docker paths) -----------


def test_build_task_event_passes_driver_and_buckets_status():
    from coder_eval.orchestrator import build_task_event

    result = EvaluationResult(
        task_id="t",
        task_description="d",
        variant_id="v",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status=FinalStatus.SUCCESS,
        iteration_count=1,
        environment_info={},
    )
    name, props = build_task_event(result, driver="docker", variant_id="v1")
    assert name == "CoderEval.Task.End"
    assert props["Driver"] == "docker"
    assert props["VariantId"] == hash_identifier("v1")

    result.final_status = FinalStatus.TIMEOUT
    name, _ = build_task_event(result, driver="docker", variant_id="v1")
    assert name == "CoderEval.Task.Failed"


async def test_docker_path_emits_task_end_host_side(tmp_path):
    # The docker branch bypasses Orchestrator._finalize_result, so the host must
    # emit Task.End itself — otherwise --driver docker runs emit zero per-task events.
    from coder_eval.models import ResolvedTask
    from coder_eval.orchestration.batch import run_batch
    from coder_eval.orchestration.config import BatchRunConfig

    task = TaskDefinition(
        task_id="dock-task",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(driver="docker"),
        success_criteria=[FileExistsCriterion(description="x", path="x.py")],
    )
    resolved = [
        ResolvedTask(
            task=task,
            task_file=tmp_path / "task.yaml",
            run_dir=tmp_path / "run" / "v1" / "dock-task" / "00",
            variant_id="v1",
        )
    ]
    config = BatchRunConfig(run_dir=tmp_path / "run", max_parallel=1)

    fake_result = EvaluationResult(
        task_id="dock-task",
        task_description="d",
        variant_id="v1",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status=FinalStatus.SUCCESS,
        iteration_count=1,
        environment_info={},
        weighted_score=1.0,
    )

    with (
        patch("coder_eval.isolation.docker_runner.DockerRunner") as mock_runner_cls,
        patch("coder_eval.telemetry.track_event") as mock_track,
    ):
        mock_runner_cls.return_value.run = AsyncMock(return_value=fake_result)
        await run_batch(resolved, config)

    task_events = [c.args for c in mock_track.call_args_list if c.args[0].startswith("CoderEval.Task.")]
    assert len(task_events) == 1
    name, props = task_events[0]
    assert name == "CoderEval.Task.End"
    assert props["Driver"] == "docker"
    assert props["TaskId"] == hash_identifier("dock-task")
