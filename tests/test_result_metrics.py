"""Unit tests for coder_eval.result_metrics — EvaluationResult-derived metrics.

These are consumed by the ORCHESTRATOR during a run as well as by the reporters,
which is why they are not in a ``reports*`` module. The `None`-vs-`0.0` contract
(CE058) is the load-bearing behaviour: an unmeasured bucket must survive as
`None` so it renders as a dash rather than claiming a measurement nobody took.
"""

from datetime import datetime

from coder_eval.analysis import calculate_command_statistics
from coder_eval.models import (
    AgentKind,
    AssistantMessage,
    CommandTelemetry,
    EvaluationResult,
    FinalStatus,
    ResultSummary,
    TaskConfigRecord,
    TurnRecord,
)
from coder_eval.result_metrics import (
    TurnTimeBuckets,
    expected_tool_calls_overage,
    expected_turns_overage,
    has_final_reply,
    turn_time_buckets,
    visible_turn_count,
)


def _result(turns: list[TurnRecord], duration: float = 0.0) -> EvaluationResult:
    return EvaluationResult(
        task_id="t",
        task_description="d",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime(2026, 1, 1),
        final_status=FinalStatus.SUCCESS,
        iteration_count=len(turns),
        iterations=turns,
        duration_seconds=duration,
    )


def _turn(**kw) -> TurnRecord:
    return TurnRecord(**({"iteration": 1, "user_input": "u", "agent_output": "a"} | kw))


def _generation(ms: float) -> AssistantMessage:
    at = datetime(2026, 1, 1)
    return AssistantMessage(content="x", started_at=at, completed_at=at, generation_duration_ms=ms)


class TestTurnTimeBuckets:
    def test_every_measured_bucket_is_summed_across_turns(self):
        turns = [
            _turn(harness_startup_ms=10.0, harness_teardown_ms=5.0, messages=[_generation(20.0)]),
            _turn(harness_startup_ms=1.0, harness_teardown_ms=2.0, messages=[_generation(3.0)]),
        ]
        buckets = turn_time_buckets(_result(turns, duration=1.0))
        assert buckets.startup_ms == 11.0
        assert buckets.teardown_ms == 7.0
        assert buckets.generation_ms == 23.0
        assert buckets.unaccounted_ms == 1000.0 - 11.0 - 23.0 - 7.0

    def test_tool_time_is_summed_from_the_stored_union(self):
        """`tool_union_ms` is the collector's own span set, so reading it is how
        this surface and the collector agree rather than merely coincide."""
        turns = [_turn(tool_union_ms=40.0), _turn(tool_union_ms=2.0)]
        assert turn_time_buckets(_result(turns, duration=1.0)).tool_ms == 42.0

    def test_a_stored_zero_tool_union_is_a_measurement_not_a_miss(self):
        """Spans were recorded and occupied no measurable time — that is `0ms`,
        not a dash, and must not fall through to re-derivation."""
        assert turn_time_buckets(_result([_turn(tool_union_ms=0.0)])).tool_ms == 0.0

    def test_an_unmeasured_bucket_stays_none_and_is_not_coerced_to_zero(self):
        """The CE058 contract — `0.0` would claim a measurement nobody took."""
        buckets = turn_time_buckets(_result([_turn(messages=[_generation(20.0)])]))
        assert buckets.startup_ms is None
        assert buckets.teardown_ms is None
        assert buckets.tool_ms is None
        assert buckets.generation_ms == 20.0

    def test_a_measured_zero_stays_zero(self):
        """Measured 0.0 differs from unmeasured — it renders as `0ms`, not a dash."""
        assert turn_time_buckets(_result([_turn(harness_startup_ms=0.0)])).startup_ms == 0.0

    def test_an_untimed_run_has_no_residual_rather_than_a_negative_one(self):
        """duration_seconds == 0.0 means never timed; subtracting real buckets
        from it would render a fabricated negative residual."""
        assert turn_time_buckets(_result([_turn(harness_startup_ms=10.0)])).unaccounted_ms is None

    def test_empty_iterations_measures_nothing(self):
        assert turn_time_buckets(_result([])) == TurnTimeBuckets(None, None, None, None, None)


def _make_result(
    *,
    resolved: dict | None = None,
    turns: list[TurnRecord] | None = None,
    task_config: bool = True,
) -> EvaluationResult:
    cfg: TaskConfigRecord | None = None
    if task_config:
        cfg = TaskConfigRecord(resolved=resolved or {}, source_yaml="")
    return EvaluationResult(
        task_id="t",
        task_description="d",
        variant_id="v",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status=FinalStatus.SUCCESS,
        iteration_count=0,
        task_config=cfg,
        turns=turns or [],
    )


def _cmd(idx: int) -> CommandTelemetry:
    return CommandTelemetry(
        tool_name="Bash",
        tool_id=f"t{idx}",
        timestamp=datetime.now(),
    )


def _turn_with_commands(commands: int = 0, reply: str | None = None) -> TurnRecord:
    """Build a TurnRecord with `commands` tool calls and an optional final reply."""
    return TurnRecord(
        iteration=1,
        user_input="p",
        agent_output="a",
        commands=[_cmd(i) for i in range(commands)],
        result_summary=(ResultSummary(is_error=False, subtype="success", result=reply) if reply is not None else None),
    )


