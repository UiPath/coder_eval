"""Unit tests for shared report helpers in coder_eval.reports_stats."""

from datetime import datetime

from coder_eval.analysis import calculate_command_statistics
from coder_eval.models import (
    AgentKind,
    CommandTelemetry,
    EvaluationResult,
    FinalStatus,
    ResultSummary,
    TaskConfigRecord,
    TurnRecord,
)
from coder_eval.reports_stats import expected_turns_overage, has_final_reply, visible_turn_count


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


def _turn(commands: int = 0, reply: str | None = None) -> TurnRecord:
    """Build a TurnRecord with `commands` tool calls and an optional final reply."""
    return TurnRecord(
        iteration=1,
        user_input="p",
        agent_output="a",
        commands=[_cmd(i) for i in range(commands)],
        result_summary=(ResultSummary(is_error=False, subtype="success", result=reply) if reply is not None else None),
    )


class TestExpectedTurnsOverage:
    def test_strict_greater_than(self):
        # 5 tools + reply = 6 visible turns. Budget 6 → no overage (equal).
        result = _make_result(
            resolved={"run_limits": {"expected_turns": 6}},
            turns=[_turn(commands=5, reply="done")],
        )
        assert expected_turns_overage(result) is None

        # 5 tools + reply = 6 visible turns. Budget 5 → overage (6 > 5).
        result = _make_result(
            resolved={"run_limits": {"expected_turns": 5}},
            turns=[_turn(commands=5, reply="done")],
        )
        assert expected_turns_overage(result) == (6, 5)

    def test_missing_reply_skipped(self):
        # Tools across multiple iterations sum correctly; absent reply
        # contributes nothing (no +1).
        result = _make_result(
            resolved={"run_limits": {"expected_turns": 5}},
            turns=[_turn(commands=4), _turn(commands=5)],
        )
        assert expected_turns_overage(result) == (9, 5)

    def test_task_config_none(self):
        result = _make_result(task_config=False, turns=[_turn(commands=10)])
        assert expected_turns_overage(result) is None

    def test_run_limits_missing(self):
        result = _make_result(resolved={}, turns=[_turn(commands=10)])
        assert expected_turns_overage(result) is None

    def test_expected_turns_unset(self):
        result = _make_result(resolved={"run_limits": {"max_turns": 10}}, turns=[_turn(commands=20)])
        assert expected_turns_overage(result) is None

    def test_invalid_expected_type(self):
        result = _make_result(resolved={"run_limits": {"expected_turns": "ten"}}, turns=[_turn(commands=20)])
        assert expected_turns_overage(result) is None

    def test_expected_turns_zero_treated_as_invalid(self):
        # Defensive: the model enforces ge=1, but a hand-rolled task.json could
        # still inject 0 — the helper must treat it as a disabled check.
        result = _make_result(resolved={"run_limits": {"expected_turns": 0}}, turns=[_turn(commands=10)])
        assert expected_turns_overage(result) is None

    def test_run_limits_not_a_dict(self):
        result = _make_result(resolved={"run_limits": "not-a-dict"}, turns=[_turn(commands=10)])
        assert expected_turns_overage(result) is None

    def test_empty_turns(self):
        result = _make_result(resolved={"run_limits": {"expected_turns": 1}}, turns=[])
        assert expected_turns_overage(result) is None


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
        result = _make_result(turns=[_turn(commands=2), _turn(commands=3, reply="done")])
        stats = calculate_command_statistics(result.iterations)
        assert visible_turn_count(result) == stats.total_commands + (1 if has_final_reply(result) else 0)

    def test_tools_without_final_reply(self):
        # Crashed before producing a reply: the +1 must be omitted on both sides.
        result = _make_result(turns=[_turn(commands=4)])
        stats = calculate_command_statistics(result.iterations)
        assert visible_turn_count(result) == stats.total_commands + (1 if has_final_reply(result) else 0)
