"""Tests for per-consumer proxy token attribution on the LLMGW path.

The bundled Claude Code CLI cannot parse Bedrock's event-stream usage shape
and reports zero usage in its SDK ResultMessage on ProxyRoute. Before this
feature, the orchestrator covered that by falling back to the proxy's
suite-wide accumulator — which pooled main-agent, sub-agent (agent_judge),
llm_judge, and any other proxy-routed traffic into a single number that
was then mis-attributed to the main agent's ``total_token_usage``.

This module locks down the snapshot/diff disentanglement that replaces
that fallback:

- ``Orchestrator._attribute_proxy_delta_to_iteration``: per-iteration
  attribution for the main agent (and any user-simulator turn that uses
  the same retry helper).
- ``agent_judge``: per-sub-agent attribution; the sub-agent's
  ``TurnRecord.token_usage`` carries the delta.
- ``llm_judge``: per-API-call attribution; the
  ``JudgeCriterionResult.token_usage`` carries the delta.

OAUTH (``DirectRoute``) and Bedrock-direct go through entirely different
code paths (the CLI's own SDK-parsed usage). Tests below assert that
``proxy=None`` is a no-op so those paths are untouched.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from coder_eval.criteria import init_criteria
from coder_eval.criteria.agent_judge import AgentJudgeChecker
from coder_eval.criteria.base import CheckContext
from coder_eval.criteria.llm_judge import LLMJudgeChecker
from coder_eval.models import (
    AgentJudgeCriterion,
    AgentKind,
    ClaudeCodeAgentConfig,
    CriterionResult,
    JudgeCriterionResult,
    JudgeVerdict,
    LLMJudgeCriterion,
    TokenUsage,
    TurnRecord,
)
from coder_eval.models.routing import DirectRoute, ProxyRoute
from coder_eval.orchestrator import Orchestrator
from coder_eval.proxy.server import ProxyUsage
from coder_eval.sandbox import Sandbox, SandboxConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeProxy:
    """Minimal stand-in for ``LLMGatewayProxy`` exposing only ``.usage``.

    The real proxy starts an aiohttp server and refreshes OAuth tokens —
    overkill for unit tests. ``LLMGatewayProxy | None`` is the only contract
    the checkers and orchestrator depend on, so a duck-typed object with a
    mutable ``ProxyUsage`` is sufficient.
    """

    def __init__(self) -> None:
        self.usage = ProxyUsage()

    def add(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation: int = 0,
        cache_read: int = 0,
        cost: float = 0.0,
    ) -> None:
        """Simulate proxy traffic landing between snapshots."""
        self.usage.input_tokens += input_tokens
        self.usage.output_tokens += output_tokens
        self.usage.cache_creation_input_tokens += cache_creation
        self.usage.cache_read_input_tokens += cache_read
        self.usage.total_cost += cost

    def usage_total(self) -> TokenUsage:
        """Mirror ``LLMGatewayProxy.usage_total`` — the cumulative counter."""
        u = self.usage
        return TokenUsage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_creation_input_tokens=u.cache_creation_input_tokens,
            cache_read_input_tokens=u.cache_read_input_tokens,
            total_cost_usd=u.total_cost,
        )


def _make_turn(agent_output: str = "(stub)", token_usage: TokenUsage | None = None) -> TurnRecord:
    return TurnRecord(
        iteration=1,
        user_input="(prompt)",
        agent_output=agent_output,
        duration_seconds=1.0,
        token_usage=token_usage,
    )


def _zero_token_usage() -> TokenUsage:
    """The shape ``TurnRecord.token_usage`` arrives in on LLMGW.

    The CLI emits ``ResultMessage.usage`` with all-zero fields because it
    cannot parse Bedrock's event-stream usage. ``_build_token_usage`` in
    ``ClaudeCodeAgent`` materialises this as a TokenUsage with all zeros
    rather than ``None``.
    """
    return TokenUsage(
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        total_cost_usd=None,
    )


# ---------------------------------------------------------------------------
# Orchestrator helper — _attribute_proxy_delta_to_iteration
# ---------------------------------------------------------------------------


class TestAttributeProxyDeltaToIteration:
    """Direct unit tests for the static helper that picks the right
    TurnRecord on the main-agent retry loop and overwrites zero usage with
    the proxy delta.
    """

    def _delta(self, input_tokens: int = 100, output_tokens: int = 50) -> TokenUsage:
        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            total_cost_usd=None,
        )

    def test_attributes_to_returned_turn_when_token_usage_is_none(self):
        """Common-case happy path: one attempt, returned successfully, no partials.

        ``token_usage=None`` is what the SDK fills in when its ResultMessage
        has no usage block at all (an obscure CLI failure mode); the helper
        should still attribute the delta.
        """
        returned = _make_turn(token_usage=None)
        Orchestrator._attribute_proxy_delta_to_iteration(
            returned_turn=returned,
            appended_partials=[],
            delta=self._delta(input_tokens=100, output_tokens=40),
        )
        assert returned.token_usage is not None
        assert returned.token_usage.input_tokens == 100
        assert returned.token_usage.output_tokens == 40

    def test_attributes_to_returned_turn_when_token_usage_is_zero(self):
        """LLMGW happy path: SDK fills token_usage with all-zero fields.

        On LLMGW the CLI emits ``ResultMessage.usage`` as zero (it can't
        parse Bedrock event-stream usage) but the agent layer still wraps
        that into a TokenUsage — so the helper has to recognise zero as
        "no real data" and overwrite it.
        """
        returned = _make_turn(token_usage=_zero_token_usage())
        Orchestrator._attribute_proxy_delta_to_iteration(
            returned_turn=returned,
            appended_partials=[],
            delta=self._delta(input_tokens=150, output_tokens=70),
        )
        assert returned.token_usage is not None
        assert returned.token_usage.input_tokens == 150
        assert returned.token_usage.output_tokens == 70

    def test_does_not_overwrite_existing_non_zero_usage(self):
        """OAUTH safety: SDK-parsed usage is real on DirectRoute; the proxy
        is None and the helper would not be invoked. The orchestrator gates
        on ``self.proxy is not None`` before calling. Even so, defend in
        depth: if a non-zero value is already there, keep it.
        """
        existing = TokenUsage(input_tokens=500, output_tokens=120)
        returned = _make_turn(token_usage=existing)
        Orchestrator._attribute_proxy_delta_to_iteration(
            returned_turn=returned,
            appended_partials=[],
            delta=self._delta(input_tokens=9999, output_tokens=9999),
        )
        # Untouched.
        assert returned.token_usage.input_tokens == 500
        assert returned.token_usage.output_tokens == 120

    def test_attributes_to_latest_partial_when_no_returned_turn(self):
        """Terminal-failure path: the retry loop raised, partial(s) drained.

        ``returned_turn=None`` represents the case where the orchestrator
        is unwinding via exception; the only TurnRecord(s) that landed are
        the partials drained by ``_on_attempt_failure``. The latest one
        carries the iteration's proxy delta.
        """
        partial_1 = _make_turn(token_usage=_zero_token_usage())
        partial_2 = _make_turn(token_usage=_zero_token_usage())
        Orchestrator._attribute_proxy_delta_to_iteration(
            returned_turn=None,
            appended_partials=[partial_1, partial_2],
            delta=self._delta(input_tokens=42, output_tokens=8),
        )
        # Latest partial gets the delta.
        assert partial_2.token_usage.input_tokens == 42
        # Older partial keeps zero — acceptable minor under-attribution
        # on the rare retry path.
        assert partial_1.token_usage.input_tokens == 0

    def test_noop_when_nothing_to_attribute_to(self):
        """Defensive: helper invoked with no record to update is a no-op."""
        # No exception, no side effects on either arg.
        Orchestrator._attribute_proxy_delta_to_iteration(
            returned_turn=None,
            appended_partials=[],
            delta=self._delta(),
        )


# ---------------------------------------------------------------------------
# agent_judge — sub-agent token attribution
# ---------------------------------------------------------------------------


# Patched at the import site inside SubAgentRunner.
_RUNNER_PATCH = "coder_eval.criteria.agent_judge.SubAgentRunner"


@pytest.fixture(autouse=True)
def _init_criteria_registry() -> None:
    init_criteria(validate=False)


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    sb = Sandbox(SandboxConfig(driver="tempdir"), task_id="proxy_attribution_test")
    sb.sandbox_dir = tmp_path
    return sb


def _minimal_agent_judge_criterion() -> AgentJudgeCriterion:
    return AgentJudgeCriterion(
        type="agent_judge",
        description="test judge",
        prompt="Grade this.",
        pass_threshold=0.5,
        weight=1.0,
        include_reference=False,
        include_agent_output=False,
        include_tool_calls=False,
        include_dialog=False,
        max_turns=5,
        turn_timeout=30,
        agent=ClaudeCodeAgentConfig(type=AgentKind.CLAUDE_CODE, model="claude-sonnet-4-6"),
    )


class _StubRunner:
    """SubAgentRunner stub that returns a zero-usage TurnRecord (LLMGW shape)
    while simulating proxy traffic accumulating during the run."""

    def __init__(
        self,
        *,
        proxy: _FakeProxy,
        sim_input: int = 100_000,
        sim_output: int = 1_500,
        verdict_score: float = 0.85,
    ) -> None:
        self._proxy = proxy
        self._sim_input = sim_input
        self._sim_output = sim_output
        self.capture = MagicMock()
        self.capture.verdict = JudgeVerdict(
            score=verdict_score,
            rationale="(test verdict)",
            findings=[],
        )
        self.capture.error = None

    def run(self, user_msg: str, *, max_turns: int, turn_timeout: float) -> TurnRecord:
        # Simulate the sub-agent's traffic landing on the proxy DURING this call.
        self._proxy.add(
            input_tokens=self._sim_input,
            output_tokens=self._sim_output,
            cache_read=300_000,
        )
        # CLI on LLMGW emits zero usage in its ResultMessage.
        return _make_turn(token_usage=_zero_token_usage())


class TestAgentJudgeProxyAttribution:
    """Verify ``agent_judge`` attributes the sub-agent's proxy delta to its
    own ``JudgeCriterionResult.token_usage`` instead of letting it pool into
    the main agent's total via the (now-removed) zero-SDK fallback.
    """

    def test_proxy_route_attributes_delta_to_criterion_result(self, sandbox: Sandbox) -> None:
        criterion = _minimal_agent_judge_criterion()
        proxy = _FakeProxy()
        # Simulate pre-existing main-agent traffic — must NOT be attributed
        # to the judge.
        proxy.add(input_tokens=5_000_000, output_tokens=200_000)
        baseline_main_input = proxy.usage.input_tokens

        runner_stub = _StubRunner(proxy=proxy, sim_input=120_000, sim_output=2_000)
        with patch(_RUNNER_PATCH, return_value=runner_stub):
            checker = AgentJudgeChecker()
            result = checker.check(
                criterion,
                sandbox,
                reference_code=None,
                turn_records=[],
                context=CheckContext(route=ProxyRoute(port=12345), proxy=proxy),
            )

        assert isinstance(result, JudgeCriterionResult)
        # Sub-agent's tokens land here, not on the main agent's bill.
        assert result.token_usage is not None
        assert result.token_usage.input_tokens == 120_000
        assert result.token_usage.output_tokens == 2_000
        assert result.token_usage.cache_read_input_tokens == 300_000
        # The main-agent baseline is undisturbed in the proxy's running
        # total; the judge's slice is in addition to it.
        assert proxy.usage.input_tokens == baseline_main_input + 120_000

    def test_direct_route_with_no_proxy_is_a_noop(self, sandbox: Sandbox) -> None:
        """On OAUTH, no proxy is provided and the sub-agent's real SDK-parsed
        usage flows through. Test the no-proxy path: sub-agent returns a real
        usage, the checker preserves it as-is on the result.
        """
        criterion = _minimal_agent_judge_criterion()

        real_usage = TokenUsage(input_tokens=98_000, output_tokens=1_800)

        class _DirectRouteStub(_StubRunner):
            def run(self, user_msg: str, *, max_turns: int, turn_timeout: float) -> TurnRecord:
                return _make_turn(token_usage=real_usage)

        with patch(_RUNNER_PATCH, return_value=_DirectRouteStub(proxy=_FakeProxy())):
            checker = AgentJudgeChecker()
            result = checker.check(
                criterion,
                sandbox,
                reference_code=None,
                turn_records=[],
                context=CheckContext(route=DirectRoute(), proxy=None),
            )

        assert isinstance(result, JudgeCriterionResult)
        # SDK-parsed value preserved.
        assert result.token_usage is not None
        assert result.token_usage.input_tokens == 98_000
        assert result.token_usage.output_tokens == 1_800


# ---------------------------------------------------------------------------
# llm_judge — API-call token attribution
# ---------------------------------------------------------------------------


# Patched at the import site inside criteria/llm_judge.py
_LLM_JUDGE_INVOKE_PATCH = "coder_eval.criteria.llm_judge._invoke_tool_channel"


def _minimal_llm_judge_criterion() -> LLMJudgeCriterion:
    return LLMJudgeCriterion(
        type="llm_judge",
        description="test llm judge",
        prompt="Grade this.",
        pass_threshold=0.5,
        weight=1.0,
        include_reference=False,
        include_agent_output=False,
        include_tool_calls=False,
        include_dialog=False,
        model="claude-sonnet-4-6",
        max_tokens=1024,
        temperature=0.0,
    )


def _fake_invoke_channel_factory(
    proxy: _FakeProxy,
    *,
    score: float = 0.9,
    sim_input: int = 80_000,
    sim_output: int = 800,
):
    """Build a stub for ``_invoke_tool_channel`` that simulates the API
    call landing on the proxy during its execution."""

    def _fake(*, criterion, route, system_msg, user_msg):
        proxy.add(input_tokens=sim_input, output_tokens=sim_output, cache_read=200_000)
        verdict = JudgeVerdict(score=score, rationale="(stub)", findings=[])
        # response_usage=None: these tests assert the PROXY delta wins.
        return verdict, None, verdict.model_dump_json(), None

    return _fake


class TestLLMJudgeProxyAttribution:
    """Verify ``llm_judge`` attaches the per-call proxy delta to its
    ``JudgeCriterionResult.token_usage`` on ProxyRoute.
    """

    def test_proxy_route_attaches_delta_to_criterion_result(self, sandbox: Sandbox) -> None:
        criterion = _minimal_llm_judge_criterion()
        proxy = _FakeProxy()
        proxy.add(input_tokens=2_000_000, output_tokens=80_000)  # main-agent baseline
        baseline = proxy.usage.snapshot()

        with patch(_LLM_JUDGE_INVOKE_PATCH, side_effect=_fake_invoke_channel_factory(proxy)):
            checker = LLMJudgeChecker()
            result = checker.check(
                criterion,
                sandbox,
                reference_code=None,
                turn_records=[],
                context=CheckContext(route=ProxyRoute(port=12345), proxy=proxy),
            )

        assert isinstance(result, JudgeCriterionResult)
        assert result.token_usage is not None
        assert result.token_usage.input_tokens == 80_000
        assert result.token_usage.output_tokens == 800
        assert result.token_usage.cache_read_input_tokens == 200_000
        # Suite-wide proxy total advanced by exactly the judge's slice.
        assert proxy.usage.input_tokens == baseline.input_tokens + 80_000

    def test_direct_route_with_no_proxy_leaves_usage_none(self, sandbox: Sandbox) -> None:
        """OAUTH path. No proxy, no snapshot; ``token_usage`` stays None.

        Attributing OAUTH llm_judge usage from the SDK response is a
        follow-up — out of scope here. The point is: no pollution on either
        side and no spurious zero TokenUsage.
        """
        criterion = _minimal_llm_judge_criterion()

        def _fake(*, criterion, route, system_msg, user_msg):
            verdict = JudgeVerdict(score=0.7, rationale="(stub)", findings=[])
            # response_usage=None: these tests assert the PROXY delta wins.
            return verdict, None, verdict.model_dump_json(), None

        with patch(_LLM_JUDGE_INVOKE_PATCH, side_effect=_fake):
            checker = LLMJudgeChecker()
            result = checker.check(
                criterion,
                sandbox,
                reference_code=None,
                turn_records=[],
                context=CheckContext(route=DirectRoute(), proxy=None),
            )

        assert isinstance(result, JudgeCriterionResult)
        assert result.token_usage is None

    def test_proxy_route_with_zero_delta_leaves_usage_none(self, sandbox: Sandbox) -> None:
        """If the judge somehow ran without touching the proxy (e.g. a
        future config where ProxyRoute is set but the judge uses a separate
        client), the zero delta should be dropped to None rather than
        surfacing as an all-zero TokenUsage. Keeps the field semantics
        consistent: ``None`` means "no proxy attribution"."""
        criterion = _minimal_llm_judge_criterion()
        proxy = _FakeProxy()

        def _fake(*, criterion, route, system_msg, user_msg):
            verdict = JudgeVerdict(score=0.9, rationale="(stub)", findings=[])
            # response_usage=None: these tests assert the PROXY delta wins.
            return verdict, None, verdict.model_dump_json(), None

        with patch(_LLM_JUDGE_INVOKE_PATCH, side_effect=_fake):
            checker = LLMJudgeChecker()
            result = checker.check(
                criterion,
                sandbox,
                reference_code=None,
                turn_records=[],
                context=CheckContext(route=ProxyRoute(port=12345), proxy=proxy),
            )

        assert isinstance(result, JudgeCriterionResult)
        assert result.token_usage is None


# ---------------------------------------------------------------------------
# Cross-cutting: main-agent aggregate excludes judge contributions
# ---------------------------------------------------------------------------


class TestMainAgentTotalExcludesJudgeContributions:
    """End-to-end check that the orchestrator's main-agent aggregate (sum
    of ``result.iterations[*].token_usage``) does NOT include any
    JudgeCriterionResult token usage, regardless of route.
    """

    def test_aggregate_sums_only_iterations(self) -> None:
        """``_aggregate_token_usage`` is purely a sum over iterations now,
        with no proxy fallback — judges live in ``success_criteria_results``
        which the aggregator never inspects.
        """
        from datetime import datetime

        from coder_eval.models import EvaluationResult, FinalStatus

        # Two main-agent turns with real per-turn usage (attribution already
        # done by ``_communicate_with_retry``'s snapshot/diff or by the SDK
        # parsing on OAUTH).
        t1 = _make_turn(token_usage=TokenUsage(input_tokens=10_000, output_tokens=300))
        t2 = _make_turn(token_usage=TokenUsage(input_tokens=15_000, output_tokens=450))

        # A judge result that — on the pre-fix code path — would have been
        # included in the main agent's total via the proxy fallback.
        judge_result = JudgeCriterionResult(
            criterion_type="agent_judge",
            description="dummy",
            score=0.9,
            token_usage=TokenUsage(input_tokens=120_000, output_tokens=2_000),
        )

        result = EvaluationResult(
            task_id="t",
            task_description="dummy",
            agent_type=AgentKind.CLAUDE_CODE,
            started_at=datetime(2026, 1, 1),
            final_status=FinalStatus.SUCCESS,
            iteration_count=2,
        )
        result.iterations.extend([t1, t2])
        result.success_criteria_results = [judge_result]

        # Drive the aggregator. Build a barely-init'd Orchestrator with
        # ``proxy=None`` so the now-removed fallback wouldn't have fired
        # anyway; the assertion targets the new contract.
        orch = Orchestrator.__new__(Orchestrator)
        orch.result = result
        orch.proxy = None
        orch._aggregate_token_usage()

        assert result.total_token_usage is not None
        # Sum of t1 + t2 only. Judge tokens are NOT included.
        assert result.total_token_usage.input_tokens == 25_000
        assert result.total_token_usage.output_tokens == 750


# ---------------------------------------------------------------------------
# TokenUsage helpers — is_empty / __add__
# ---------------------------------------------------------------------------


class TestTokenUsageHelpers:
    """Unit coverage for the shared primitives the attribution paths rely on."""

    def test_is_empty_true_for_all_zero(self) -> None:
        assert TokenUsage().is_empty()
        assert _zero_token_usage().is_empty()
        # Cost is ignored — a delta with cost but no tokens is still "empty".
        assert TokenUsage(total_cost_usd=0.01).is_empty()

    def test_is_empty_false_when_any_counter_set(self) -> None:
        assert not TokenUsage(input_tokens=1).is_empty()
        assert not TokenUsage(output_tokens=1).is_empty()
        assert not TokenUsage(cache_creation_input_tokens=1).is_empty()
        assert not TokenUsage(cache_read_input_tokens=1).is_empty()

    def test_add_sums_token_fields(self) -> None:
        a = TokenUsage(
            input_tokens=10,
            output_tokens=2,
            cache_creation_input_tokens=3,
            cache_read_input_tokens=4,
        )
        b = TokenUsage(
            input_tokens=100,
            output_tokens=20,
            cache_creation_input_tokens=30,
            cache_read_input_tokens=40,
        )
        total = a + b
        assert total.input_tokens == 110
        assert total.output_tokens == 22
        assert total.cache_creation_input_tokens == 33
        assert total.cache_read_input_tokens == 44

    def test_add_cost_is_none_when_neither_has_cost(self) -> None:
        assert (TokenUsage(input_tokens=1) + TokenUsage(input_tokens=1)).total_cost_usd is None

    def test_add_cost_sums_only_present_operands(self) -> None:
        assert (TokenUsage(total_cost_usd=0.5) + TokenUsage()).total_cost_usd == 0.5
        assert (TokenUsage(total_cost_usd=0.5) + TokenUsage(total_cost_usd=0.25)).total_cost_usd == 0.75


# ---------------------------------------------------------------------------
# Orchestrator helper — _accumulate_judge_usage
# ---------------------------------------------------------------------------


def _judge(token_usage: TokenUsage | None) -> JudgeCriterionResult:
    return JudgeCriterionResult(
        criterion_type="llm_judge",
        description="dummy judge",
        score=0.9,
        token_usage=token_usage,
    )


class TestAccumulateJudgeUsage:
    """The simulation every_turn/both loop replaces ``success_criteria_results``
    each turn; this helper keeps a per-criterion running total so earlier
    judge calls aren't dropped from the persisted result.
    """

    def test_single_check_passes_through(self) -> None:
        accum: dict[tuple[int, str], TokenUsage] = {}
        results = [_judge(TokenUsage(input_tokens=100, output_tokens=10))]
        Orchestrator._accumulate_judge_usage(results, accum)
        assert results[0].token_usage is not None
        assert results[0].token_usage.input_tokens == 100

    def test_accumulates_across_turns(self) -> None:
        accum: dict[tuple[int, str], TokenUsage] = {}
        # Turn 1: judge billed 100/10.
        turn1 = [_judge(TokenUsage(input_tokens=100, output_tokens=10))]
        Orchestrator._accumulate_judge_usage(turn1, accum)
        # Turn 2: a FRESH results list (as check_all returns) with that turn's
        # per-turn slice only.
        turn2 = [_judge(TokenUsage(input_tokens=250, output_tokens=25))]
        Orchestrator._accumulate_judge_usage(turn2, accum)
        # The latest results list now carries the cumulative dialog total.
        assert turn2[0].token_usage is not None
        assert turn2[0].token_usage.input_tokens == 350
        assert turn2[0].token_usage.output_tokens == 35

    def test_carries_total_forward_when_later_turn_has_no_judge_tokens(self) -> None:
        accum: dict[tuple[int, str], TokenUsage] = {}
        Orchestrator._accumulate_judge_usage([_judge(TokenUsage(input_tokens=100))], accum)
        # A later check where the judge produced an empty/None slice (e.g.
        # skipped, or a zero proxy delta dropped to None) must not lose the
        # running total.
        later = [_judge(None)]
        Orchestrator._accumulate_judge_usage(later, accum)
        assert later[0].token_usage is not None
        assert later[0].token_usage.input_tokens == 100

    def test_ignores_non_judge_results(self) -> None:
        accum: dict[tuple[int, str], TokenUsage] = {}
        plain = CriterionResult(criterion_type="file_exists", description="x", score=1.0)
        results: list[CriterionResult] = [plain, _judge(TokenUsage(input_tokens=42))]
        Orchestrator._accumulate_judge_usage(results, accum)
        # Non-judge result untouched; only the judge slot accumulated.
        assert accum == {(1, "llm_judge"): results[1].token_usage}
        assert results[1].token_usage is not None
        assert results[1].token_usage.input_tokens == 42

    def test_per_criterion_isolation(self) -> None:
        """Two judges at different positions accumulate independently."""
        accum: dict[tuple[int, str], TokenUsage] = {}
        turn1 = [_judge(TokenUsage(input_tokens=100)), _judge(TokenUsage(input_tokens=5))]
        Orchestrator._accumulate_judge_usage(turn1, accum)
        turn2 = [_judge(TokenUsage(input_tokens=100)), _judge(TokenUsage(input_tokens=5))]
        Orchestrator._accumulate_judge_usage(turn2, accum)
        assert turn2[0].token_usage is not None
        assert turn2[1].token_usage is not None
        assert turn2[0].token_usage.input_tokens == 200
        assert turn2[1].token_usage.input_tokens == 10

    def test_two_same_type_judges_across_three_turns_stay_distinct(self) -> None:
        """Two ``llm_judge`` criteria at positions 0 and 1 accumulate as separate
        ledger entries across a 3-turn dialog — the (position, type) key keeps
        them from merging even though their criterion_type is identical.
        """
        accum: dict[tuple[int, str], TokenUsage] = {}
        per_turn = [(10, 1), (20, 2), (30, 3)]  # (judge-0 input, judge-1 input) per turn
        for j0_in, j1_in in per_turn:
            results = [_judge(TokenUsage(input_tokens=j0_in)), _judge(TokenUsage(input_tokens=j1_in))]
            Orchestrator._accumulate_judge_usage(results, accum)
            latest = results
        # Distinct ledger entries, one per position.
        assert set(accum.keys()) == {(0, "llm_judge"), (1, "llm_judge")}
        assert accum[(0, "llm_judge")].input_tokens == 60  # 10+20+30
        assert accum[(1, "llm_judge")].input_tokens == 6  # 1+2+3
        # The latest results list carries each judge's cumulative total.
        assert latest[0].token_usage is not None and latest[0].token_usage.input_tokens == 60
        assert latest[1].token_usage is not None and latest[1].token_usage.input_tokens == 6

    def test_one_shot_accumulate_yields_slice_without_carry_forward(self) -> None:
        """A single accumulate over a fresh ledger (the shape the non-simulation
        path would produce if it ever called this) yields per-criterion totals
        equal to that one turn's slice — no carry-forward."""
        accum: dict[tuple[int, str], TokenUsage] = {}
        results = [_judge(TokenUsage(input_tokens=42, output_tokens=7))]
        Orchestrator._accumulate_judge_usage(results, accum)
        assert results[0].token_usage is not None
        assert results[0].token_usage.input_tokens == 42
        assert results[0].token_usage.output_tokens == 7
        assert accum == {(0, "llm_judge"): results[0].token_usage}


