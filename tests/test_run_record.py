"""Characterization + layering tests for the run.json row serializer.

``eval_result_to_task_dict`` moved out of the experiment reporter into
``coder_eval.run_record`` — its *home* changed, its *body* did not. The snapshot
below was captured from the pre-move function, so any difference is a mistake in
the move rather than an intended change. It also pins the run.json key set, which
the evalboard and every archived run depend on.
"""

import ast
from datetime import datetime
from pathlib import Path

from coder_eval.models import (
    AgentKind,
    CommandTelemetry,
    EarlyStopInfo,
    EarlyStopReason,
    EvaluationResult,
    FinalStatus,
    ResultSummary,
    TaskConfigRecord,
    TokenUsage,
    TurnRecord,
)
from coder_eval.run_record import eval_result_to_task_dict


REPO_ROOT = Path(__file__).parent.parent


EXPECTED_ROW = {
    "actual_commands": None,
    "agent_config": None,
    "agent_cost_usd": 0.5,
    "cache_creation_input_tokens": 300,
    "cache_read_input_tokens": 400,
    "commands_efficiency": None,
    "cost_complete": True,
    "duration": 3.0,
    "early_stop_reason": None,
    "error_category": None,
    "error_message": None,
    "expected_commands": None,
    "expected_tool_calls": None,
    "expected_tool_calls_overage": None,
    "expected_turns": None,
    "expected_turns_overage": None,
    "gate_threshold": None,
    "generation_ms": None,
    "has_final_reply": False,
    "input_tokens": 100,
    "installed_tools": None,
    "iteration_count": 1,
    "iterations": [
        {
            "assistant_turn_count": 0,
            "command_count": 0,
            "crash_reason": None,
            "crashed": False,
            "duration_seconds": 2.5,
            "iteration": 1,
        }
    ],
    "judge_cost_usd": None,
    "tool_calls_exhausted": False,
    "model_turns": None,
    "model_used": "claude-haiku-4-5",
    "output_tokens": 200,
    "reference_similarity": None,
    "replicate_index": 2,
    "sdk_options": None,
    "simulator_cost_usd": None,
    "startup_ms": None,
    "status": "SUCCESS",
    "stopped_early": False,
    "tags": [],
    "task_id": "char-task",
    "task_path": None,
    "teardown_ms": None,
    "tool_calls_remaining_at_stop": None,
    "tool_ms": None,
    "total_cost_usd": 0.5,
    "total_tokens": 1000,
    "total_turns": 0,
    "variant_id": None,
    "visible_turns": 0,
    "weighted_score": 0.75,
}


def _result() -> EvaluationResult:
    usage = TokenUsage(
        uncached_input_tokens=100,
        output_tokens=200,
        cache_creation_input_tokens=300,
        cache_read_input_tokens=400,
        total_cost_usd=0.5,
    )
    turn = TurnRecord(iteration=1, user_input="u", agent_output="a", token_usage=usage, duration_seconds=2.5)
    return EvaluationResult(
        task_id="char-task",
        task_description="characterization",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime(2026, 1, 2, 3, 4, 5),
        final_status=FinalStatus.SUCCESS,
        iteration_count=1,
        iterations=[turn],
        weighted_score=0.75,
        model_used="claude-haiku-4-5",
        duration_seconds=3.0,
        total_token_usage=usage,
    )


class TestSerializerIsUnchangedByTheMove:
    def test_row_matches_the_pre_move_snapshot(self):
        assert eval_result_to_task_dict(_result(), replicate_index=2) == EXPECTED_ROW

    def test_the_run_json_key_set_is_unchanged(self):
        """Keys are the contract the evalboard and archived runs read."""
        assert set(eval_result_to_task_dict(_result()).keys()) == set(EXPECTED_ROW)

    def test_input_tokens_carries_the_uncached_slice_not_the_derived_total(self):
        """Same word, two quantities — the key is NOT TokenUsage.input_tokens."""
        row = eval_result_to_task_dict(_result())
        assert row["input_tokens"] == 100
        assert _result().total_token_usage.input_tokens == 800


class TestRunRecordIsNotInTheReportsLayer:
    """The whole point of the move — otherwise unobservable.

    Carrying the run.json serializer in the reports layer was the only reason
    ``orchestration/batch.py`` imported from ``reports*`` at all.
    """

    def test_module_imports_nothing_from_the_reports_layer(self):
        source = (REPO_ROOT / "src" / "coder_eval" / "run_record.py").read_text(encoding="utf-8")
        offenders = [
            node.module
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.module and "reports" in node.module
        ]
        assert not offenders, f"run_record must not import the reports layer, found: {offenders}"


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


