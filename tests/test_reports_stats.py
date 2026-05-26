"""Unit tests for shared report helpers in coder_eval.reports_stats."""

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
from coder_eval.reports_stats import expected_turns_overage


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
