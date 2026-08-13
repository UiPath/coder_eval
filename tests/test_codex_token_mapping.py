"""SDK-free coverage for the Codex token-mapping contract.

``tests/test_codex_agent.py`` is gated behind ``importorskip("openai_codex")``,
so in CI (where the Codex SDK is absent) none of the Codex token math runs — yet
this is exactly where the uncached/derived-input contract is defined for Codex
(fresh input = ``input - cached``, ``cache_creation = 0`` because OpenAI bills no
separate cache-write fee). ``_fresh_input_tokens`` lives at module scope and the
Codex SDK is imported lazily inside methods, so it (and the resulting
``TokenUsage`` shape) can be exercised without the SDK installed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from coder_eval.agents.codex_agent import _fresh_input_tokens, _ThreadTotals
from coder_eval.models import TokenUsage


class TestFreshInputTokens:
    """``fresh = max(input - cached, 0)`` — the uncached slice for Codex."""

    @pytest.mark.parametrize(
        ("raw_input", "cached", "expected"),
        [
            (1000, 250, 750),  # typical: prefix served from cache
            (250, 250, 0),  # whole prompt cached → no fresh input
            (100, 250, 0),  # clamped: cached can't exceed input
            (500, 0, 500),  # no cache → all input is fresh
            (0, 0, 0),  # empty
        ],
    )
    def test_fresh_slice(self, raw_input: int, cached: int, expected: int):
        assert _fresh_input_tokens(raw_input, cached) == expected

    def test_never_negative(self):
        # Even with absurd over-reporting of cached, the fresh slice floors at 0.
        assert _fresh_input_tokens(10, 10_000) == 0


class TestCodexTokenUsageContract:
    """The TokenUsage shape Codex builds from a per-generation token_count."""

    def test_fresh_is_uncached_and_cache_creation_is_zero(self):
        # Codex maps: uncached = input - cached, cache_creation = 0 (no write fee),
        # cache_read = cached. The derived total folds them back to the full prompt.
        raw_input, cached, output = 1000, 250, 80
        tu = TokenUsage(
            uncached_input_tokens=_fresh_input_tokens(raw_input, cached),
            output_tokens=output,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=cached,
        )
        assert tu.uncached_input_tokens == 750
        assert tu.cache_creation_input_tokens == 0
        assert tu.cache_read_input_tokens == 250
        # Derived total = uncached + cache_creation + cache_read = the full prompt.
        assert tu.input_tokens == 1000
        # Cost bills the uncached slice, never the derived total.
        assert tu.uncached_input_tokens < tu.input_tokens


def _sdk_usage(input_tokens: int, output_tokens: int, cached: int):
    """A stand-in for the SDK's ThreadTokenUsage — only ``.total`` is read."""
    return SimpleNamespace(
        total=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached,
        )
    )


def _agent():
    from coder_eval.agents.codex_agent import CodexAgent
    from coder_eval.models import CodexAgentConfig

    return CodexAgent(CodexAgentConfig(type="codex", model="gpt-5.6-terra"))


class TestThreadTotals:
    """``since`` turns the thread-cumulative snapshot into a per-turn delta."""

    def test_delta_against_baseline(self):
        assert _ThreadTotals(1000, 100, 400).since(_ThreadTotals(600, 40, 250)) == _ThreadTotals(400, 60, 150)

    def test_first_turn_is_the_whole_snapshot(self):
        assert _ThreadTotals(1000, 100, 400).since(_ThreadTotals()) == _ThreadTotals(1000, 100, 400)

    def test_backwards_total_means_a_restarted_thread(self):
        # A fresh thread counts from zero, so the snapshot is already turn-local.
        # Returning it whole beats clamping every bucket to 0 and losing the turn.
        restarted = _ThreadTotals(300, 20, 100)
        assert restarted.since(_ThreadTotals(9000, 800, 5000)) == restarted