def _turn_with_expected(n: int | None) -> TurnRecord:
    return TurnRecord(iteration=1, user_input="p", agent_output="a", num_turns=n)


def _visible_turn_with_expected(commands: int = 0, reply: str | None = None) -> TurnRecord:
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
        result = _make_result(turns=[_visible_turn_with_expected(commands=5, reply="done")])
        d = eval_result_to_task_dict(result)
        assert d["visible_turns"] == 6

    def test_no_reply_omits_plus_one(self):
        result = _make_result(turns=[_visible_turn_with_expected(commands=4), _visible_turn_with_expected(commands=5)])
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
        result = _make_result(turns=[_turn_with_expected(2), _turn_with_expected(3), _turn_with_expected(4)])
        d = eval_result_to_task_dict(result)
        assert d["total_turns"] == 9

    def test_handles_none(self):
        result = _make_result(turns=[_turn_with_expected(None), _turn_with_expected(3), _turn_with_expected(None)])
        d = eval_result_to_task_dict(result)
        assert d["total_turns"] == 3

    def test_empty_turns(self):
        result = _make_result(turns=[])
        d = eval_result_to_task_dict(result)
        assert d["total_turns"] == 0


class TestExpectedTurnsKey:
    def test_emits_when_configured(self):
        result = _make_result(
            resolved={"run_limits": {"expected_tool_calls": 12}},
            turns=[_turn_with_expected(5)],
        )
        d = eval_result_to_task_dict(result)
        assert d["expected_tool_calls"] == 12

    def test_none_when_unset(self):
        result = _make_result(
            resolved={"run_limits": {"max_tool_calls": 10}},
            turns=[_turn_with_expected(5)],
        )
        d = eval_result_to_task_dict(result)
        assert d["expected_tool_calls"] is None

    def test_none_when_task_config_none(self):
        result = _make_result(task_config=False, turns=[_turn_with_expected(5)])
        d = eval_result_to_task_dict(result)
        assert d["expected_tool_calls"] is None

    def test_row_carries_the_tool_call_and_model_turn_keys_and_none_of_the_historical_ones(self):
        result = _make_result(resolved={"run_limits": {"expected_turns": 12}}, turns=[_turn_with_expected(5)])
        d = eval_result_to_task_dict(result)
        assert {
            "tool_calls_exhausted",
            "tool_calls_remaining_at_stop",
            "expected_tool_calls",
            "expected_tool_calls_overage",
            "model_turns",
            "expected_turns",
            "expected_turns_overage",
        } <= d.keys()
        assert not {"max_turns_exhausted", "turns_remaining_at_stop"} & d.keys()
        assert d["expected_tool_calls"] is None

    def test_row_carries_the_model_turn_target_and_its_overage(self):
        result = _make_result(resolved={"run_limits": {"expected_turns": 3}})
        result.model_turns = 5
        d = eval_result_to_task_dict(result)
        assert d["model_turns"] == 5
        assert d["expected_turns"] == 3
        assert d["expected_turns_overage"] == [5, 3]

    def test_a_record_without_a_model_turn_count_carries_no_expected_turns(self):
        result = _make_result(resolved={"run_limits": {"expected_turns": 3}})
        d = eval_result_to_task_dict(result)
        assert d["model_turns"] is None
        assert d["expected_turns"] is None
        assert d["expected_turns_overage"] is None

    def test_row_carries_tool_calls_remaining_at_stop_from_early_stop(self):
        result = _make_result()
        result.early_stop = EarlyStopInfo(
            reason=EarlyStopReason.CRITERION_FAILED,
            deciding_criterion_type="file_exists",
            deciding_criterion_description="x",
            sdk_turn_index=2,
            tool_call_index=3,
            elapsed_seconds=1.0,
            tool_calls_remaining_at_stop=7,
        )
        d = eval_result_to_task_dict(result)
        assert d["tool_calls_remaining_at_stop"] == 7

    def test_none_when_invalid_type(self):
        result = _make_result(
            resolved={"run_limits": {"expected_tool_calls": "ten"}},
            turns=[_turn_with_expected(5)],
        )
        d = eval_result_to_task_dict(result)
        assert d["expected_tool_calls"] is None

    def test_none_when_zero(self):
        result = _make_result(
            resolved={"run_limits": {"expected_tool_calls": 0}},
            turns=[_turn_with_expected(5)],
        )
        d = eval_result_to_task_dict(result)
        assert d["expected_tool_calls"] is None

    def test_none_when_run_limits_not_dict(self):
        result = _make_result(
            resolved={"run_limits": "not-a-dict"},
            turns=[_turn_with_expected(5)],
        )
        d = eval_result_to_task_dict(result)
        assert d["expected_tool_calls"] is None
