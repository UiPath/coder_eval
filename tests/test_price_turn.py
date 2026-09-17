"""``pricing.price_turn``: the one rule for a turn's cost, shared by every adapter and the monitor."""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from coder_eval.models import RunLimits, TokenUsage
from coder_eval.orchestration.turn_monitor import TurnMonitor
from coder_eval.pricing import calculate_cost, price_turn
from coder_eval.streaming.events import AgentEndEvent, AgentStartEvent, StreamEvent


_HAIKU = "claude-haiku-4-5"
_USAGE = TokenUsage(
    uncached_input_tokens=1000, output_tokens=500, cache_creation_input_tokens=10, cache_read_input_tokens=20
)


def _rate(model: str, usage: TokenUsage = _USAGE) -> float:
    cost = calculate_cost(
        model,
        usage.uncached_input_tokens,
        usage.output_tokens,
        usage.cache_creation_input_tokens,
        usage.cache_read_input_tokens,
    )
    assert cost is not None
    return cost


def _reported(cost: float | None, usage: TokenUsage = _USAGE) -> TokenUsage:
    return usage.model_copy(update={"total_cost_usd": cost})


class TestTheRule:
    def test_a_finite_non_zero_reported_cost_wins(self) -> None:
        assert price_turn(_reported(0.42), (_HAIKU,)) == 0.42

    @pytest.mark.parametrize("reported", [None, 0.0, 1.5])
    def test_empty_usage_returns_the_reported_cost_unchanged(self, reported: float | None) -> None:
        assert price_turn(TokenUsage(total_cost_usd=reported), (_HAIKU,)) == reported

    def test_the_rate_card_prices_an_unreported_cost(self) -> None:
        assert price_turn(_USAGE, (_HAIKU,)) == pytest.approx(_rate(_HAIKU))

    def test_a_reported_zero_on_a_priced_model_uses_the_rate_card(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("DEBUG", logger="coder_eval.pricing"):
            assert price_turn(_reported(0.0), (_HAIKU,)) == pytest.approx(_rate(_HAIKU))
        assert "using the rate card" in caplog.text

    def test_a_reported_zero_that_no_model_prices_stays_zero(self) -> None:
        assert price_turn(_reported(0.0), ("nowhere/not-a-model", None)) == 0.0

    def test_nothing_reported_and_nothing_priced_is_none(self) -> None:
        assert price_turn(_USAGE, ("nowhere/not-a-model", None)) is None
        assert price_turn(_USAGE, ()) is None

    @pytest.mark.parametrize("reported", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_reported_cost_counts_as_unreported(self, reported: float) -> None:
        assert price_turn(_reported(reported), (_HAIKU,)) == pytest.approx(_rate(_HAIKU))
        assert price_turn(_reported(reported), ("nowhere/not-a-model",)) is None
        assert price_turn(TokenUsage(total_cost_usd=reported), (_HAIKU,)) is None

    def test_the_first_priced_model_wins_and_none_is_skipped(self) -> None:
        models = (None, "", "nowhere/not-a-model", "claude-sonnet-4-6", _HAIKU)
        assert price_turn(_USAGE, models) == pytest.approx(_rate("claude-sonnet-4-6"))

    def test_a_bedrock_prefixed_id_prices_like_the_bare_id(self) -> None:
        assert price_turn(_USAGE, ("eu.anthropic.claude-haiku-4-5",)) == pytest.approx(_rate(_HAIKU))


class _Recorder:
    def __init__(self) -> None:
        self.events: list[StreamEvent] = []

    def on_event(self, event: StreamEvent) -> None:
        self.events.append(event)


def _monitor_cost(events: list[StreamEvent], model: str, reported: float | None) -> float | None:
    """The monitor's price for the same turn, fed the harness's RAW report instead of the adapter's price."""
    monitor = TurnMonitor("t", [], limits=RunLimits(max_usd=1000.0), model=model)
    for event in events:
        if isinstance(event, AgentEndEvent):
            event = event.model_copy(update={"usage": _reported(reported, event.usage)})
        monitor.on_event(event)
    return monitor.cost_usd()


def _adapter_cost(events: list[StreamEvent]) -> float | None:
    ends = [e for e in events if isinstance(e, AgentEndEvent)]
    assert len(ends) == 1
    return ends[0].usage.total_cost_usd


async def _run_cli(agent: Any, cli: str, lines: list[str], working_dir: str) -> list[StreamEvent]:
    from tests._fixtures.golden_streams.pi_fixtures import _FakeProcess

    proc = _FakeProcess(lines)

    async def fake_exec(*_argv: str, **_kwargs: Any) -> _FakeProcess:
        proc.stderr = proc  # type: ignore[assignment]
        return proc

    recorder = _Recorder()
    with (
        patch.object(asyncio, "create_subprocess_exec", fake_exec),
        patch("shutil.which", lambda _name: f"/usr/local/bin/{cli}"),
        patch.object(os, "killpg", lambda _pgid, _sig: None, create=True),
    ):
        await agent.start(working_dir)
        await agent.communicate("do it", iteration=1, stream_callback=recorder)
    return recorder.events


def _pi_lines(cost: float) -> list[str]:
    from tests._fixtures.golden_streams.pi_fixtures import _turn_end, _turn_start

    return [_turn_start(), _turn_end(inp=1000, out=500, cost=cost)]


def _opencode_lines(cost: float) -> list[str]:
    tokens = {"total": 1500, "input": 1000, "output": 500, "reasoning": 0, "cache": {"write": 0, "read": 0}}
    part = {"sessionID": "ses_1", "id": "prt_2", "messageID": "msg_1", "reason": "stop", "cost": cost, "tokens": tokens}
    return [
        json.dumps({"type": "step_start", "sessionID": "ses_1", "part": {"sessionID": "ses_1", "id": "prt_1"}}),
        json.dumps({"type": "step_finish", "sessionID": "ses_1", "part": part}),
    ]


class TestAdapterAndMonitorAgree:
    """Each harness's published turn cost equals what the ``max_usd`` monitor sums for the same turn."""

    @pytest.mark.parametrize("cost", [0.25, 0.0])
    async def test_pi(self, cost: float, tmp_path: Any) -> None:
        from coder_eval.agents.pi_agent import PiAgent
        from coder_eval.models import PiAgentConfig

        model = "openrouter/moonshotai/kimi-k3"
        agent = PiAgent(PiAgentConfig(type="pi", model=model), task_id="t")
        events = await _run_cli(agent, "pi", _pi_lines(cost), str(tmp_path))
        expected = cost or _rate(model, TokenUsage(uncached_input_tokens=1000, output_tokens=500))
        assert _adapter_cost(events) == pytest.approx(expected)
        assert _monitor_cost(events, model, cost) == pytest.approx(expected)

    @pytest.mark.parametrize("cost", [0.25, 0.0])
    async def test_opencode(self, cost: float, tmp_path: Any) -> None:
        from coder_eval.agents.opencode_agent import OpenCodeAgent
        from coder_eval.models import OpenCodeAgentConfig

        model = "deepseek/deepseek-v4-pro"
        agent = OpenCodeAgent(OpenCodeAgentConfig(type="opencode", model=model), task_id="t")
        events = await _run_cli(agent, "opencode", _opencode_lines(cost), str(tmp_path))
        expected = cost or _rate(model, TokenUsage(uncached_input_tokens=1000, output_tokens=500))
        assert _adapter_cost(events) == pytest.approx(expected)
        assert _monitor_cost(events, model, cost) == pytest.approx(expected)

    async def test_antigravity(self, tmp_path: Any) -> None:
        from coder_eval.agents import antigravity_agent
        from tests._fixtures.golden_streams.antigravity_fixtures import _agent_with_steps, _no_sleep, _step, _usage

        steps = [_step("TEXT_RESPONSE", "DONE", content="ok", complete=True, usage=_usage(1000, 200, 300, 50))]
        agent = _agent_with_steps(steps)
        agent.working_directory = tmp_path
        recorder = _Recorder()
        with patch.object(antigravity_agent.asyncio, "sleep", _no_sleep):
            await agent.communicate("do it", iteration=1, stream_callback=recorder)
        expected = _rate(
            "gemini-3.5-flash", TokenUsage(uncached_input_tokens=800, output_tokens=350, cache_read_input_tokens=200)
        )
        assert _adapter_cost(recorder.events) == pytest.approx(expected)
        assert _monitor_cost(recorder.events, "gemini-3.5-flash", None) == pytest.approx(expected)

    def test_codex(self) -> None:
        from coder_eval.agents.codex_agent import CodexAgent
        from coder_eval.models import CodexAgentConfig

        model = "gpt-5.6-terra"
        agent = CodexAgent(CodexAgentConfig(type="codex", model=model))
        sdk = SimpleNamespace(total=SimpleNamespace(input_tokens=1000, output_tokens=500, cached_input_tokens=400))
        usage = agent._token_usage_from_sdk(sdk)
        expected = _rate(model, TokenUsage(uncached_input_tokens=600, output_tokens=500, cache_read_input_tokens=400))
        assert usage is not None and usage.total_cost_usd == pytest.approx(expected)
        events: list[StreamEvent] = [AgentStartEvent(task_id="t"), AgentEndEvent(task_id="t", usage=usage)]
        assert _monitor_cost(events, model, None) == pytest.approx(expected)

    @pytest.mark.parametrize("sdk_cost", [None, 0.0, 0.33])
    def test_claude(self, sdk_cost: float | None) -> None:
        from coder_eval.agents.claude_code_agent import ClaudeCodeAgent

        sdk_usage = {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 20}
        usage = ClaudeCodeAgent._build_token_usage([], sdk_usage, sdk_cost, None, _HAIKU)
        assert usage is not None and usage.total_cost_usd is not None
        expected = sdk_cost or _rate(_HAIKU, usage)
        assert usage.total_cost_usd == pytest.approx(expected)
        events: list[StreamEvent] = [AgentStartEvent(task_id="t"), AgentEndEvent(task_id="t", usage=usage)]
        assert _monitor_cost(events, _HAIKU, sdk_cost) == pytest.approx(expected)
