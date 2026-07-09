"""Claude Code golden-master scenarios: recorded SDK streams + a runner.

The Claude agent consumes the module-global ``query`` async generator, so each
scenario supplies a ``mock_query`` that yields a recorded list of duck-typed SDK
events. The duck-typed classes mirror the real SDK shapes the agent sniffs for
(see the ``_is_*`` guards in ``claude_code_agent``). Mocks accept a ``transport``
kwarg because ``communicate`` forwards one once a ``timeout`` is set.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

from claude_agent_sdk import ProcessError

import coder_eval.agents.claude_code_agent as claude_module
from coder_eval.models import AgentKind, parse_agent_config


ClaudeCodeAgent = claude_module.ClaudeCodeAgent


# --- Duck-typed SDK message stand-ins ---------------------------------------
# Identified by attribute shape exactly as the real SDK objects are (the agent
# never isinstance-checks these). Kept minimal but faithful to the fields the
# dispatch loop reads.


class TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class ThinkingBlock:
    def __init__(self, thinking: str, signature: str | None = None) -> None:
        self.thinking = thinking
        self.signature = signature


class ToolUseBlock:
    def __init__(self, tool_id: str, name: str, input_params: Any) -> None:
        self.id = tool_id
        self.name = name
        self.input = input_params


class ToolResultBlock:
    def __init__(self, tool_use_id: str, is_error: bool, content: Any) -> None:
        self.tool_use_id = tool_use_id
        self.is_error = is_error
        self.content = content


class AssistantMessage:
    def __init__(
        self,
        content: list[Any],
        *,
        usage: dict[str, Any] | None = None,
        message_id: str | None = None,
        parent_tool_use_id: str | None = None,
        model: str = "mock-model",
        stop_reason: str = "end_turn",
    ) -> None:
        self.content = content
        self.model = model
        self.usage = usage or {}
        self.stop_reason = stop_reason
        if message_id is not None:
            self.message_id = message_id
        if parent_tool_use_id is not None:
            self.parent_tool_use_id = parent_tool_use_id


class UserMessage:
    """A tool-result UserMessage. ``tool_use_result`` carries the sub-agent
    breakdown when ``agent_id`` is supplied (exercises sub-agent synthesis)."""

    def __init__(
        self,
        tool_use_id: str,
        is_error: bool,
        content: Any,
        *,
        agent_id: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        self.content = [ToolResultBlock(tool_use_id, is_error, content)]
        result: dict[str, Any] = {"tool_use_id": tool_use_id, "is_error": is_error, "content": content}
        if agent_id is not None:
            result["agentId"] = agent_id
        if usage is not None:
            result["usage"] = usage
        self.tool_use_result = result


class ResultMessage:
    def __init__(
        self,
        *,
        session_id: str = "mock-session",
        usage: dict[str, Any] | None = None,
        model_usage: dict[str, Any] | None = None,
        total_cost_usd: float | None = 0.05,
        num_turns: int = 1,
        is_error: bool = False,
        subtype: str = "success",
        stop_reason: str | None = "end_turn",
        result: str | None = "done",
    ) -> None:
        self.session_id = session_id
        self.usage = usage or {}
        self.model_usage = model_usage
        self.total_cost_usd = total_cost_usd
        self.num_turns = num_turns
        self.is_error = is_error
        self.subtype = subtype
        self.stop_reason = stop_reason
        self.result = result


class StreamEvent:
    """Raw Anthropic stream event (``message_start`` / ``message_delta``)."""

    def __init__(self, event: dict[str, Any]) -> None:
        self.event = event


class PoisonMessage:
    """Survives the dispatch loop untouched (matches no ``_is_*`` guard) but
    makes ``format_messages`` raise — the ``type`` property it reads blows up.

    Exercises the ``_finalize`` ``_format_messages`` try/except placeholder.
    """

    @property
    def type(self) -> str:
        raise RuntimeError("boom formatting")


def message_start(message_id: str) -> StreamEvent:
    return StreamEvent({"type": "message_start", "message": {"id": message_id, "usage": {"output_tokens": 1}}})


def message_delta(output_tokens: int) -> StreamEvent:
    return StreamEvent({"type": "message_delta", "usage": {"output_tokens": output_tokens}})


def _async_query(events: list[Any]) -> Callable[..., Any]:
    """Build a ``mock_query`` that yields ``events`` in order (transport-aware)."""

    async def mock_query(prompt: Any, options: Any, transport: Any = None) -> Any:
        for event in events:
            yield event

    return mock_query


def _raising_query(events: list[Any], exc: BaseException) -> Callable[..., Any]:
    """Build a ``mock_query`` that yields ``events`` then raises ``exc``."""

    async def mock_query(prompt: Any, options: Any, transport: Any = None) -> Any:
        for event in events:
            yield event
        raise exc

    return mock_query


@dataclass
class ClaudeScenario:
    """One recorded Claude stream + how to drive it."""

    name: str
    build_query: Callable[[], Callable[..., Any]]
    timeout: float | None = None
    expects: type[BaseException] | None = None
    # When set, ``ClaudeCodeAgent._timed_out`` is patched to this constant so the
    # timeout-vs-crash classification is deterministic without real wall-clock.
    timed_out: bool | None = None
    # When set, patches ``time.monotonic`` (for the in-loop deadline-break case).
    monotonic: Callable[[], float] | None = None
    prompt: str = "do the thing"


def _deadline_break_query() -> Callable[..., Any]:
    """Yield one text turn, then flip the (faked) clock past the deadline before
    yielding a second turn — so the in-loop guard breaks and DISCARDS the second.
    """
    state = {"past_deadline": False}

    async def mock_query(prompt: Any, options: Any, transport: Any = None) -> Any:
        yield AssistantMessage([TextBlock("first turn, before the deadline")], message_id="msg_keep")
        # The next __anext__ runs here, after the first turn's loop body — flip
        # the clock so the SECOND turn's top-of-loop deadline check breaks.
        state["past_deadline"] = True
        yield AssistantMessage([TextBlock("second turn, past the deadline — discarded")], message_id="msg_drop")

    def fake_monotonic() -> float:
        return 2000.0 if state["past_deadline"] else 1000.0

    # Attach the clock + mutable state to the function so the scenario can wire
    # the clock as a patch and RESET the state before each run.
    mock_query.fake_monotonic = fake_monotonic  # type: ignore[attr-defined]
    mock_query.state = state  # type: ignore[attr-defined]
    return mock_query


def _build_deadline_break_scenario() -> ClaudeScenario:
    query = _deadline_break_query()

    def build_query() -> Callable[..., Any]:
        # The scenario object is built ONCE at module import but is replayed by
        # multiple parametrized tests (golden master + reconciliation invariant).
        # The query closure's `past_deadline` latches True after the first run and
        # would never flip back, so a second replay on the same xdist worker would
        # start already-past-deadline and fail to raise. Reset it on every run.
        query.state["past_deadline"] = False  # type: ignore[attr-defined]
        return query

    return ClaudeScenario(
        name="i_in_loop_deadline_break",
        build_query=build_query,
        timeout=100.0,
        expects=None,  # set below
        monotonic=query.fake_monotonic,  # type: ignore[attr-defined]
    )


# --- Scenario catalogue ------------------------------------------------------


def _scenario_a() -> ClaudeScenario:
    events = [
        AssistantMessage(
            [TextBlock("Hello, here is the answer.")],
            usage={"input_tokens": 50, "output_tokens": 20},
            message_id="msg_a",
        ),
        ResultMessage(usage={"input_tokens": 50, "output_tokens": 20}),
    ]
    return ClaudeScenario(name="a_single_text_turn", build_query=lambda: _async_query(events))


def _scenario_b() -> ClaudeScenario:
    tool_id = "toolu_b"
    events = [
        AssistantMessage(
            [ToolUseBlock(tool_id, "Bash", {"command": "ls"})],
            usage={"input_tokens": 80, "output_tokens": 12},
            message_id="msg_b",
        ),
        UserMessage(tool_id, False, "file1.py\nfile2.py"),
        ResultMessage(usage={"input_tokens": 80, "output_tokens": 12}),
    ]
    return ClaudeScenario(name="b_tool_use_result", build_query=lambda: _async_query(events))


def _scenario_c() -> ClaudeScenario:
    """Multi-emission sharing one id + delta distribution AND the
    message_start->message_delta->assistant read-after-write fallback.

    Call A: start(A) -> text(A) -> tool(A) -> delta(A): exercises dedup + the
    content-weighted output split across the two A emissions.
    Call B: start(B) -> delta(B) -> text(B): the delta arrives before any B
    emission, so it falls back to the stamp-on-next path consumed by text(B).
    """
    a_usage = {
        "input_tokens": 200,
        "output_tokens": 5,
        "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 0,
    }
    b_usage = {
        "input_tokens": 37,
        "output_tokens": 5,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 1000,
    }
    events = [
        message_start("msg_A"),
        AssistantMessage([TextBlock("brief plan")], usage=a_usage, message_id="msg_A"),
        AssistantMessage([ToolUseBlock("toolu_c", "Bash", {"command": "sleep 1"})], usage=a_usage, message_id="msg_A"),
        message_delta(179),
        UserMessage("toolu_c", False, "ok"),
        message_start("msg_B"),
        message_delta(34),
        AssistantMessage([TextBlock("done")], usage=b_usage, message_id="msg_B"),
        ResultMessage(usage={"input_tokens": 237, "output_tokens": 213}),
    ]
    return ClaudeScenario(name="c_multi_emission_delta", build_query=lambda: _async_query(events))


def _scenario_d() -> ClaudeScenario:
    """Sub-agent terminal generation rides on the Agent tool-result UserMessage."""
    agent_tool_id = "toolu_agent_d"
    sub_usage = {
        "input_tokens": 300,
        "output_tokens": 120,
        "cache_creation_input_tokens": 40,
        "cache_read_input_tokens": 60,
    }
    events = [
        AssistantMessage(
            [ToolUseBlock(agent_tool_id, "Agent", {"prompt": "compute the sum"})],
            usage={"input_tokens": 90, "output_tokens": 8},
            message_id="msg_d",
        ),
        UserMessage(agent_tool_id, False, "the sub-agent answer", agent_id="agent-d", usage=sub_usage),
        ResultMessage(usage={"input_tokens": 90, "output_tokens": 8}),
    ]
    return ClaudeScenario(name="d_subagent_terminal", build_query=lambda: _async_query(events))


def _scenario_e() -> ClaudeScenario:
    """ResultMessage carries model_usage (authoritative aggregate) AND usage that
    backfills the preceding id-less assistant emission (claude:969)."""
    model_usage = {
        "mock-model": {
            "inputTokens": 500,
            "outputTokens": 80,
            "cacheCreationInputTokens": 20,
            "cacheReadInputTokens": 30,
            "costUSD": 0.12,
        }
    }
    events = [
        AssistantMessage([TextBlock("an id-less emission")]),  # no message_id -> backfilled
        ResultMessage(
            usage={
                "input_tokens": 123,
                "output_tokens": 45,
                "cache_creation_input_tokens": 5,
                "cache_read_input_tokens": 7,
            },
            model_usage=model_usage,
        ),
    ]
    return ClaudeScenario(name="e_model_usage_and_backfill", build_query=lambda: _async_query(events))


def _scenario_f() -> ClaudeScenario:
    """Orphaned tool: ToolUse with no matching result -> unknown/unresolved."""
    events = [
        AssistantMessage(
            [ToolUseBlock("toolu_f", "Write", {"file_path": "x.py", "content": "code"})],
            usage={"input_tokens": 60, "output_tokens": 10},
            message_id="msg_f",
        ),
        ResultMessage(usage={"input_tokens": 60, "output_tokens": 10}),
    ]
    return ClaudeScenario(name="f_orphaned_tool", build_query=lambda: _async_query(events))


def _scenario_g() -> ClaudeScenario:
    """Crash whose partial record cannot be formatted -> placeholder agent_output."""
    events: list[Any] = [PoisonMessage()]
    return ClaudeScenario(
        name="g_crash_format_placeholder",
        build_query=lambda: _raising_query(events, RuntimeError("crash after poison")),
        expects=None,  # set below to AgentCrashError
    )


def _scenario_h1() -> ClaudeScenario:
    """Timeout: SIGKILL surfaces as ProcessError(exit=-9); classified as timeout."""
    return ClaudeScenario(
        name="h1_timeout_process_error",
        build_query=lambda: _raising_query([], ProcessError("killed", exit_code=-9, stderr="")),
        timeout=30.0,
        timed_out=True,
    )


def _scenario_h2() -> ClaudeScenario:
    """Non-timeout ProcessError -> AgentCrashError."""
    return ClaudeScenario(
        name="h2_process_error_crash",
        build_query=lambda: _raising_query([], ProcessError("boom", exit_code=1, stderr="bad config")),
    )


def _build_catalogue() -> list[ClaudeScenario]:
    from coder_eval.errors import AgentCrashError, TurnTimeoutError

    scenarios = [
        _scenario_a(),
        _scenario_b(),
        _scenario_c(),
        _scenario_d(),
        _scenario_e(),
        _scenario_f(),
    ]

    g = _scenario_g()
    g.expects = AgentCrashError
    scenarios.append(g)

    h1 = _scenario_h1()
    h1.expects = TurnTimeoutError
    scenarios.append(h1)

    h2 = _scenario_h2()
    h2.expects = AgentCrashError
    scenarios.append(h2)

    i = _build_deadline_break_scenario()
    i.expects = TurnTimeoutError
    scenarios.append(i)

    return scenarios


CLAUDE_SCENARIOS: list[ClaudeScenario] = _build_catalogue()


def _patches(scenario: ClaudeScenario) -> Iterator[Any]:
    """Context managers required to run ``scenario`` deterministically."""
    yield patch.object(claude_module, "query", scenario.build_query())
    if scenario.timeout is not None:
        # A timeout makes communicate construct a real SubprocessCLITransport;
        # replace it with a stub so no CLI is needed.
        yield patch.object(claude_module, "SubprocessCLITransport", MagicMock(return_value=MagicMock()))
    if scenario.timed_out is not None:
        constant = scenario.timed_out
        yield patch.object(ClaudeCodeAgent, "_timed_out", staticmethod(lambda *a, **k: constant))
    if scenario.monotonic is not None:
        yield patch.object(claude_module.time, "monotonic", scenario.monotonic)


async def run_claude_scenario(scenario: ClaudeScenario, working_dir: str) -> dict[str, Any]:
    """Run ``scenario`` and return the ``TurnRecord``/``pending_turn`` model_dump.

    Raises ``AssertionError`` if a crash/timeout scenario fails to raise its
    expected exception (so a refactor that silently swallows the failure is
    caught).
    """
    import pytest

    config = parse_agent_config(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)
    await agent.start(working_dir)

    with contextlib.ExitStack() as stack:
        for ctx in _patches(scenario):
            stack.enter_context(ctx)
        if scenario.expects is not None:
            with pytest.raises(scenario.expects):
                await agent.communicate(scenario.prompt, timeout=scenario.timeout)
            record = agent.pending_turn
            assert record is not None, f"{scenario.name}: pending_turn was not set on the failure path"
        else:
            record = await agent.communicate(scenario.prompt, timeout=scenario.timeout)

    return record.model_dump(mode="json")
