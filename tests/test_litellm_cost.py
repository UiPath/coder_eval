"""Tests for the actual-cost join (litellm_cost.py) + the orchestrator hook.

Covers loading the proxy's per-call JSONL and stitching real OpenRouter cost +
per-call cache onto a run's turns, incl. the retry no-double-count rule and the
whole-turn fallback to static pricing.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

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
from coder_eval.orchestrator import Orchestrator
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

    def test_tag_only_records_attach_calls_but_keep_static_cost(self):
        # A record with no cost (cost=None) still attaches the call, but must NOT
        # wipe the static estimate to None.
        result = _result([_turn(0, static_cost=0.5)])
        apply_actual_cost(result, run_id="R", task_id="T", records=[_rec(0, None)])
        assert result.iterations[0].token_usage.total_cost_usd == 0.5
        assert len(result.iterations[0].provider_call_costs) == 1

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
        a1, a2, _recon = real.messages
        assert (a1.cost_usd, a2.cost_usd) == (0.01, 0.02)  # distributed onto the REAL turn
        assert len(real.provider_call_costs) == 2
        assert real.token_usage.total_cost_usd == 0.03
        assert empty.provider_call_costs == []  # empty turn stranded nothing
        assert empty.token_usage.total_cost_usd == 0.0


class TestOrchestratorJoinHook:
    """The orchestrator method is called as an unbound function on a minimal
    fake self, so the branch is covered without standing up a full Orchestrator."""

    def test_noop_off_litellm_route(self):
        fake = SimpleNamespace(route=DirectRoute(), result=_result([_turn(0, 0.5)]))
        Orchestrator._join_litellm_actual_cost(fake)  # must not touch anything
        assert fake.result.iterations[0].token_usage.total_cost_usd == 0.5

    def test_joins_on_litellm_route(self, tmp_path, monkeypatch):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        log = tmp_path / "costs.jsonl"
        run_id = hash_identifier(run_dir.as_posix())
        log.write_text(f'{{"run_id":"{run_id}","task_id":"calc","iteration":"0","cost":0.09,"cache_read":5}}\n')
        monkeypatch.setattr(orch_mod.settings, "litellm_cost_log", str(log))
        fake = SimpleNamespace(
            route=LiteLLMRoute(base_url="http://x:4000", auth_token="k", model="deepseek/deepseek-v4-pro"),
            result=_result([_turn(0, static_cost=0.5)]),
            run_dir=run_dir,
            _log_task_id="calc",
        )
        Orchestrator._join_litellm_actual_cost(fake)
        assert fake.result.iterations[0].token_usage.total_cost_usd == 0.09  # static 0.5 overridden
        assert fake.result.iterations[0].provider_call_costs[0].cache_read_tokens == 5

    def test_join_never_raises_on_bad_log(self, tmp_path, monkeypatch):
        monkeypatch.setattr(orch_mod.settings, "litellm_cost_log", str(tmp_path / "does-not-exist.jsonl"))
        fake = SimpleNamespace(
            route=LiteLLMRoute(base_url="http://x:4000", auth_token="k"),
            result=_result([_turn(0, static_cost=0.5)]),
            run_dir=tmp_path,
            _log_task_id="calc",
        )
        Orchestrator._join_litellm_actual_cost(fake)  # missing file → no-op, no raise
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
            f'{{"run_id":"{run_id}","task_id":"calc","iteration":"0","cost":0.03}}\n'
            f'{{"run_id":"{run_id}","task_id":"calc","iteration":"1","cost":0.05}}\n'
        )
        monkeypatch.setattr(orch_mod.settings, "litellm_cost_log", str(log))
        fake = SimpleNamespace(
            route=LiteLLMRoute(base_url="http://x:4000", auth_token="k", model="deepseek/deepseek-v4-pro"),
            result=_result([_turn(0, static_cost=0.5), _turn(1, static_cost=0.5)]),
            run_dir=run_dir,
            _log_task_id="calc",
        )
        Orchestrator._join_litellm_actual_cost(fake)
        Orchestrator._aggregate_token_usage(fake)
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


class TestDistributeOntoMessages:
    def test_matches_generations_to_calls_by_output_and_drains_reconcile(self):
        # Generations matched to calls by output_tokens (10 → g1, 20 → g2).
        msgs = [_asst("m1", output=10), _asst("m2", output=20), ReconciliationMessage()]
        turn = _turn_msgs(1, msgs, uncached=1904, cache_read=4096, output=30)
        # g2 has input 5000 total, 4096 cached → 904 uncached.
        records = [_call_rec(1, 0.01, 1000, 0, "g1", out=10), _call_rec(1, 0.02, 5000, 4096, "g2", out=20)]
        apply_actual_cost(_result([turn]), run_id="R", task_id="T", records=records)
        a1, a2, recon = turn.messages
        assert (a1.input_tokens, a1.cache_read_tokens, a1.cost_usd) == (1000, 0, 0.01)
        assert (a2.input_tokens, a2.cache_read_tokens, a2.cost_usd) == (904, 4096, 0.02)
        assert (recon.input_tokens, recon.cache_read_tokens) == (0, 0)  # drained
        assert recon.cost_usd == 0.0  # total 0.03 minus (0.01 + 0.02)
        # Reconciliation invariant preserved: buckets still sum to token_usage.
        assert a1.input_tokens + a2.input_tokens + recon.input_tokens == 1904
        assert a1.cache_read_tokens + a2.cache_read_tokens + recon.cache_read_tokens == 4096

    def test_content_block_split_matches_on_summed_output(self):
        # One generation split into 2 content-block emissions (shared message_id):
        # its summed output (3+4=7) matches the call's output.
        msgs = [_asst("m1", output=3), _asst("m1", output=4), ReconciliationMessage()]
        turn = _turn_msgs(1, msgs, uncached=1000, cache_read=0, output=7)
        apply_actual_cost(_result([turn]), run_id="R", task_id="T", records=[_call_rec(1, 0.05, 1000, 0, "g1", out=7)])
        a1, a2, recon = turn.messages
        assert (a1.input_tokens, a1.cost_usd) == (1000, 0.05)  # rep carries the call
        assert (a2.input_tokens, a2.cost_usd) == (0, None)  # sibling zeroed (no double count)
        assert recon.input_tokens == 0

    def test_aux_call_unmatched_goes_to_reconcile(self):
        # 2 generations (out 10, 20) + an unpaired auxiliary small-model call (out 5).
        # The mains match by output; the aux matches no generation → reconcile.
        msgs = [_asst("m1", output=10), _asst("m2", output=20), ReconciliationMessage()]
        turn = _turn_msgs(1, msgs, uncached=1050, cache_read=500, output=35)
        records = [
            _call_rec(1, 0.001, 50, 0, "aux", out=5),
            _call_rec(1, 0.01, 600, 0, "g1", out=10),
            _call_rec(1, 0.02, 900, 500, "g2", out=20),
        ]
        apply_actual_cost(_result([turn]), run_id="R", task_id="T", records=records)
        a1, a2, recon = turn.messages
        assert (a1.input_tokens, a1.cost_usd) == (600, 0.01)  # matched g1
        assert (a2.input_tokens, a2.cache_read_tokens, a2.cost_usd) == (400, 500, 0.02)  # matched g2 (900-500)
        # The aux call lands in reconcile: its uncached input (50) + its cost (0.001).
        assert recon.input_tokens == 50
        assert recon.cache_read_tokens == 0
        assert recon.cost_usd == pytest.approx(0.001)
        assert turn.token_usage.total_cost_usd == 0.031

    def test_bails_to_reconcile_when_order_diverges(self):
        # Sub-agent-style: generations and calls carry the same outputs but in a
        # DIFFERENT order (gens 10,20 vs calls 20,10). The order-respecting walk
        # can't bind every generation cleanly → it refuses to guess and leaves the
        # stream sparse, with the reconcile row carrying the real total.
        msgs = [_asst("m1", output=10), _asst("m2", output=20), ReconciliationMessage()]
        turn = _turn_msgs(1, msgs, uncached=1000, cache_read=0, output=30)
        records = [_call_rec(1, 0.02, 900, 0, "g2", out=20), _call_rec(1, 0.01, 600, 0, "g1", out=10)]
        apply_actual_cost(_result([turn]), run_id="R", task_id="T", records=records)
        a1, a2, recon = turn.messages
        assert (a1.cost_usd, a2.cost_usd) == (None, None)  # not guessed
        assert recon.cost_usd == pytest.approx(0.03)  # whole real total on reconcile
        assert turn.token_usage.total_cost_usd == pytest.approx(0.03)

    def test_missing_message_id_leaves_sparse_but_reconciles_cost(self):
        msgs = [_asst(None), ReconciliationMessage(input_tokens=1000)]
        turn = _turn_msgs(1, msgs, uncached=1000, cache_read=0, output=5)
        apply_actual_cost(_result([turn]), run_id="R", task_id="T", records=[_call_rec(1, 0.01, 1000, 0, "g1")])
        a1, recon = turn.messages
        assert a1.input_tokens == 0 and recon.input_tokens == 1000  # untouched (can't group)
        assert recon.cost_usd == 0.01  # cost still surfaced on the reconcile row

    def test_cost_less_run_leaves_reconcile_cost_none(self):
        # A call with no reported cost on a turn whose total_cost_usd is None: the
        # reconcile row's cost stays None (not 0.0), while token buckets still reconcile.
        msgs = [_asst("m1", output=5), ReconciliationMessage()]
        turn = _turn_msgs(1, msgs, uncached=1000, cache_read=0, output=5, static_cost=None)
        apply_actual_cost(_result([turn]), run_id="R", task_id="T", records=[_call_rec(1, None, 1000, 0, "g1", out=5)])
        a1, recon = turn.messages
        assert recon.cost_usd is None  # no cost to reconcile
        assert a1.input_tokens + recon.input_tokens == 1000  # buckets still reconcile

    def test_no_reconciliation_row_still_costs_the_turn(self):
        # A turn with generations but no ReconciliationMessage: distribution runs and
        # hits the reconciliation-is-None early return, yet the turn still gets its
        # real cost and the per-call breakdown.
        turn = _turn_msgs(1, [_asst("m1", output=5)], uncached=1000, cache_read=0, output=5)
        apply_actual_cost(_result([turn]), run_id="R", task_id="T", records=[_call_rec(1, 0.02, 1000, 0, "g1", out=5)])
        (a1,) = turn.messages
        assert turn.token_usage.total_cost_usd == 0.02
        assert turn.provider_call_costs[0].cost_usd == 0.02
        assert a1.cost_usd == 0.02  # distributed onto the generation