class TestPerTurnUsageFromCumulativeSdkTotal:
    """The Codex thread is created once per task and reused for every turn, so the
    SDK's ``total`` keeps climbing. Each turn must report only its own slice —
    ``orchestrator._aggregate_token_usage`` SUMS the per-turn usages, so a
    cumulative figure makes the task total a sum of prefix sums."""

    def test_each_turn_reports_its_own_delta(self):
        agent = _agent()
        # Thread-cumulative totals as the SDK reports them at the end of turns 1-3.
        turn1 = agent._token_usage_from_sdk(_sdk_usage(10_000, 500, 6_000))
        turn2 = agent._token_usage_from_sdk(_sdk_usage(26_000, 1_300, 18_000))
        turn3 = agent._token_usage_from_sdk(_sdk_usage(40_000, 2_000, 30_000))

        assert turn1 is not None and turn2 is not None and turn3 is not None
        # Turn 1: 10k prompt, 6k of it cached.
        assert (turn1.uncached_input_tokens, turn1.output_tokens, turn1.cache_read_input_tokens) == (4_000, 500, 6_000)
        # Turn 2 spent 16k input / 800 output / 12k cached — NOT the 26k running total.
        assert (turn2.uncached_input_tokens, turn2.output_tokens, turn2.cache_read_input_tokens) == (4_000, 800, 12_000)
        assert (turn3.uncached_input_tokens, turn3.output_tokens, turn3.cache_read_input_tokens) == (2_000, 700, 12_000)

    def test_summed_turns_equal_the_final_cumulative_total(self):
        # The invariant the orchestrator depends on: Σ(per-turn usage) over a task
        # == the thread's final cumulative total. Reporting `total` per turn broke
        # this, inflating an N-turn task by roughly (N+1)/2.
        agent = _agent()
        cumulative = [(10_000, 500, 6_000), (26_000, 1_300, 18_000), (40_000, 2_000, 30_000)]
        turns = [agent._token_usage_from_sdk(_sdk_usage(*c)) for c in cumulative]

        final_input, final_output, final_cached = cumulative[-1]
        assert sum(t.output_tokens for t in turns if t) == final_output
        assert sum(t.cache_read_input_tokens for t in turns if t) == final_cached
        # uncached + cache_read reconstitutes the full prompt count.
        assert sum(t.uncached_input_tokens + t.cache_read_input_tokens for t in turns if t) == final_input

    def test_cost_is_per_turn_not_cumulative(self):
        agent = _agent()
        agent._token_usage_from_sdk(_sdk_usage(100_000, 5_000, 80_000))
        second = agent._token_usage_from_sdk(_sdk_usage(101_000, 5_100, 80_500))
        assert second is not None and second.total_cost_usd is not None
        # Turn 2 was a 1k-prompt sliver (500 fresh + 500 cached, 100 output).
        # Pricing the 101k running total instead would be roughly 100x this.
        assert second.total_cost_usd < 0.01

    def test_baseline_is_per_agent_not_global(self):
        first, second = _agent(), _agent()
        first._token_usage_from_sdk(_sdk_usage(50_000, 900, 30_000))
        fresh = second._token_usage_from_sdk(_sdk_usage(8_000, 200, 5_000))
        assert fresh is not None
        assert fresh.uncached_input_tokens == 3_000

    @pytest.mark.parametrize("sdk_usage", [None, SimpleNamespace(total=None)])
    def test_absent_total_yields_none_and_leaves_baseline_untouched(self, sdk_usage):
        agent = _agent()
        agent._token_usage_from_sdk(_sdk_usage(10_000, 500, 6_000))
        before = agent._thread_usage_baseline
        assert agent._token_usage_from_sdk(sdk_usage) is None
        assert agent._thread_usage_baseline == before


class TestCrashFallbackBaseline:
    """A crashed turn never yields an SDK total, so ``_token_usage_from_messages``
    reports it. The baseline still has to advance past it, or the next turn's
    delta re-books the crashed turn's tokens."""

    def test_advance_moves_baseline_past_a_message_derived_turn(self):
        agent = _agent()
        agent._advance_usage_baseline(
            TokenUsage(uncached_input_tokens=4_000, output_tokens=500, cache_read_input_tokens=6_000)
        )
        # SDK input is the full prompt, cached prefix included → 4k + 6k.
        assert agent._thread_usage_baseline == _ThreadTotals(input=10_000, output=500, cached=6_000)

    def test_turn_after_a_crash_is_not_inflated(self):
        agent = _agent()
        # Turn 1 completes normally: 10k prompt / 6k cached.
        agent._token_usage_from_sdk(_sdk_usage(10_000, 500, 6_000))
        # Turn 2 crashes; its tokens come off the flushed messages instead.
        agent._advance_usage_baseline(
            TokenUsage(uncached_input_tokens=2_000, output_tokens=300, cache_read_input_tokens=8_000)
        )
        # Turn 3's cumulative total includes turns 1 and 2; only turn 3 is new.
        turn3 = agent._token_usage_from_sdk(_sdk_usage(32_000, 1_100, 22_000))
        assert turn3 is not None
        assert (turn3.uncached_input_tokens, turn3.output_tokens, turn3.cache_read_input_tokens) == (4_000, 300, 8_000)

    def test_advance_is_a_noop_for_none(self):
        agent = _agent()
        agent._advance_usage_baseline(None)
        assert agent._thread_usage_baseline == _ThreadTotals()
