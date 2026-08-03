"""Unit tests for coder_eval.reports_experiment.eval_result_to_task_dict."""

from datetime import datetime

from coder_eval.models import (
    AgentKind,
    CommandTelemetry,
    EvaluationResult,
    FinalStatus,
    IntegrityFinding,
    IntegrityFindingKind,
    IntegrityInfo,
    IntegrityMode,
    IntegrityVerdict,
    ResultSummary,
    TaskConfigRecord,
    TurnRecord,
)
from coder_eval.reports_experiment import _ROW_INTEGRITY_FINDINGS_MAX, eval_result_to_task_dict


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


class TestRowKeySet:
    """Pins the run.json row key set.

    ``eval_result_to_task_dict`` is a hand-maintained dict literal: a new field on
    ``EvaluationResult`` reaches ``task.json`` for free but is silently ABSENT
    from ``run.json``, which is what the evalboard and triage read. Nothing
    pinned this key set before (not even ``stopped_early``), so the omission was
    invisible. Adding a row key here is intentional; removing or renaming one is
    a breaking change for downstream consumers.
    """

    EXPECTED_KEYS = frozenset(
        {
            "task_id",
            "replicate_index",
            "status",
            "weighted_score",
            "duration",
            "iteration_count",
            "tags",
            "task_path",
            "iterations",
            "model_used",
            "reference_similarity",
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "total_tokens",
            "total_cost_usd",
            "agent_cost_usd",
            "cost_complete",
            "judge_cost_usd",
            "simulator_cost_usd",
            "error_message",
            "error_category",
            "expected_commands",
            "actual_commands",
            "commands_efficiency",
            "agent_config",
            "sdk_options",
            "installed_tools",
            "max_turns_exhausted",
            "expected_turns_overage",
            "total_turns",
            "visible_turns",
            "expected_turns",
            "has_final_reply",
            "stopped_early",
            "early_stop_reason",
            "turns_remaining_at_stop",
            "gate_threshold",
            "integrity_verdict",
            "integrity_voided",
            "integrity_findings",
            "variant_id",
        }
    )

    def test_key_set_is_exactly_as_pinned(self):
        d = eval_result_to_task_dict(_make_result(turns=[_turn(5)]))
        assert set(d) == self.EXPECTED_KEYS


class TestIntegrityKeys:
    def test_defaults_report_a_skipped_untainted_row(self):
        d = eval_result_to_task_dict(_make_result(turns=[_turn(5)]))
        assert d["integrity_verdict"] == "skipped"
        assert d["integrity_voided"] is False
        assert d["integrity_findings"] == []

    def test_verdict_and_findings_reach_the_row(self):
        result = _make_result(turns=[_turn(5)])
        result.integrity = IntegrityInfo(
            verdict=IntegrityVerdict.TAINTED,
            mode=IntegrityMode.VOID,
            voided=True,
            findings=[
                IntegrityFinding(
                    kind=IntegrityFindingKind.GRADED_READ,
                    detail="read RESOLUTION.md",
                    iteration=1,
                    command_index=3,
                    tool_name="Bash",
                    evidence="cat RESOLUTION.md",
                )
            ],
        )
        d = eval_result_to_task_dict(result)
        assert d["integrity_verdict"] == "tainted"
        assert d["integrity_voided"] is True
        assert d["integrity_findings"] == ["graded_read: read RESOLUTION.md"]

    def test_findings_are_capped(self):
        result = _make_result(turns=[_turn(5)])
        result.integrity = IntegrityInfo(
            verdict=IntegrityVerdict.TAINTED,
            findings=[IntegrityFinding(kind=IntegrityFindingKind.GRADED_READ, detail=f"hit {i}") for i in range(20)],
        )
        d = eval_result_to_task_dict(result)
        assert len(d["integrity_findings"]) == _ROW_INTEGRITY_FINDINGS_MAX
