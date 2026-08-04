"""Tests for OpenHandsAgent — the OpenHands Software Agent SDK backend.

REQUIRES: `pip install 'coder-eval[openhands]'` (or `uv sync --extra openhands`).
The OpenHands SDK is an optional extra; the SDK-dependent portions of this module
skip when it isn't installed, so `make test` / CI stay green without the extra.

The config / registry / pricing / sandbox-allowlist tests do NOT need the SDK (the
model layer never imports it) and run in the base Quality Gate; only the agent
runtime tests below the importorskip need the extra.
"""

from __future__ import annotations

import pytest

from coder_eval.models import (
    AgentConfig,
    AgentKind,
    OpenHandsAgentConfig,
    parse_agent_config,
)


# --- SDK-independent config / registry / pricing / allowlist tests ----------
#
# These exercise the model + config layer, which must parse `agent.type: openhands`
# WITHOUT importing the OpenHands SDK — so a task can be loaded/validated on a base
# install and only fail at start() if the extra is missing.


class TestOpenHandsConfigParsing:
    """OpenHandsAgentConfig round-trips through the discriminated union."""

    def test_parse_by_string_kind(self):
        cfg = parse_agent_config(type="openhands")
        assert isinstance(cfg, OpenHandsAgentConfig)
        assert cfg.type == AgentKind.OPENHANDS

    def test_parse_by_enum_kind(self):
        cfg = parse_agent_config(type=AgentKind.OPENHANDS, model="openrouter/z-ai/glm-5.2")
        assert isinstance(cfg, OpenHandsAgentConfig)
        assert cfg.type == AgentKind.OPENHANDS
        assert cfg.model == "openrouter/z-ai/glm-5.2"

    def test_discriminated_union_round_trip(self):
        """An openhands agent block validates + serializes via the AgentConfig union."""
        from pydantic import TypeAdapter

        adapter = TypeAdapter(AgentConfig)
        cfg = adapter.validate_python({"type": "openhands", "model": "anthropic/claude-sonnet-4-6"})
        assert isinstance(cfg, OpenHandsAgentConfig)
        dumped = adapter.dump_python(cfg)
        assert dumped["type"] == "openhands"
        assert dumped["model"] == "anthropic/claude-sonnet-4-6"

    def test_extra_forbid_bites(self):
        """An unknown key on the openhands config raises (inherited extra='forbid')."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            parse_agent_config(type="openhands", not_a_real_field=123)

    def test_no_model_is_allowed(self):
        """model may be omitted (resolved at the agent layer from OPENHANDS_MODEL)."""
        cfg = parse_agent_config(type="openhands")
        assert cfg.model is None


class TestOpenHandsConfigParsesWithoutSDK:
    """Parsing an openhands agent block must NOT import the OpenHands SDK.

    Guards the 'parse without the extra' invariant: a task pinning
    `agent.type: openhands` loads/validates fine on a base install; the SDK import
    is deferred to start().
    """

    def test_parse_does_not_import_openhands_sdk(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def _guard(name, *args, **kwargs):
            if name == "openhands" or name.startswith("openhands."):
                raise AssertionError(f"config parsing imported the OpenHands SDK ({name!r})")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _guard)
        cfg = parse_agent_config(type="openhands", model="openrouter/z-ai/glm-5.2")
        assert isinstance(cfg, OpenHandsAgentConfig)


class TestOpenHandsPricingKey:
    """The provider-prefixed OSS ids OpenHands passes must key the rate card.

    `_normalize_model` was extended to strip `openrouter/`/`openai/` so a
    provider-prefixed id resolves to the bare OSS rate (else cost reads unpriced).
    """

    def test_openrouter_prefixed_glm_prices(self):
        from coder_eval.pricing import calculate_cost

        prefixed = calculate_cost("openrouter/z-ai/glm-5.2", uncached_input_tokens=1_000_000, output_tokens=0)
        bare = calculate_cost("z-ai/glm-5.2", uncached_input_tokens=1_000_000, output_tokens=0)
        assert prefixed is not None
        assert prefixed == bare

    def test_openrouter_prefixed_kimi_and_deepseek_price(self):
        from coder_eval.pricing import is_priced

        assert is_priced("openrouter/moonshotai/kimi-k3")
        assert is_priced("openrouter/deepseek/deepseek-v4-pro")

    def test_openai_prefix_stripped(self):
        from coder_eval.pricing import calculate_cost

        prefixed = calculate_cost("openai/gpt-5.4", uncached_input_tokens=1_000_000, output_tokens=0)
        bare = calculate_cost("gpt-5.4", uncached_input_tokens=1_000_000, output_tokens=0)
        assert prefixed is not None
        assert prefixed == bare


class TestOpenHandsEnvPassthroughAllowlist:
    """The provider-credential env vars OpenHands needs are in the default allowlist."""

    def test_provider_vars_present_in_default_env_passthrough(self):
        from coder_eval.models.sandbox import DockerDriverConfig

        allowlist = DockerDriverConfig().env_passthrough
        for var in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "OPENHANDS_BASE_URL", "OPENHANDS_MODEL"):
            assert var in allowlist, f"{var} missing from default env_passthrough allowlist"


# --- Registry dispatch (SDK-free — registration is import-time, not start()) -


class TestOpenHandsRegistryDispatch:
    """create_agent dispatches type=openhands to OpenHandsAgent (SDK not needed)."""

    def test_create_agent_returns_openhands_agent(self):
        from coder_eval.agent import AgentState
        from coder_eval.agents.openhands_agent import OpenHandsAgent
        from coder_eval.agents.registry import create_agent

        cfg = parse_agent_config(type=AgentKind.OPENHANDS)
        agent = create_agent(AgentKind.OPENHANDS, cfg)
        assert isinstance(agent, OpenHandsAgent)
        assert agent.get_state() == AgentState.WORKING
        assert agent.pending_turn is None

    def test_register_builtins_rot_check_includes_openhands(self):
        from coder_eval.agents import register_builtins
        from coder_eval.agents.registry import AgentRegistry

        # Must not raise: the rot-protection loop asserts OPENHANDS registered.
        register_builtins(AgentRegistry)
        assert AgentRegistry.get(AgentKind.OPENHANDS) is not None

    def test_capability_flags(self):
        from coder_eval.agents.openhands_agent import OpenHandsAgent

        assert OpenHandsAgent.supports_cooperative_stop is True
        assert OpenHandsAgent.supports_cost_log_tags is True


class TestOpenHandsEnvironmentInfo:
    """get_environment_info records model always; proxy host only when set."""

    def test_records_model_only_without_base_url(self, monkeypatch):
        from coder_eval.agents.openhands_agent import OpenHandsAgent

        monkeypatch.delenv("OPENHANDS_BASE_URL", raising=False)
        agent = OpenHandsAgent(parse_agent_config(type=AgentKind.OPENHANDS, model="anthropic/claude-sonnet-4-6"))
        assert agent.get_environment_info() == {"openhands_model": "anthropic/claude-sonnet-4-6"}

    def test_records_proxy_host_when_base_url_set(self, monkeypatch):
        from coder_eval.agents.openhands_agent import OpenHandsAgent

        monkeypatch.setenv("OPENHANDS_BASE_URL", "https://key@proxy.internal:4000/v1")
        agent = OpenHandsAgent(parse_agent_config(type=AgentKind.OPENHANDS, model="litellm_proxy/glm"))
        info = agent.get_environment_info()
        assert info == {"openhands_model": "litellm_proxy/glm", "openhands_base_url_host": "proxy.internal"}
        assert "key" not in str(info)  # embedded credential not leaked


class TestOpenHandsEffectiveModel:
    """agent.model wins; else settings.openhands_model; else None."""

    def test_config_model_wins(self, monkeypatch):
        from coder_eval.agents.openhands_agent import OpenHandsAgent
        from coder_eval.config import settings

        monkeypatch.setattr(settings, "openhands_model", "openrouter/fallback")
        agent = OpenHandsAgent(parse_agent_config(type=AgentKind.OPENHANDS, model="openrouter/pinned"))
        assert agent._effective_model() == "openrouter/pinned"

    def test_settings_fallback(self, monkeypatch):
        from coder_eval.agents.openhands_agent import OpenHandsAgent
        from coder_eval.config import settings

        monkeypatch.setattr(settings, "openhands_model", "openrouter/fallback")
        agent = OpenHandsAgent(parse_agent_config(type=AgentKind.OPENHANDS))
        assert agent._effective_model() == "openrouter/fallback"

    def test_none_when_unset(self, monkeypatch):
        from coder_eval.agents.openhands_agent import OpenHandsAgent
        from coder_eval.config import settings

        monkeypatch.setattr(settings, "openhands_model", None)
        agent = OpenHandsAgent(parse_agent_config(type=AgentKind.OPENHANDS))
        assert agent._effective_model() is None


# --- SDK-dependent agent runtime tests (mocked SDK; require the extra) --------

pytest.importorskip("openhands.sdk")

import threading  # noqa: E402
import time  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from coder_eval.agent import AgentState  # noqa: E402
from coder_eval.agents.openhands_agent import OpenHandsAgent, _OpenHandsTurnState  # noqa: E402
from coder_eval.errors import AgentCrashError, TurnTimeoutError  # noqa: E402
from coder_eval.models import TokenUsage  # noqa: E402
from coder_eval.streaming.collector import EventCollector  # noqa: E402
from coder_eval.streaming.events import AgentEndStatus  # noqa: E402


class _KindEvent:
    """A lightweight scripted event whose ``kind`` selects the handler.

    Lets us drive _OpenHandsTurnState.dispatch without constructing real SDK event
    objects (whose constructors are heavy). We patch dispatch to route on ``kind``.
    """

    def __init__(self, kind: str, **fields):
        self.kind = kind
        for k, v in fields.items():
            setattr(self, k, v)


def _agent_message(text: str) -> _KindEvent:
    """A message _KindEvent whose content is a real TextContent (content_to_str reads it)."""
    from openhands.sdk.llm import Message, TextContent

    return _KindEvent(
        "message", source="agent", llm_message=Message(role="assistant", content=[TextContent(text=text)])
    )


def _kind_route(state: _OpenHandsTurnState, event) -> None:
    """Stand-in for _route that keys on ``event.kind`` (test-only)."""
    if event.kind == "action":
        state._on_action(event)
    elif event.kind == "observation":
        state._on_observation(event)
    elif event.kind == "error":
        state._on_agent_error(event)
    elif event.kind == "message":
        state._on_message(event)


def _make_state(agent: OpenHandsAgent, *, should_stop=None) -> tuple[_OpenHandsTurnState, EventCollector, list]:
    captured: list = []
    collector = EventCollector()
    from coder_eval.streaming.callbacks import CompositeStreamCallback

    emit = CompositeStreamCallback([collector, SimpleNamespace(on_event=captured.append)])
    agent._begin_turn()
    state = _OpenHandsTurnState(
        agent=agent,
        emit=emit,
        task_id="openhands",
        turn_id="openhands-1",
        collector=collector,
        user_input="do it",
        iteration=1,
        model="openrouter/z-ai/glm-5.2",
        turn_start_time=0.0,
        should_stop=should_stop,
    )
    return state, collector, captured


class TestOpenHandsEventMapping:
    """A scripted event sequence reduces into a correct TurnRecord."""

    def test_message_action_observation_reduce(self, monkeypatch):
        monkeypatch.setattr(_OpenHandsTurnState, "_route", _kind_route)
        agent = OpenHandsAgent(parse_agent_config(type=AgentKind.OPENHANDS, model="openrouter/z-ai/glm-5.2"))
        state, collector, captured = _make_state(agent)

        # A text message (real TextContent so content_to_str reads it), a tool call,
        # its observation, then finalize.
        msg = _agent_message("Hello world")
        action = _KindEvent(
            "action",
            tool_name="execute_bash",
            tool_call_id="t1",
            action=SimpleNamespace(model_dump=lambda mode="json": {"command": "echo hi"}),
        )
        obs = _KindEvent("observation", action_id="t1", observation=SimpleNamespace(agent_observation="hi\n"))

        state.dispatch(msg)
        state.dispatch(action)
        state.dispatch(obs)
        state.finalize(AgentEndStatus.COMPLETED, crashed=False, crash_reason=None)

        record = collector.build_turn_record()
        # Tool start->end pairing on tool_call_id==action_id.
        assert [c.tool_name for c in record.commands] == ["execute_bash"]
        assert record.commands[0].result_status == "success"
        assert record.commands[0].result_summary == "hi\n"
        # Assistant text captured.
        assert "Hello world" in record.agent_output
        # Exactly one AgentEnd closes the tree.
        from coder_eval.streaming.events import AgentEndEvent

        assert sum(1 for e in captured if isinstance(e, AgentEndEvent)) == 1

    def test_tool_failure_via_agent_error_event(self, monkeypatch):
        monkeypatch.setattr(_OpenHandsTurnState, "_route", _kind_route)
        agent = OpenHandsAgent(parse_agent_config(type=AgentKind.OPENHANDS, model="openrouter/z-ai/glm-5.2"))
        state, collector, _ = _make_state(agent)

        action = _KindEvent(
            "action",
            tool_name="execute_bash",
            tool_call_id="t1",
            action=SimpleNamespace(model_dump=lambda mode="json": {}),
        )
        err = _KindEvent("error", tool_name="execute_bash", tool_call_id="t1", error="boom")
        state.dispatch(action)
        state.dispatch(err)
        state.finalize(AgentEndStatus.COMPLETED, crashed=False, crash_reason=None)

        record = collector.build_turn_record()
        assert record.commands[0].result_status == "error"
        assert record.commands[0].error_message == "boom"

    def test_orphan_tool_closed_unresolved(self, monkeypatch):
        monkeypatch.setattr(_OpenHandsTurnState, "_route", _kind_route)
        agent = OpenHandsAgent(parse_agent_config(type=AgentKind.OPENHANDS, model="openrouter/z-ai/glm-5.2"))
        state, collector, captured = _make_state(agent)

        action = _KindEvent(
            "action",
            tool_name="str_replace_editor",
            tool_call_id="orphan",
            action=SimpleNamespace(model_dump=lambda mode="json": {}),
        )
        state.dispatch(action)  # no observation → orphan
        state.finalize(AgentEndStatus.COMPLETED, crashed=False, crash_reason=None)

        record = collector.build_turn_record()
        orphans = [c for c in record.commands if c.tool_id == "orphan"]
        assert len(orphans) == 1
        assert orphans[0].tool_name == "str_replace_editor"
        assert orphans[0].result_status == "unknown"
        from coder_eval.streaming.events import ToolEndEvent, ToolEndStatus

        unresolved = [e for e in captured if isinstance(e, ToolEndEvent) and e.status == ToolEndStatus.UNRESOLVED]
        assert len(unresolved) == 1


class TestOpenHandsTokenMapping:
    """_token_usage_from_openhands maps the accumulated Metrics into TokenUsage.

    OpenHands' prompt_tokens INCLUDES the cache buckets, so uncached =
    prompt - cache_read - cache_write. Buckets must sum to the full prompt.
    """

    def _conversation(self, *, prompt, completion, cache_read, cache_write, reasoning, cost):
        usage = SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            reasoning_tokens=reasoning,
        )
        metrics = SimpleNamespace(accumulated_token_usage=usage, accumulated_cost=cost)
        stats = SimpleNamespace(get_combined_metrics=lambda: metrics)
        return SimpleNamespace(conversation_stats=stats)

    def test_cache_inclusive_prompt_yields_fresh_slice(self):
        agent = OpenHandsAgent(parse_agent_config(type=AgentKind.OPENHANDS, model="openrouter/z-ai/glm-5.2"))
        conv = self._conversation(prompt=5898, completion=64, cache_read=5760, cache_write=0, reasoning=10, cost=0.0063)
        usage = agent._token_usage_from_openhands(conv)
        assert usage is not None
        assert usage.uncached_input_tokens == 138  # 5898 - 5760 - 0
        assert usage.cache_read_input_tokens == 5760
        assert usage.cache_creation_input_tokens == 0
        # completion_tokens (incl. reasoning, billed as output by LiteLLM) → output.
        assert usage.output_tokens == 64
        # Derived input_tokens == the full prompt.
        assert usage.input_tokens == 5898
        assert usage.total_cost_usd == 0.0063

    def test_cache_write_bucket_present(self):
        agent = OpenHandsAgent(parse_agent_config(type=AgentKind.OPENHANDS, model="openrouter/z-ai/glm-5.2"))
        conv = self._conversation(prompt=1000, completion=5, cache_read=600, cache_write=200, reasoning=0, cost=0.0)
        usage = agent._token_usage_from_openhands(conv)
        assert usage is not None
        assert usage.uncached_input_tokens == 200  # 1000 - 600 - 200
        assert usage.cache_creation_input_tokens == 200
        assert usage.cache_read_input_tokens == 600
        assert usage.input_tokens == 1000  # buckets sum to the full prompt

    def test_cache_over_report_clamps_preserving_sum(self):
        """A malformed report where cache_read + cache_write > prompt must still keep
        the three input buckets summing to the full prompt (no over-count)."""
        agent = OpenHandsAgent(parse_agent_config(type=AgentKind.OPENHANDS, model="openrouter/z-ai/glm-5.2"))
        conv = self._conversation(prompt=100, completion=5, cache_read=90, cache_write=20, reasoning=0, cost=0.0)
        usage = agent._token_usage_from_openhands(conv)
        assert usage is not None
        assert usage.uncached_input_tokens == 0  # floored
        assert usage.cache_read_input_tokens == 90
        assert usage.cache_creation_input_tokens == 10  # clamped to the remainder
        # Invariant preserved: the three buckets sum to prompt, not to 200.
        assert usage.input_tokens == 100

    def test_unpriced_model_keeps_native_cost(self):
        agent = OpenHandsAgent(parse_agent_config(type=AgentKind.OPENHANDS, model="openrouter/some-unpriced-model"))
        conv = self._conversation(prompt=100, completion=5, cache_read=0, cache_write=0, reasoning=0, cost=0.42)
        usage = agent._token_usage_from_openhands(conv)
        assert usage is not None
        assert usage.total_cost_usd == 0.42  # native estimate kept, not zeroed

    def test_non_finite_cost_dropped(self):
        agent = OpenHandsAgent(parse_agent_config(type=AgentKind.OPENHANDS, model="openrouter/z-ai/glm-5.2"))
        conv = self._conversation(prompt=100, completion=5, cache_read=0, cache_write=0, reasoning=0, cost=float("nan"))
        usage = agent._token_usage_from_openhands(conv)
        assert usage is not None
        assert usage.total_cost_usd is None  # nan/inf never propagates into cost math

    def test_reconciliation_invariant_holds(self, monkeypatch):
        """Summing the four buckets across TurnRecord.messages == token_usage."""
        monkeypatch.setattr(_OpenHandsTurnState, "_route", _kind_route)
        agent = OpenHandsAgent(parse_agent_config(type=AgentKind.OPENHANDS, model="openrouter/z-ai/glm-5.2"))
        state, collector, _ = _make_state(agent)
        state.dispatch(_agent_message("hi"))
        state.usage = TokenUsage(
            uncached_input_tokens=138, output_tokens=64, cache_read_input_tokens=5760, cache_creation_input_tokens=0
        )
        state.finalize(AgentEndStatus.COMPLETED, crashed=False, crash_reason=None)
        record = collector.build_turn_record()
        assert record.token_usage is not None
        in_sum = sum(getattr(m, "input_tokens", 0) for m in record.messages)
        out_sum = sum(getattr(m, "output_tokens", 0) for m in record.messages)
        cr_sum = sum(getattr(m, "cache_read_tokens", 0) for m in record.messages)
        cw_sum = sum(getattr(m, "cache_creation_tokens", 0) for m in record.messages)
        assert in_sum == record.token_usage.uncached_input_tokens
        assert out_sum == record.token_usage.output_tokens
        assert cr_sum == record.token_usage.cache_read_input_tokens
        assert cw_sum == record.token_usage.cache_creation_input_tokens


# --- Fake SDK objects for driving communicate() end-to-end -------------------


class _FakeState:
    def __init__(self, status_name: str):
        self.execution_status = SimpleNamespace(name=status_name, value=status_name.lower())


class _FakeConversation:
    """A fake Conversation whose run() replays scripted events into the callback."""

    def __init__(self, *, events, status="FINISHED", metrics=None, run_hook=None, **_kw):
        self._callbacks = _kw.get("callbacks") or []
        self._events = events
        self._status = status
        self._run_hook = run_hook
        self.closed = False
        self.paused = False
        self.state = _FakeState(status)
        usage = metrics or SimpleNamespace(
            prompt_tokens=100, completion_tokens=10, cache_read_tokens=0, cache_write_tokens=0, reasoning_tokens=0
        )
        m = SimpleNamespace(accumulated_token_usage=usage, accumulated_cost=0.01)
        self.conversation_stats = SimpleNamespace(get_combined_metrics=lambda: m)

    def send_message(self, _msg):
        pass

    def run(self):
        for ev in self._events:
            for cb in self._callbacks:
                cb(ev)
        if self._run_hook is not None:
            self._run_hook(self)

    def pause(self):
        self.paused = True

    def close(self):
        self.closed = True


def _install_fake_sdk(monkeypatch, conversation_factory, *, captured: dict | None = None):
    """Patch the lazy SDK imports inside _run_conversation via sys.modules shims.

    When ``captured`` is provided, the kwargs the agent passes to ``LLM(...)`` and
    ``Conversation(...)`` are recorded there (for asserting the endpoint config lever).
    """
    import openhands.sdk as ohsdk

    def _llm(**kw):
        if captured is not None:
            captured["llm"] = kw
        return SimpleNamespace(**kw)

    def _conversation(**kw):
        if captured is not None:
            captured["conversation"] = kw
        return conversation_factory(**kw)

    monkeypatch.setattr(ohsdk, "LLM", _llm, raising=False)
    monkeypatch.setattr(ohsdk, "Agent", lambda **kw: SimpleNamespace(**kw), raising=False)
    monkeypatch.setattr(ohsdk, "Tool", lambda **kw: SimpleNamespace(**kw), raising=False)
    monkeypatch.setattr(ohsdk, "Conversation", _conversation, raising=False)


async def _started_agent(tmp_path, model="openrouter/z-ai/glm-5.2") -> OpenHandsAgent:
    agent = OpenHandsAgent(parse_agent_config(type=AgentKind.OPENHANDS, model=model))
    await agent.start(str(tmp_path))
    return agent


class TestRouteDispatchDriftGuard:
    """_route dispatches REAL SDK event types (isinstance) — guards against SDK drift.

    Uses a genuine MessageEvent built through the SDK's own constructors (the
    action/observation/error paths are pure attribute reads and are covered via the
    _KindEvent shim elsewhere; a real MessageEvent is the cheapest real-type guard
    that a future SDK rename of MessageEvent / its content accessor would break)."""

    def test_real_message_event_routes_and_captures_text(self):
        from openhands.sdk.event import MessageEvent
        from openhands.sdk.llm import Message, TextContent

        agent = OpenHandsAgent(parse_agent_config(type=AgentKind.OPENHANDS, model="openrouter/z-ai/glm-5.2"))
        state, collector, _ = _make_state(agent)
        ev = MessageEvent(source="agent", llm_message=Message(role="assistant", content=[TextContent(text="hi there")]))
        state.dispatch(ev)  # real _route, real isinstance dispatch
        state.finalize(AgentEndStatus.COMPLETED, crashed=False, crash_reason=None)
        assert "hi there" in collector.build_turn_record().agent_output


class TestCommunicateHappyPath:
    async def test_happy_path_collects_output_and_tokens(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_OpenHandsTurnState, "_route", _kind_route)
        events = [
            _KindEvent(
                "action",
                tool_name="execute_bash",
                tool_call_id="t1",
                action=SimpleNamespace(model_dump=lambda mode="json": {"command": "echo hi"}),
            ),
            _KindEvent("observation", action_id="t1", observation=SimpleNamespace(agent_observation="hi\n")),
        ]

        def factory(**kw):
            return _FakeConversation(events=events, status="FINISHED", **kw)

        _install_fake_sdk(monkeypatch, factory)
        agent = await _started_agent(tmp_path)
        record = await agent.communicate("do it")

        assert [c.tool_name for c in record.commands] == ["execute_bash"]
        assert record.token_usage is not None
        assert record.token_usage.uncached_input_tokens == 100
        assert agent.get_state() == AgentState.WORKING
        assert agent.pending_turn is None
        assert agent._active_conversation is None


class TestEndpointConfigLever:
    """The direct-vs-proxy switch is env-only: OPENHANDS_BASE_URL threads into
    LLM(base_url=...) with NO code branch on backend; _effective_model carries the
    agent.model provider prefix unchanged; delete_on_close=False + max_iteration_per_run
    are wired on the Conversation."""

    async def test_base_url_set_builds_llm_with_base_url(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_OpenHandsTurnState, "_route", _kind_route)
        monkeypatch.setenv("OPENHANDS_BASE_URL", "https://proxy.internal:4000/v1")
        captured: dict = {}

        def factory(**kw):
            return _FakeConversation(events=[], status="FINISHED", **kw)

        _install_fake_sdk(monkeypatch, factory, captured=captured)
        agent = await _started_agent(tmp_path, model="litellm_proxy/z-ai/glm-5.2")
        await agent.communicate("do it", max_turns=7)

        assert captured["llm"]["base_url"] == "https://proxy.internal:4000/v1"
        # Model prefix carried unchanged (no code branch on backend).
        assert captured["llm"]["model"] == "litellm_proxy/z-ai/glm-5.2"
        # Conversation config: delete_on_close=False + max_turns→max_iteration_per_run.
        assert captured["conversation"]["delete_on_close"] is False
        assert captured["conversation"]["max_iteration_per_run"] == 7

    async def test_base_url_unset_builds_llm_with_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_OpenHandsTurnState, "_route", _kind_route)
        monkeypatch.delenv("OPENHANDS_BASE_URL", raising=False)
        captured: dict = {}

        def factory(**kw):
            return _FakeConversation(events=[], status="FINISHED", **kw)

        _install_fake_sdk(monkeypatch, factory, captured=captured)
        agent = await _started_agent(tmp_path, model="openrouter/z-ai/glm-5.2")
        await agent.communicate("do it")

        assert captured["llm"]["base_url"] is None
        assert captured["llm"]["model"] == "openrouter/z-ai/glm-5.2"
        # No max_turns → the default iteration cap.
        assert captured["conversation"]["max_iteration_per_run"] == 500

    async def test_cost_log_tags_forwarded_only_when_present(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_OpenHandsTurnState, "_route", _kind_route)
        captured: dict = {}

        def factory(**kw):
            return _FakeConversation(events=[], status="FINISHED", **kw)

        _install_fake_sdk(monkeypatch, factory, captured=captured)
        # Direct path: no tags → extra_headers None.
        agent = OpenHandsAgent(parse_agent_config(type=AgentKind.OPENHANDS, model="openrouter/z-ai/glm-5.2"))
        await agent.start(str(tmp_path))
        await agent.communicate("do it")
        assert captured["llm"]["extra_headers"] is None

        # Proxy path: cost_log_tags → forwarded as extra_headers.
        agent2 = OpenHandsAgent(
            parse_agent_config(type=AgentKind.OPENHANDS, model="litellm_proxy/glm"),
            cost_log_tags={"x-ce-run-id": "run-123"},
        )
        await agent2.start(str(tmp_path))
        await agent2.communicate("do it")
        assert captured["llm"]["extra_headers"] == {"x-ce-run-id": "run-123"}


class TestCommunicateCrash:
    async def test_error_status_funnels_to_crash(self, monkeypatch, tmp_path):
        def factory(**kw):
            return _FakeConversation(events=[], status="ERROR", **kw)

        _install_fake_sdk(monkeypatch, factory)
        agent = await _started_agent(tmp_path)

        with pytest.raises(AgentCrashError):
            await agent.communicate("do it")
        assert agent.pending_turn is not None
        assert agent.pending_turn.crashed is True
        assert agent.get_state() == AgentState.ERROR
        await agent.discard_pending_turn()
        assert agent._iteration == 0

    async def test_run_exception_funnels_to_crash(self, monkeypatch, tmp_path):
        def boom(_conv):
            raise RuntimeError("stream blew up")

        def factory(**kw):
            return _FakeConversation(events=[], status="RUNNING", run_hook=boom, **kw)

        _install_fake_sdk(monkeypatch, factory)
        agent = await _started_agent(tmp_path)

        with pytest.raises(AgentCrashError):
            await agent.communicate("do it")
        assert agent.pending_turn is not None
        assert agent.pending_turn.crashed is True

    @pytest.mark.parametrize("status", ["STUCK", "WAITING_FOR_CONFIRMATION", "IDLE", "PAUSED"])
    async def test_non_finished_terminal_status_crashes(self, monkeypatch, tmp_path, status):
        """ALLOWLIST classification: any terminal status other than FINISHED (that we
        did not ourselves cause via pause) is a crash — never a silent COMPLETED. This
        includes STUCK, WAITING_FOR_CONFIRMATION, an unexpected IDLE, and a PAUSED we
        did NOT trigger (no timeout / no should_stop)."""

        def factory(**kw):
            return _FakeConversation(events=[], status=status, **kw)

        _install_fake_sdk(monkeypatch, factory)
        agent = await _started_agent(tmp_path)

        with pytest.raises(AgentCrashError):
            await agent.communicate("do it")
        assert agent.pending_turn is not None
        assert agent.pending_turn.crashed is True


class TestCommunicateTimeout:
    async def test_timeout_pauses_and_raises(self, monkeypatch, tmp_path):
        started = threading.Event()

        def block(_conv):
            started.set()
            # Block until paused by the watchdog (poll the flag).
            for _ in range(200):
                if _conv.paused:
                    return
                time.sleep(0.01)

        def factory(**kw):
            return _FakeConversation(events=[], status="PAUSED", run_hook=block, **kw)

        _install_fake_sdk(monkeypatch, factory)
        agent = await _started_agent(tmp_path)

        with pytest.raises(TurnTimeoutError):
            await agent.communicate("do it", timeout=0.15)
        assert agent.pending_turn is not None
        assert agent.pending_turn.crashed is True
        # The worker was drained before finalize (no abandon), so the turn's real
        # token/cost total is committed onto the partial record — not lost to a race.
        assert agent.pending_turn.token_usage is not None
        assert agent.pending_turn.token_usage.uncached_input_tokens == 100

    async def test_plugins_configured_warns(self, monkeypatch, tmp_path, caplog):
        import logging

        agent = OpenHandsAgent(
            parse_agent_config(
                type=AgentKind.OPENHANDS,
                model="openrouter/z-ai/glm-5.2",
                plugins=[{"type": "local", "path": "/some/skills"}],
            )
        )
        with caplog.at_level(logging.WARNING):
            await agent.start(str(tmp_path))
        assert any("ignores agent.plugins" in r.message for r in caplog.records)


class TestCommunicateStoppedEarly:
    async def test_should_stop_finalizes_stopped_early(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_OpenHandsTurnState, "_route", _kind_route)
        action = _KindEvent(
            "action",
            tool_name="execute_bash",
            tool_call_id="t1",
            action=SimpleNamespace(model_dump=lambda mode="json": {}),
        )

        def factory(**kw):
            conv = _FakeConversation(events=[action], status="PAUSED", **kw)
            return conv

        _install_fake_sdk(monkeypatch, factory)
        agent = await _started_agent(tmp_path)

        record = await agent.communicate("do it", should_stop=lambda: True)
        # Clean stop (not a crash), record returned; pause() was requested.
        assert agent.pending_turn is None
        assert record is not None


class TestLifecycleIdempotency:
    async def test_stop_and_kill_sync_idempotent(self, monkeypatch, tmp_path):
        conv = _FakeConversation(events=[], status="FINISHED", callbacks=[])
        agent = await _started_agent(tmp_path)
        agent._active_conversation = conv
        await agent.stop()
        assert conv.closed is True
        # Idempotent second calls do not raise.
        await agent.stop()
        agent.kill_sync()
        assert agent._active_conversation is None


class TestStartWithoutSDK:
    async def test_start_missing_extra_raises_install_hint(self, monkeypatch, tmp_path):
        import builtins

        real_import = builtins.__import__

        def _no_openhands(name, *args, **kwargs):
            if name == "openhands.sdk" or name == "openhands.tools":
                raise ImportError("no openhands")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_openhands)
        agent = OpenHandsAgent(parse_agent_config(type=AgentKind.OPENHANDS, model="openrouter/z-ai/glm-5.2"))
        with pytest.raises(RuntimeError, match=r"coder-eval\[openhands\]"):
            await agent.start(str(tmp_path))


class TestOffThreadEmission:
    """The novel-risk gate: run()'s callback fires on the to_thread worker while the
    main coroutine is parked. Events must still reduce into a correct TurnRecord —
    no dropped/duplicated tool ends, ordering preserved."""

    async def test_off_thread_events_reduce_correctly(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_OpenHandsTurnState, "_route", _kind_route)
        emit_threads: list[int] = []
        main_thread = threading.get_ident()

        action = _KindEvent(
            "action",
            tool_name="execute_bash",
            tool_call_id="t1",
            action=SimpleNamespace(model_dump=lambda mode="json": {}),
        )
        obs = _KindEvent("observation", action_id="t1", observation=SimpleNamespace(agent_observation="ok"))

        class _RecordingConversation(_FakeConversation):
            def run(self):
                emit_threads.append(threading.get_ident())
                super().run()

        def factory(**kw):
            return _RecordingConversation(events=[action, obs], status="FINISHED", **kw)

        _install_fake_sdk(monkeypatch, factory)
        agent = await _started_agent(tmp_path)
        record = await agent.communicate("do it")

        # run() executed off the main event-loop thread.
        assert emit_threads and emit_threads[0] != main_thread
        # Exactly one tool, resolved once (no dup/drop).
        assert len(record.commands) == 1
        assert record.commands[0].result_status == "success"
