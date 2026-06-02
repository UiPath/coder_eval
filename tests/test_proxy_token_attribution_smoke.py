"""End-to-end smoke test for proxy token attribution.

Drives ``SuccessChecker.check_all`` over a fabricated dummy task that
includes BOTH an ``agent_judge`` and an ``llm_judge`` criterion, with a
fake ``LLMGatewayProxy``-like accumulator that simulates proxy traffic
landing during each judge's work.

Verifies that:
  - each judge's portion of proxy traffic lands on its own
    ``JudgeCriterionResult.token_usage`` (the disentanglement holds end-
    to-end across the SuccessChecker → criteria → judge call chain), and
  - the per-criterion deltas sum to the proxy's running total — no
    tokens are dropped or counted twice.

This complements the focused unit tests in
``test_proxy_token_attribution.py`` by exercising the full plumbing path:
``SuccessChecker(..., proxy=fake)`` → ``check_all`` → ``BaseCriterion.check``
signature filter → each criterion's ``_check_impl`` receiving ``proxy``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from coder_eval.criteria import init_criteria
from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import (
    AgentJudgeCriterion,
    AgentKind,
    ClaudeCodeAgentConfig,
    JudgeCriterionResult,
    JudgeVerdict,
    LLMJudgeCriterion,
    TurnRecord,
)
from coder_eval.models.routing import ProxyRoute
from coder_eval.proxy.server import ProxyUsage
from coder_eval.sandbox import Sandbox, SandboxConfig


# Patch points — keep aligned with criterion module imports.
_RUNNER_PATCH = "coder_eval.criteria.agent_judge.SubAgentRunner"
_LLM_JUDGE_INVOKE_PATCH = "coder_eval.criteria.llm_judge._invoke_tool_channel"


class _FakeProxy:
    """Mutable usage accumulator that quacks like ``LLMGatewayProxy``."""

    def __init__(self) -> None:
        self.usage = ProxyUsage()

    def add(self, *, input_tokens: int = 0, output_tokens: int = 0, cost: float = 0.0) -> None:
        self.usage.input_tokens += input_tokens
        self.usage.output_tokens += output_tokens
        self.usage.total_cost += cost


@pytest.fixture(autouse=True)
def _registry() -> None:
    init_criteria(validate=False)


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    sb = Sandbox(SandboxConfig(driver="tempdir"), task_id="proxy_attribution_smoke")
    sb.sandbox_dir = tmp_path
    return sb


def _make_dummy_task_criteria() -> list:
    """A minimal task with both judge kinds present."""
    return [
        AgentJudgeCriterion(
            type="agent_judge",
            description="dummy agent_judge",
            prompt="Grade.",
            pass_threshold=0.5,
            weight=1.0,
            include_reference=False,
            include_agent_output=False,
            include_tool_calls=False,
            include_dialog=False,
            max_turns=3,
            turn_timeout=20,
            agent=ClaudeCodeAgentConfig(type=AgentKind.CLAUDE_CODE, model="claude-sonnet-4-6"),
        ),
        LLMJudgeCriterion(
            type="llm_judge",
            description="dummy llm_judge",
            prompt="Grade.",
            pass_threshold=0.5,
            weight=1.0,
            include_reference=False,
            include_agent_output=False,
            include_tool_calls=False,
            include_dialog=False,
            model="claude-sonnet-4-6",
            max_tokens=1024,
            temperature=0.0,
        ),
    ]


def _stub_runner_for_agent_judge(proxy: _FakeProxy, sim_input: int, sim_output: int):
    """Returns a SubAgentRunner stub that simulates proxy traffic and
    returns a zero-usage TurnRecord (the LLMGW shape)."""

    class _Stub:
        def __init__(self) -> None:
            self.capture = MagicMock()
            self.capture.verdict = JudgeVerdict(score=0.9, rationale="(agent_judge stub)", findings=[])
            self.capture.error = None

        def run(self, user_msg, *, max_turns, turn_timeout):
            proxy.add(input_tokens=sim_input, output_tokens=sim_output)
            # CLI emits zero usage on LLMGW.
            from coder_eval.models import TokenUsage

            return TurnRecord(
                iteration=1,
                user_input="(judge prompt)",
                agent_output="(judge stub)",
                duration_seconds=1.0,
                token_usage=TokenUsage(),  # all zeros
            )

    return _Stub()


def _stub_invoke_for_llm_judge(proxy: _FakeProxy, sim_input: int, sim_output: int):
    """Returns a side-effect function for ``_invoke_tool_channel`` that
    simulates proxy traffic and returns a successful verdict."""

    def _fake(*, criterion, route, system_msg, user_msg):
        proxy.add(input_tokens=sim_input, output_tokens=sim_output)
        verdict = JudgeVerdict(score=0.85, rationale="(llm_judge stub)", findings=[])
        # 4-tuple: response_usage=None so the proxy delta is the attributed value.
        return verdict, None, verdict.model_dump_json(), None

    return _fake


def test_end_to_end_judge_token_attribution_on_proxy_route(sandbox: Sandbox) -> None:
    """Drive SuccessChecker.check_all across both judge kinds with a fake
    proxy. Each judge's slice should land on its own ``token_usage``; the
    sum should equal the proxy's running delta.
    """
    proxy = _FakeProxy()
    # Pre-existing "main agent" traffic — must NOT be attributed to any judge.
    proxy.add(input_tokens=2_000_000, output_tokens=80_000)
    baseline_input = proxy.usage.input_tokens
    baseline_output = proxy.usage.output_tokens

    # Simulated per-judge slices.
    agent_judge_in, agent_judge_out = 150_000, 1_800
    llm_judge_in, llm_judge_out = 90_000, 700

    runner_stub = _stub_runner_for_agent_judge(proxy, agent_judge_in, agent_judge_out)
    invoke_stub = _stub_invoke_for_llm_judge(proxy, llm_judge_in, llm_judge_out)

    with (
        patch(_RUNNER_PATCH, return_value=runner_stub),
        patch(_LLM_JUDGE_INVOKE_PATCH, side_effect=invoke_stub),
    ):
        checker = SuccessChecker(
            sandbox,
            init_registry=False,
            validate_registry=False,
            route=ProxyRoute(port=12345),
            proxy=proxy,
        )
        results = checker.check_all(_make_dummy_task_criteria(), turn_records=[])

    # One per criterion, in input order.
    assert len(results) == 2
    agent_judge_result, llm_judge_result = results
    assert isinstance(agent_judge_result, JudgeCriterionResult)
    assert isinstance(llm_judge_result, JudgeCriterionResult)

    # Each judge attributes ONLY its own slice — not the baseline, not the other's.
    assert agent_judge_result.token_usage is not None
    assert agent_judge_result.token_usage.input_tokens == agent_judge_in
    assert agent_judge_result.token_usage.output_tokens == agent_judge_out

    assert llm_judge_result.token_usage is not None
    assert llm_judge_result.token_usage.input_tokens == llm_judge_in
    assert llm_judge_result.token_usage.output_tokens == llm_judge_out

    # Proxy total advanced by exactly baseline + both judge slices — no
    # token is lost or double-counted between the two checkers.
    assert proxy.usage.input_tokens == baseline_input + agent_judge_in + llm_judge_in
    assert proxy.usage.output_tokens == baseline_output + agent_judge_out + llm_judge_out


def test_judges_independent_when_both_run(sandbox: Sandbox) -> None:
    """If the agent_judge runs first and then the llm_judge runs, the
    llm_judge's snapshot is taken AFTER the agent_judge has finished — so
    its delta MUST NOT include the agent_judge's tokens.
    """
    proxy = _FakeProxy()

    runner_stub = _stub_runner_for_agent_judge(proxy, sim_input=200_000, sim_output=3_000)
    invoke_stub = _stub_invoke_for_llm_judge(proxy, sim_input=50_000, sim_output=400)

    with (
        patch(_RUNNER_PATCH, return_value=runner_stub),
        patch(_LLM_JUDGE_INVOKE_PATCH, side_effect=invoke_stub),
    ):
        checker = SuccessChecker(
            sandbox,
            init_registry=False,
            validate_registry=False,
            route=ProxyRoute(port=12345),
            proxy=proxy,
        )
        results = checker.check_all(_make_dummy_task_criteria(), turn_records=[])

    agent_judge_result, llm_judge_result = results
    # The agent_judge's slice is 200_000 in; the llm_judge's slice is
    # 50_000 in. If we had a leak the llm_judge would show 250_000.
    assert agent_judge_result.token_usage is not None
    assert agent_judge_result.token_usage.input_tokens == 200_000
    assert llm_judge_result.token_usage is not None
    assert llm_judge_result.token_usage.input_tokens == 50_000
