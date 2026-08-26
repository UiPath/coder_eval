"""Tests for the actual-cost join (litellm_cost.py) + the orchestrator hook.

Covers loading the proxy's per-call JSONL and stitching real OpenRouter cost +
per-call cache onto a run's turns, incl. the retry no-double-count rule and the
whole-turn fallback to static pricing.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import coder_eval.orchestrator as orch_mod
from coder_eval.litellm_cost import apply_actual_cost, load_cost_records
from coder_eval.models import (
    AgentKind,
    AssistantMessage,
    DirectRoute,
    EvaluationResult,
    LiteLLMRoute,
    ReconciliationMessage,
    TokenUsage,
    TurnRecord,
)
from coder_eval.telemetry import hash_identifier


def _turn(iteration: int, static_cost: float | None = None) -> TurnRecord:
    usage = TokenUsage(uncached_input_tokens=100, output_tokens=10, total_cost_usd=static_cost)
    return TurnRecord(iteration=iteration, user_input="u", agent_output="a", duration_seconds=1.0, token_usage=usage)


def _result(turns: list[TurnRecord]) -> EvaluationResult:
    return EvaluationResult(
        task_id="t",
        task_description="d",
        variant_id="v",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime(2026, 7, 29),
        final_status="SUCCESS",
        iteration_count=len(turns),
        environment_info={},
        iterations=turns,
    )


def _rec(iteration: int, cost: float | None, *, run_id="R", task_id="T", cache_read=0, call_id="gen"):
    return {
        "run_id": run_id,
        "task_id": task_id,
        "iteration": str(iteration),
        "call_id": call_id,
        "cost": cost,
        "input": 100,
        "cache_read": cache_read,
        "cache_write": 0,
        "output": 10,
    }


class TestLoadCostRecords:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_cost_records(tmp_path / "nope.jsonl") == []

    def test_skips_blank_and_garbled_and_non_dict_lines(self, tmp_path):
        p = tmp_path / "c.jsonl"
        p.write_text('{"run_id":"R","cost":0.01}\n\n   \n{bad json\n[1,2,3]\n{"run_id":"R2"}\n', encoding="utf-8")
        recs = load_cost_records(p)
        assert len(recs) == 2
        assert recs[0]["run_id"] == "R" and recs[1]["run_id"] == "R2"


class TestApplyActualCost:
    def test_overrides_per_turn_cost_and_attaches_calls(self):
        result = _result([_turn(0, static_cost=0.5), _turn(1, static_cost=0.5)])
        records = [
            _rec(0, 0.01, call_id="g0a", cache_read=64),
            _rec(0, 0.02, call_id="g0b"),
            _rec(1, 0.05, call_id="g1"),
        ]
        applied = apply_actual_cost(result, run_id="R", task_id="T", records=records)
        assert applied == 2
        assert result.iterations[0].token_usage.total_cost_usd == 0.03  # 0.01 + 0.02, not the 0.5 estimate
        assert result.iterations[1].token_usage.total_cost_usd == 0.05
        # Per-call breakdown attached: 2 calls on turn 0, 1 on turn 1.
        assert len(result.iterations[0].provider_call_costs) == 2
        assert result.iterations[0].provider_call_costs[0].cache_read_tokens == 64
        assert result.iterations[1].provider_call_costs[0].cost_usd == 0.05

    def test_no_matching_records_is_noop_and_keeps_static(self):
        result = _result([_turn(0, static_cost=0.5)])
        # run_id mismatch → nothing applied, static estimate intact.
        applied = apply_actual_cost(result, run_id="R", task_id="T", records=[_rec(0, 0.01, run_id="OTHER")])
        assert applied == 0
        assert result.iterations[0].token_usage.total_cost_usd == 0.5
        assert result.iterations[0].provider_call_costs == []

    def test_unmatched_iteration_keeps_static_for_that_turn_only(self):
        result = _result([_turn(0, static_cost=0.5), _turn(1, static_cost=0.7)])
        applied = apply_actual_cost(result, run_id="R", task_id="T", records=[_rec(0, 0.01)])
        assert applied == 1
        assert result.iterations[0].token_usage.total_cost_usd == 0.01  # joined
        assert result.iterations[1].token_usage.total_cost_usd == 0.7  # static fallback

    def test_retry_credits_survivor_and_zeroes_sibling_no_double_count(self):
        # Two turns share iteration 0 (a crashed attempt + its retry). The
        # iteration's calls must be credited ONCE (to the last/survivor turn).
        result = _result([_turn(0, static_cost=0.5), _turn(0, static_cost=0.5)])
        records = [_rec(0, 0.01, call_id="a"), _rec(0, 0.02, call_id="b")]
        apply_actual_cost(result, run_id="R", task_id="T", records=records)
        assert result.iterations[1].token_usage.total_cost_usd == 0.03  # survivor gets the sum
        assert result.iterations[0].token_usage.total_cost_usd == 0.0  # sibling zeroed
        assert result.iterations[0].provider_call_costs == []
        assert len(result.iterations[1].provider_call_costs) == 2
        # Run aggregate re-derives to exactly the real total — no double count.
        run_total = sum(t.token_usage.total_cost_usd for t in result.iterations)
        assert run_total == 0.03

    def test_creates_token_usage_when_absent(self):
        turn = TurnRecord(iteration=0, user_input="u", agent_output="a", duration_seconds=1.0, token_usage=None)
        result = _result([turn])
        apply_actual_cost(result, run_id="R", task_id="T", records=[_rec(0, 0.04)])
        assert result.iterations[0].token_usage is not None
        assert result.iterations[0].token_usage.total_cost_usd == 0.04

    def test_unpriced_call_keeps_static_and_attaches_nothing(self):
        # A record with no cost (cost=None) must NOT override the static estimate
        # (that would bill it at $0 and understate the turn) and must NOT attach a
        # misleading breakdown — the turn falls back to static, loudly (warned).
        result = _result([_turn(0, static_cost=0.5)])
        applied = apply_actual_cost(result, run_id="R", task_id="T", records=[_rec(0, None)])
        assert applied == 0
        assert result.iterations[0].token_usage.total_cost_usd == 0.5
        assert result.iterations[0].provider_call_costs == []

    def test_partial_coverage_keeps_static_for_that_turn(self):
        # One priced call + one unpriced call on the same turn: overriding would bill
        # the unpriced call at $0, so the whole turn keeps its static estimate.
        result = _result([_turn(0, static_cost=0.42)])
        records = [_rec(0, 0.01, call_id="a"), _rec(0, None, call_id="b")]
        applied = apply_actual_cost(result, run_id="R", task_id="T", records=records)
        assert applied == 0
        assert result.iterations[0].token_usage.total_cost_usd == 0.42
        assert result.iterations[0].provider_call_costs == []

    def test_attempt_nonce_prevents_rerun_double_count(self):
        # The append-only log holds a PRIOR attempt's rows AND this attempt's rows
        # under the same (run_id, task_id, iteration). The attempt nonce scopes the
        # join to THIS attempt, so cost is not summed across both (reproduced the
        # $0.06-for-$0.03 double-count the review flagged).
        result = _result([_turn(0, static_cost=0.5)])
        records = [
            {**_rec(0, 0.03, call_id="old"), "attempt": "prev"},
            {**_rec(0, 0.03, call_id="new"), "attempt": "curr"},
        ]
        applied = apply_actual_cost(result, run_id="R", task_id="T", attempt="curr", records=records)
        assert applied == 1
        assert result.iterations[0].token_usage.total_cost_usd == 0.03  # this attempt only, not 0.06
        assert [c.call_id for c in result.iterations[0].provider_call_costs] == ["new"]

    def test_transactional_no_mutation_when_a_record_is_malformed(self):
        # A well-formed JSON row with a wrong-typed cost raises while building the
        # per-call breakdown. The whole join must abort with NO turn mutated (the
        # caller keeps static pricing), not leave a half-joined run.
        result = _result([_turn(0, static_cost=0.5), _turn(1, static_cost=0.7)])
        good = _rec(0, 0.03)
        bad = {**_rec(1, 0.0), "cost": {"not": "a number"}}
        with pytest.raises(ValidationError):
            apply_actual_cost(result, run_id="R", task_id="T", records=[good, bad])
        # Byte-identical to the pre-join state: neither turn was overridden.
        assert result.iterations[0].token_usage.total_cost_usd == 0.5
        assert result.iterations[1].token_usage.total_cost_usd == 0.7
        assert result.iterations[0].provider_call_costs == []

    def test_credits_gen_bearing_turn_not_trailing_empty_turn(self):
        # Two TurnRecords share iteration=1 (seen on multi-turn runs): the real
        # agent turn WITH generations, and a trailing empty model=None turn. The
        # calls must be credited to the turn that generated, not the empty one.
        real = _turn_msgs(
            1,
            [_asst("m1", output=10), _asst("m2", output=20), ReconciliationMessage()],
            uncached=1500,
            cache_read=0,
            output=30,
        )
        empty = TurnRecord(iteration=1, user_input="u", agent_output="", duration_seconds=1.0, token_usage=TokenUsage())
        result = _result([real, empty])
        records = [_call_rec(1, 0.01, 600, 0, "g1", out=10), _call_rec(1, 0.02, 900, 0, "g2", out=20)]
        apply_actual_cost(result, run_id="R", task_id="T", records=records)
        # Credited to the REAL (generation-bearing) turn, not the trailing empty one.
        assert len(real.provider_call_costs) == 2
        assert real.token_usage.total_cost_usd == 0.03
        assert empty.provider_call_costs == []  # empty turn stranded nothing
        assert empty.token_usage.total_cost_usd == 0.0

    def test_degenerate_no_usage_call_is_ignored(self):
        # A degenerate call with NO cost AND no tokens (a null record some providers
        # emit) must not revert an otherwise-priced turn to static; it is dropped.
        result = _result([_turn(0, static_cost=0.5)])
        good = _rec(0, 0.03, call_id="real")
        degenerate = {**_rec(0, None, call_id="null"), "input": 0, "output": 0}
        applied = apply_actual_cost(result, run_id="R", task_id="T", records=[good, degenerate])
        assert applied == 1
        assert result.iterations[0].token_usage.total_cost_usd == 0.03  # priced, degenerate ignored
        assert [c.call_id for c in result.iterations[0].provider_call_costs] == ["real"]

    def test_retry_unpriced_survivor_does_not_zero_the_sibling(self):
        # Regression: if the survivor falls back to static (an unpriced usage call),
        # the crashed sibling must NOT be zeroed first — otherwise the iteration's
        # spend is silently dropped. Both keep their static estimate.
        result = _result([_turn(0, static_cost=0.5), _turn(0, static_cost=0.6)])
        records = [_rec(0, 0.01, call_id="a"), _rec(0, None, call_id="b")]  # b has usage but no cost
        applied = apply_actual_cost(result, run_id="R", task_id="T", records=records)
        assert applied == 0
        assert result.iterations[0].token_usage.total_cost_usd == 0.5  # sibling NOT zeroed
        assert result.iterations[1].token_usage.total_cost_usd == 0.6  # survivor stays static
        assert result.iterations[1].provider_call_costs == []

    def test_orphaned_iteration_records_are_warned(self, caplog):
        # A cost record tagged with an iteration NO turn has (e.g. a stale log) is
        # surfaced as orphaned spend, not silently ignored; the real turn still joins.
        import logging

        result = _result([_turn(0, static_cost=0.5)])
        records = [_rec(0, 0.02, call_id="a"), _rec(5, 0.09, call_id="orphan")]  # iteration 5: no turn
        with caplog.at_level(logging.WARNING):
            applied = apply_actual_cost(result, run_id="R", task_id="T", records=records)
        assert applied == 1
        assert result.iterations[0].token_usage.total_cost_usd == 0.02
        assert "matched no turn" in caplog.text and "'5'" in caplog.text


class TestOrchestratorJoinHook:
    """The orchestrator method is called as an unbound function on a minimal
    fake self, so the branch is covered without standing up a full Orchestrator."""

    def test_noop_off_litellm_route(self):
        fake = SimpleNamespace(route=DirectRoute(), result=_result([_turn(0, 0.5)]))
        orch_mod.Orchestrator._join_litellm_actual_cost(fake)  # must not touch anything
        assert fake.result.iterations[0].token_usage.total_cost_usd == 0.5

    def test_joins_on_litellm_route(self, tmp_path, monkeypatch):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        log = tmp_path / "costs.jsonl"
        run_id = hash_identifier(run_dir.as_posix())
        log.write_text(
            f'{{"run_id":"{run_id}","task_id":"calc","iteration":"0","attempt":"att1","cost":0.09,"cache_read":5}}\n'
        )
        monkeypatch.setattr(orch_mod.settings, "litellm_cost_log", str(log))
        fake = SimpleNamespace(
            route=LiteLLMRoute(base_url="http://x:4000", model="deepseek/deepseek-v4-pro"),
            result=_result([_turn(0, static_cost=0.5)]),
            _cost_correlation_run_id=run_id,
            _cost_attempt_nonce="att1",
            _log_task_id="calc",
        )
        orch_mod.Orchestrator._join_litellm_actual_cost(fake)
        assert fake.result.iterations[0].token_usage.total_cost_usd == 0.09  # static 0.5 overridden
        assert fake.result.iterations[0].provider_call_costs[0].cache_read_tokens == 5

    def test_join_never_raises_on_bad_log(self, tmp_path, monkeypatch):
        monkeypatch.setattr(orch_mod.settings, "litellm_cost_log", str(tmp_path / "does-not-exist.jsonl"))
        fake = SimpleNamespace(
            route=LiteLLMRoute(base_url="http://x:4000"),
            result=_result([_turn(0, static_cost=0.5)]),
            _cost_correlation_run_id="R",
            _cost_attempt_nonce="att1",
            _log_task_id="calc",
        )
        orch_mod.Orchestrator._join_litellm_actual_cost(fake)  # missing file → no-op, no raise
        assert fake.result.iterations[0].token_usage.total_cost_usd == 0.5

    def test_run_total_rederives_from_actual_after_join(self, tmp_path, monkeypatch):
        # The join must run BEFORE aggregation so the run-level total sums the
        # corrected per-turn costs, not the static estimate. Two turns, static 0.5
        # each (Σ 1.0); the log books actuals 0.03 + 0.05 (Σ 0.08). If the ordering
        # ever reversed, aggregation would sum the stale static costs and this fails.
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        log = tmp_path / "costs.jsonl"
        run_id = hash_identifier(run_dir.as_posix())
        log.write_text(
            f'{{"run_id":"{run_id}","task_id":"calc","iteration":"0","attempt":"att1","cost":0.03}}\n'
            f'{{"run_id":"{run_id}","task_id":"calc","iteration":"1","attempt":"att1","cost":0.05}}\n'
        )
        monkeypatch.setattr(orch_mod.settings, "litellm_cost_log", str(log))
        fake = SimpleNamespace(
            route=LiteLLMRoute(base_url="http://x:4000", model="deepseek/deepseek-v4-pro"),
            result=_result([_turn(0, static_cost=0.5), _turn(1, static_cost=0.5)]),
            _cost_correlation_run_id=run_id,
            _cost_attempt_nonce="att1",
            _log_task_id="calc",
        )
        orch_mod.Orchestrator._join_litellm_actual_cost(fake)
        orch_mod.Orchestrator._aggregate_token_usage(fake)
        assert fake.result.total_token_usage.total_cost_usd == pytest.approx(0.08)  # Σ actual, not Σ static


def _asst(message_id, output=5):
    return AssistantMessage(
        started_at=datetime(2026, 7, 29),
        completed_at=datetime(2026, 7, 29),
        generation_duration_ms=1.0,
        message_id=message_id,
        output_tokens=output,
    )


def _turn_msgs(iteration, messages, *, uncached, cache_read, output, static_cost=0.5):
    tu = TokenUsage(
        uncached_input_tokens=uncached,
        cache_read_input_tokens=cache_read,
        output_tokens=output,
        total_cost_usd=static_cost,
    )
    return TurnRecord(
        iteration=iteration, user_input="u", agent_output="a", duration_seconds=1.0, token_usage=tu, messages=messages
    )


def _call_rec(iteration, cost, inp, cache_read, call_id, out=5):
    return {
        "run_id": "R",
        "task_id": "T",
        "iteration": str(iteration),
        "call_id": call_id,
        "cost": cost,
        "input": inp,
        "cache_read": cache_read,
        "cache_write": 0,
        "output": out,
    }
