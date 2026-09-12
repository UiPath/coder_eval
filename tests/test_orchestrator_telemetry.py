"""Telemetry emission from Orchestrator._finalize_result (CoderEval.Task.End).

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
    ("status", "category"),
    [
        (FinalStatus.SUCCESS, "succeeded"),
        (FinalStatus.FAILURE, "failed"),
        (FinalStatus.MAX_TURNS_EXHAUSTED, "failed"),
        (FinalStatus.ERROR, "error"),
        (FinalStatus.TIMEOUT, "failed"),
        (FinalStatus.TOKEN_BUDGET_EXCEEDED, "failed"),
        (FinalStatus.COST_BUDGET_EXCEEDED, "failed"),
    ],
)
def test_every_status_emits_task_end_with_status_and_category(tmp_path, status, category):
    # Every task emits the SAME event name; outcome lives in the Status/Category
    # dimensions, never the name. Category mirrors the canonical FinalStatus.category.
    orch = _bootstrap(tmp_path, final_status=status, iterations=0)
    mock_track = _finalize_and_capture(orch)
    name, props = mock_track.call_args.args
    assert name == "CoderEval.Task.End"
    assert props["Status"] == status.value
    assert props["Category"] == category


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


def test_category_dimension_tracks_final_status_category_for_every_status():
    # Guard against drift: the telemetry Category dimension must be a pure
    # function of the canonical FinalStatus.category (the SSOT shared with
    # reports) for EVERY status — never an independently-maintained bucket. A
    # newly-added FinalStatus is covered automatically.
    from coder_eval.orchestrator import build_task_event

    for status in FinalStatus:
        result = EvaluationResult(
            task_id="t",
            task_description="d",
            variant_id="v",
            agent_type=AgentKind.CLAUDE_CODE,
            started_at=datetime.now(),
            final_status=status,
            iteration_count=1,
            environment_info={},
        )
        name, props = build_task_event(result, driver="tempdir", variant_id="v")
        assert name == "CoderEval.Task.End"
        assert props["Category"] == status.category


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
    assert props["Category"] == "succeeded"

    result.final_status = FinalStatus.TIMEOUT
    name, props = build_task_event(result, driver="docker", variant_id="v1")
    assert name == "CoderEval.Task.End"
    assert props["Status"] == "TIMEOUT"
    assert props["Category"] == "failed"


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


class TestTheFourBucketDimensions:
    """`CoderEval.Task.End` carries the wall-clock buckets, each independently optional.

    Each is OMITTED rather than coalesced to 0 when unmeasured, for the same
    reason `Score` is: a dashboard averaging `StartupMs` with no filter reads a
    laundered zero as a harness that booted instantly, which is
    indistinguishable from a run predating the capture. Four separate
    assertions, not one combined — a single `all four present` check passes
    even if one dimension were wired to another's value.
    """

    _NAMES = ("StartupMs", "GenerationMs", "ToolExecMs", "TeardownMs")

    @staticmethod
    def _result(turns):
        from coder_eval.models import TurnRecord

        return EvaluationResult(
            task_id="t",
            task_description="d",
            variant_id="v",
            agent_type=AgentKind.CLAUDE_CODE,
            started_at=datetime.now(),
            final_status=FinalStatus.SUCCESS,
            iteration_count=len(turns),
            environment_info={},
            duration_seconds=10.0,
            iterations=[TurnRecord.model_validate(t) for t in turns],
        )

    @staticmethod
    def _turn(**overrides):
        from datetime import timedelta

        base = datetime(2026, 9, 11, 9, 0, 0)
        turn = {
            "iteration": 1,
            "user_input": "go",
            "agent_output": "done",
            "duration_seconds": 5.0,
            "messages": [
                {
                    "role": "assistant",
                    "started_at": base.isoformat(),
                    "completed_at": (base + timedelta(milliseconds=800)).isoformat(),
                    "generation_duration_ms": 800.0,
                }
            ],
            "harness_startup_ms": 500.0,
            "harness_teardown_ms": 100.0,
            "tool_union_ms": 200.0,
        }
        turn.update(overrides)
        return turn

    def _props(self, **overrides):
        from coder_eval.orchestrator import build_task_event

        _, props = build_task_event(self._result([self._turn(**overrides)]), driver="tempdir", variant_id="v1")
        return props

    def test_every_dimension_is_present_and_carries_its_own_value(self):
        props = self._props()
        assert props["StartupMs"] == pytest.approx(500.0)
        assert props["GenerationMs"] == pytest.approx(800.0)
        assert props["ToolExecMs"] == pytest.approx(200.0)
        assert props["TeardownMs"] == pytest.approx(100.0)

    def test_an_unmeasured_startup_is_omitted(self):
        props = self._props(harness_startup_ms=None)
        assert "StartupMs" not in props
        assert "GenerationMs" in props and "ToolExecMs" in props and "TeardownMs" in props

    def test_an_unmeasured_teardown_is_omitted(self):
        props = self._props(harness_teardown_ms=None)
        assert "TeardownMs" not in props
        assert "StartupMs" in props

    def test_an_unmeasured_tool_bucket_is_omitted(self):
        # No stored value AND no bounded command to derive one from.
        props = self._props(tool_union_ms=None, commands=[])
        assert "ToolExecMs" not in props
        assert "GenerationMs" in props

    def test_an_unmeasured_generation_is_omitted(self):
        props = self._props(messages=[])
        assert "GenerationMs" not in props
        assert "StartupMs" in props

    def test_a_measured_zero_is_emitted_rather_than_omitted(self):
        """The control: 0.0 is a measurement and must reach the dashboard."""
        props = self._props(harness_startup_ms=0.0)
        assert props["StartupMs"] == 0.0

    def test_it_does_not_sum_anything_itself(self):
        """One producer for the buckets, and `build_task_event` is not it."""
        import inspect

        from coder_eval.orchestrator import build_task_event

        source = inspect.getsource(build_task_event)
        assert "turn_time_buckets(result)" in source
        assert "harness_startup_ms" not in source, "the summation belongs to reports_stats"
