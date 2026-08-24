"""Unit tests for coder_eval.reports_experiment.eval_result_to_task_dict."""

from datetime import datetime

from coder_eval.models import (
    AgentKind,
    CommandTelemetry,
    EvaluationResult,
    FinalStatus,
    ResultSummary,
    TaskConfigRecord,
    TurnRecord,
)
from coder_eval.reports_experiment import eval_result_to_task_dict


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


def _turn(n: int | None) -> TurnRecord:
    return TurnRecord(iteration=1, user_input="p", agent_output="a", num_turns=n)


def _visible_turn(commands: int = 0, reply: str | None = None) -> TurnRecord:
    """TurnRecord with `commands` tool calls and an optional final reply."""
    return TurnRecord(
        iteration=1,
        user_input="p",
        agent_output="a",
        commands=[
            CommandTelemetry(tool_name="Bash", tool_id=f"t{i}", timestamp=datetime.now()) for i in range(commands)
        ],
        result_summary=(ResultSummary(is_error=False, subtype="success", result=reply) if reply is not None else None),
    )


class TestVisibleTurns:
    def test_counts_tool_calls_plus_final_reply(self):
        # 5 tool calls + a final reply = 6 visible turns.
        result = _make_result(turns=[_visible_turn(commands=5, reply="done")])
        d = eval_result_to_task_dict(result)
        assert d["visible_turns"] == 6

    def test_no_reply_omits_plus_one(self):
        result = _make_result(turns=[_visible_turn(commands=4), _visible_turn(commands=5)])
        d = eval_result_to_task_dict(result)
        assert d["visible_turns"] == 9

    def test_empty_turns(self):
        result = _make_result(turns=[])
        d = eval_result_to_task_dict(result)
        assert d["visible_turns"] == 0


class TestReplicateIndex:
    def test_emits_replicate_index_when_supplied(self):
        # Repeated runs share a task_id; the replicate index is what distinguishes
        # the rows so downstream consumers (evalboard) don't collapse them to one.
        result = _make_result(turns=[])
        assert eval_result_to_task_dict(result, replicate_index=2)["replicate_index"] == 2

    def test_replicate_index_defaults_to_none(self):
        result = _make_result(turns=[])
        assert eval_result_to_task_dict(result)["replicate_index"] is None


class TestTotalTurns:
    def test_emits_total_turns(self):
        result = _make_result(turns=[_turn(2), _turn(3), _turn(4)])
        d = eval_result_to_task_dict(result)
        assert d["total_turns"] == 9

    def test_handles_none(self):
        result = _make_result(turns=[_turn(None), _turn(3), _turn(None)])
        d = eval_result_to_task_dict(result)
        assert d["total_turns"] == 3

    def test_empty_turns(self):
        result = _make_result(turns=[])
        d = eval_result_to_task_dict(result)
        assert d["total_turns"] == 0


class TestExpectedTurnsKey:
    def test_emits_when_configured(self):
        result = _make_result(
            resolved={"run_limits": {"expected_turns": 12}},
            turns=[_turn(5)],
        )
        d = eval_result_to_task_dict(result)
        assert d["expected_turns"] == 12

    def test_none_when_unset(self):
        result = _make_result(
            resolved={"run_limits": {"max_turns": 10}},
            turns=[_turn(5)],
        )
        d = eval_result_to_task_dict(result)
        assert d["expected_turns"] is None

    def test_none_when_task_config_none(self):
        result = _make_result(task_config=False, turns=[_turn(5)])
        d = eval_result_to_task_dict(result)
        assert d["expected_turns"] is None

    def test_none_when_invalid_type(self):
        result = _make_result(
            resolved={"run_limits": {"expected_turns": "ten"}},
            turns=[_turn(5)],
        )
        d = eval_result_to_task_dict(result)
        assert d["expected_turns"] is None

    def test_none_when_zero(self):
        result = _make_result(
            resolved={"run_limits": {"expected_turns": 0}},
            turns=[_turn(5)],
        )
        d = eval_result_to_task_dict(result)
        assert d["expected_turns"] is None

    def test_none_when_run_limits_not_dict(self):
        result = _make_result(
            resolved={"run_limits": "not-a-dict"},
            turns=[_turn(5)],
        )
        d = eval_result_to_task_dict(result)
        assert d["expected_turns"] is None