class TestExpectedToolCallsOverage:
    def test_strict_greater_than(self):
        # 5 tools + reply = 6 visible turns. Budget 6 → no overage (equal).
        result = _make_result(
            resolved={"run_limits": {"expected_tool_calls": 6}},
            turns=[_turn_with_commands(commands=5, reply="done")],
        )
        assert expected_tool_calls_overage(result) is None

        # 5 tools + reply = 6 visible turns. Budget 5 → overage (6 > 5).
        result = _make_result(
            resolved={"run_limits": {"expected_tool_calls": 5}},
            turns=[_turn_with_commands(commands=5, reply="done")],
        )
        assert expected_tool_calls_overage(result) == (6, 5)

    def test_missing_reply_skipped(self):
        # Tools across multiple iterations sum correctly; absent reply
        # contributes nothing (no +1).
        result = _make_result(
            resolved={"run_limits": {"expected_tool_calls": 5}},
            turns=[_turn_with_commands(commands=4), _turn_with_commands(commands=5)],
        )
        assert expected_tool_calls_overage(result) == (9, 5)

    def test_task_config_none(self):
        result = _make_result(task_config=False, turns=[_turn_with_commands(commands=10)])
        assert expected_tool_calls_overage(result) is None

    def test_run_limits_missing(self):
        result = _make_result(resolved={}, turns=[_turn_with_commands(commands=10)])
        assert expected_tool_calls_overage(result) is None

    def test_expected_tool_calls_unset(self):
        result = _make_result(resolved={"run_limits": {"max_turns": 10}}, turns=[_turn_with_commands(commands=20)])
        assert expected_tool_calls_overage(result) is None

    def test_expected_turns_does_not_feed_the_tool_call_overage(self):
        result = _make_result(resolved={"run_limits": {"expected_turns": 5}}, turns=[_turn_with_commands(commands=20)])
        assert expected_tool_calls_overage(result) is None

    def test_invalid_expected_type(self):
        result = _make_result(
            resolved={"run_limits": {"expected_tool_calls": "ten"}}, turns=[_turn_with_commands(commands=20)]
        )
        assert expected_tool_calls_overage(result) is None

    def test_expected_tool_calls_zero_treated_as_invalid(self):
        # Defensive: the model enforces ge=1, but a hand-rolled task.json could
        # still inject 0 — the helper must treat it as a disabled check.
        result = _make_result(
            resolved={"run_limits": {"expected_tool_calls": 0}}, turns=[_turn_with_commands(commands=10)]
        )
        assert expected_tool_calls_overage(result) is None

    def test_run_limits_not_a_dict(self):
        result = _make_result(resolved={"run_limits": "not-a-dict"}, turns=[_turn_with_commands(commands=10)])
        assert expected_tool_calls_overage(result) is None

    def test_empty_turns(self):
        result = _make_result(resolved={"run_limits": {"expected_tool_calls": 1}}, turns=[])
        assert expected_tool_calls_overage(result) is None


class TestTurnDefinitionMatchesDoc:
    """Pin the turn definition:

        visible_turn_count == command_stats.total_commands + (1 if final reply)

    The evalboard "Turns" cell, ``displayedTurns``/``actual_commands``, and the
    proposed historical reconstruction all read ``command_stats.total_commands``
    (i.e. the tool-call part of the persisted ``visible_turns`` field). If
    someone later changes ``calculate_command_statistics`` to filter commands,
    that count would silently diverge from ``visible_turn_count`` and these
    cells would drift. This test fails first if that ever happens.
    """

    def test_mixed_tools_with_final_reply(self):
        # 2 + 3 tool calls across two iterations, plus a final reply.
        result = _make_result(turns=[_turn_with_commands(commands=2), _turn_with_commands(commands=3, reply="done")])
        stats = calculate_command_statistics(result.iterations)
        assert visible_turn_count(result) == stats.total_commands + (1 if has_final_reply(result) else 0)

    def test_tools_without_final_reply(self):
        # Crashed before producing a reply: the +1 must be omitted on both sides.
        result = _make_result(turns=[_turn_with_commands(commands=4)])
        stats = calculate_command_statistics(result.iterations)
        assert visible_turn_count(result) == stats.total_commands + (1 if has_final_reply(result) else 0)


class TestExpectedTurnsOverage:
    @staticmethod
    def _result(expected: object, model_turns: int | None) -> EvaluationResult:
        run_limits = {} if expected is None else {"expected_turns": expected}
        result = _make_result(resolved={"run_limits": run_limits})
        result.model_turns = model_turns
        return result

    def test_over(self):
        assert expected_turns_overage(self._result(3, 5)) == (5, 3)

    def test_equal_is_not_over(self):
        assert expected_turns_overage(self._result(3, 3)) is None

    def test_no_model_turn_count(self):
        assert expected_turns_overage(self._result(3, None)) is None

    def test_key_absent(self):
        assert expected_turns_overage(self._result(None, 5)) is None

    def test_invalid_expected_type(self):
        assert expected_turns_overage(self._result("ten", 5)) is None