class TestNonJudgeIgnoresContext:
    """Non-judge checkers accept the uniform ``context`` kwarg and ignore it —
    a populated CheckContext (route/proxy/reference_dir) must not change their
    behavior or error.
    """

    def test_file_exists_ignores_populated_context(self, sandbox: Sandbox) -> None:
        from coder_eval.criteria.file_exists import FileExistsChecker
        from coder_eval.models import FileExistsCriterion

        assert sandbox.sandbox_dir is not None
        (sandbox.sandbox_dir / "present.txt").write_text("hi")
        criterion = FileExistsCriterion(description="x", path="present.txt")

        ctx = CheckContext(route=ProxyRoute(port=1), proxy=_FakeProxy())
        result = FileExistsChecker().check(criterion, sandbox, context=ctx)
        assert result.score == 1.0
        assert result.error is None


# ---------------------------------------------------------------------------
# Orchestrator — _reconcile_proxy_usage (Proxy-only diagnostic invariant)
# ---------------------------------------------------------------------------


def _eval_result_with(
    *,
    total_token_usage: TokenUsage | None,
    judges: list[CriterionResult],
):
    """Barely-init'd EvaluationResult carrying a main total + judge results."""
    from datetime import datetime

    from coder_eval.models import EvaluationResult, FinalStatus

    result = EvaluationResult(
        task_id="t",
        task_description="dummy",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime(2026, 1, 1),
        final_status=FinalStatus.SUCCESS,
        iteration_count=1,
    )
    result.total_token_usage = total_token_usage
    result.success_criteria_results = judges
    return result


