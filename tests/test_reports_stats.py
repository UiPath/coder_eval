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
from coder_eval.reports_stats import has_final_reply, visible_turn_count


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