def _orch_for_reconcile(proxy, result):
    orch = Orchestrator.__new__(Orchestrator)
    orch.result = result
    orch.proxy = proxy
    return orch


class TestReconcileProxyUsage:
    """``_reconcile_proxy_usage`` is the keystone correctness guarantee: it
    cross-checks per-consumer attribution against the proxy's independent
    counter. It is diagnostic-only — logs INFO on a clean reconcile, WARNING
    on a gap, and NEVER raises or fails a run.
    """

    def test_gap_zero_logs_info(self, caplog) -> None:
        proxy = _FakeProxy()
        main = TokenUsage(input_tokens=25_000, output_tokens=750)
        judge_usage = TokenUsage(input_tokens=120_000, output_tokens=2_000)
        # Proxy ground-truth counter == main + judge (zero gap by construction).
        proxy.add(
            input_tokens=main.input_tokens + judge_usage.input_tokens,
            output_tokens=main.output_tokens + judge_usage.output_tokens,
        )
        result = _eval_result_with(total_token_usage=main, judges=[_judge(judge_usage)])
        orch = _orch_for_reconcile(proxy, result)

        with caplog.at_level(logging.INFO, logger="coder_eval.orchestrator"):
            orch._reconcile_proxy_usage()

        # Read the expected total from the proxy, not a hardcoded literal.
        expected_total = proxy.usage_total().total_tokens
        info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert any("reconciled" in m and str(expected_total) in m for m in info_msgs)
        assert not any(r.levelno == logging.WARNING for r in caplog.records)

    def test_nonzero_gap_logs_warning_and_does_not_raise(self, caplog) -> None:
        proxy = _FakeProxy()
        main = TokenUsage(input_tokens=10_000, output_tokens=300)
        # A dropped older partial: proxy counted more than we attributed.
        dropped = 5_000
        proxy.add(input_tokens=main.input_tokens + dropped, output_tokens=main.output_tokens)
        result = _eval_result_with(total_token_usage=main, judges=[])
        orch = _orch_for_reconcile(proxy, result)

        with caplog.at_level(logging.WARNING, logger="coder_eval.orchestrator"):
            orch._reconcile_proxy_usage()  # must NOT raise

        warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        # WARNING names the gap (= dropped tokens).
        assert any("gap" in m and str(dropped) in m for m in warn_msgs)

    def test_proxy_none_is_noop(self, caplog) -> None:
        result = _eval_result_with(total_token_usage=TokenUsage(input_tokens=10_000), judges=[])
        orch = _orch_for_reconcile(None, result)

        with caplog.at_level(logging.INFO, logger="coder_eval.orchestrator"):
            orch._reconcile_proxy_usage()

        # No reconciliation log records at all when there's no proxy.
        assert not any("reconcil" in r.getMessage() or "proxy usage gap" in r.getMessage() for r in caplog.records)

    def test_judge_with_none_usage_contributes_zero(self, caplog) -> None:
        """A judge with token_usage=None (unknown) contributes 0 to the
        attributed sum, so its tokens show up as part of the gap. Acceptable
        and observable — must still WARNING, never raise."""
        proxy = _FakeProxy()
        main = TokenUsage(input_tokens=10_000, output_tokens=300)
        unknown_judge_tokens = 8_000
        proxy.add(input_tokens=main.input_tokens + unknown_judge_tokens, output_tokens=main.output_tokens)
        result = _eval_result_with(total_token_usage=main, judges=[_judge(None)])
        orch = _orch_for_reconcile(proxy, result)

        with caplog.at_level(logging.WARNING, logger="coder_eval.orchestrator"):
            orch._reconcile_proxy_usage()

        warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("gap" in m and str(unknown_judge_tokens) in m for m in warn_msgs)

    def test_reconciliation_bug_is_swallowed(self, caplog) -> None:
        """A bug inside reconciliation (here: usage_total raising) must be
        logged and swallowed — never abort the run."""

        class _BrokenProxy:
            def usage_total(self) -> TokenUsage:
                raise RuntimeError("boom")

        result = _eval_result_with(total_token_usage=TokenUsage(input_tokens=1), judges=[])
        orch = _orch_for_reconcile(_BrokenProxy(), result)

        with caplog.at_level(logging.WARNING, logger="coder_eval.orchestrator"):
            orch._reconcile_proxy_usage()  # must NOT raise

        assert any("reconciliation failed" in r.getMessage() for r in caplog.records)
