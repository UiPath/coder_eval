"""Codex golden-master scenarios: recorded notification streams + a runner.

The Codex agent pumps ``turn_handle.stream()`` via ``asyncio.to_thread(next, …)``,
so each scenario supplies an ordered list of notification objects (the same
``SimpleNamespace`` shapes the real SDK emits, mirroring the helpers in
``test_codex_agent``). The agent is wired with fakes, bypassing the real SDK
``start()``. ``CODEX_HOME`` is pointed at a sessions-less dir so sub-agent rollout
recovery short-circuits (the on-disk recovery path is covered by
``test_codex_agent`` — see plan decision D3).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from openai_codex.generated.v2_all import Turn, TurnCompletedNotification

from coder_eval.agents.codex_agent import CodexAgent
from coder_eval.models import AgentKind, parse_agent_config


CODEX_MODEL = "gpt-5-codex"


# --- Notification factories (mirror test_codex_agent) -----------------------


def _item(method: str, root: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(method=method, payload=SimpleNamespace(item=SimpleNamespace(root=root)))


def _delta(text: str) -> SimpleNamespace:
    return SimpleNamespace(method="item/agentMessage/delta", payload=SimpleNamespace(delta=text))


def _token_usage(
    *,
    inp: int,
    out: int,
    cached: int,
    reasoning: int = 0,
    total_inp: int | None = None,
    total_out: int | None = None,
    total_cached: int | None = None,
) -> SimpleNamespace:
    """A ``thread/tokenUsage/updated`` notification.

    ``last`` is this generation's per-message delta (drives per-message bucketing
    + the flush boundary); ``total`` is the cumulative turn figure (drives
    ``_token_usage_from_sdk``). ``total`` defaults to ``last`` for single-generation
    turns.
    """
    last = SimpleNamespace(
        input_tokens=inp, output_tokens=out, cached_input_tokens=cached, reasoning_output_tokens=reasoning
    )
    total = SimpleNamespace(
        input_tokens=inp if total_inp is None else total_inp,
        output_tokens=out if total_out is None else total_out,
        cached_input_tokens=cached if total_cached is None else total_cached,
    )
    return SimpleNamespace(
        method="thread/tokenUsage/updated",
        payload=SimpleNamespace(token_usage=SimpleNamespace(last=last, total=total)),
    )


def _turn_completed(*, items: list[dict[str, Any]] | None = None, duration_ms: int = 1500) -> SimpleNamespace:
    turn = Turn(
        id="turn_1",
        status="completed",
        duration_ms=duration_ms,
        started_at=None,
        completed_at=None,
        error=None,
        items=items or [],
        items_view="full",
    )
    return SimpleNamespace(method="turn/completed", payload=TurnCompletedNotification(thread_id="th_1", turn=turn))


def _command(tool_id: str = "cmd_1", *, exit_code: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        type="commandExecution",
        id=tool_id,
        command="echo hi",
        exit_code=exit_code,
        aggregated_output="hi\n",
        duration_ms=12,
    )


def _reasoning(text: str = "", item_id: str = "r1") -> SimpleNamespace:
    return SimpleNamespace(type="reasoning", id=item_id, content=[text] if text else [], summary=[])


def _agent_message(text: str, item_id: str = "m1") -> SimpleNamespace:
    return SimpleNamespace(type="agentMessage", id=item_id, text=text)


def _collab(
    tool: str,
    *,
    call_id: str,
    model: str | None = None,
    prompt: str | None = None,
    result: str | None = None,
    child_thread: str = "thread_child",
) -> SimpleNamespace:
    states: dict[str, Any] = {}
    if result is not None:
        states[child_thread] = SimpleNamespace(message=result, status="completed")
    return SimpleNamespace(
        type="collabAgentToolCall",
        id=call_id,
        tool=tool,
        model=model,
        prompt=prompt,
        status="completed",
        receiver_thread_ids=[child_thread],
        agents_states=states,
    )


# --- Scenario catalogue ------------------------------------------------------


@dataclass
class CodexScenario:
    name: str
    notifications: list[Any]
    expects: type[BaseException] | None = None


def _build_catalogue() -> list[CodexScenario]:
    from coder_eval.errors import AgentCrashError

    scenarios: list[CodexScenario] = []

    # (a) agentMessage only.
    scenarios.append(
        CodexScenario(
            name="a_agent_message_only",
            notifications=[
                _delta("Hello world"),
                _item("item/completed", _agent_message("Hello world")),
                _token_usage(inp=100, out=40, cached=8),
                _turn_completed(),
            ],
        )
    )

    # (b) commandExecution start+complete + tokenUsage + turn/completed.
    cmd = _command("cmd_b")
    scenarios.append(
        CodexScenario(
            name="b_command_execution",
            notifications=[
                _item("item/started", cmd),
                _item("item/completed", cmd),
                _token_usage(inp=120, out=30, cached=0),
                _turn_completed(),
            ],
        )
    )

    # (c) reasoning placeholder with reasoning tokens (thinking/action split).
    scenarios.append(
        CodexScenario(
            name="c_reasoning_placeholder",
            notifications=[
                _item("item/completed", _reasoning(text="")),
                _delta("final answer"),
                _item("item/completed", _agent_message("final answer")),
                _token_usage(inp=100, out=50, cached=8, reasoning=20),
                _turn_completed(),
            ],
        )
    )

    # (d) is_error patched cross-flush: item/completed (error) lands AFTER the
    # generation's tokenUsage flush, patching the already-flushed block.
    cmd_d = _command("cmd_d", exit_code=1)
    scenarios.append(
        CodexScenario(
            name="d_cross_flush_is_error",
            notifications=[
                _item("item/started", cmd_d),
                _token_usage(inp=90, out=15, cached=0),
                _item("item/completed", cmd_d),
                _turn_completed(),
            ],
        )
    )

    # (e) orphan tool: item/started with no item/completed -> closed unresolved.
    scenarios.append(
        CodexScenario(
            name="e_orphan_tool",
            notifications=[
                _item("item/started", _command("cmd_orphan")),
                _delta("done"),
                _turn_completed(),
            ],
        )
    )

    # (f) collab spawn whose rollout is NOT found -> in-memory fallback nests the
    # returned text (no on-disk recovery; see D3).
    spawn = _collab("spawnAgent", call_id="call_spawn", model="gpt-5.5", prompt="sum 1..100")
    wait = _collab("wait", call_id="call_wait", result="5050")
    scenarios.append(
        CodexScenario(
            name="f_collab_fallback",
            notifications=[
                _item("item/started", spawn),
                _item("item/completed", spawn),
                _item("item/started", wait),
                _item("item/completed", wait),
                _delta("done"),
                _turn_completed(),
            ],
        )
    )

    # (g) turn/completed with items but no streamed messages -> items rebuild.
    scenarios.append(
        CodexScenario(
            name="g_items_rebuild",
            notifications=[
                _turn_completed(items=[{"type": "agentMessage", "id": "m1", "text": "rebuilt from items"}]),
            ],
        )
    )

    # (h) stream ends without turn/completed -> RuntimeError -> crash; the partial
    # is built via the finally/_flush_message path.
    scenarios.append(
        CodexScenario(
            name="h_no_turn_completed_crash",
            notifications=[
                _delta("partial"),
                _item("item/completed", _agent_message("partial")),
                _token_usage(inp=100, out=40, cached=8),
            ],
            expects=AgentCrashError,
        )
    )

    return scenarios


CODEX_SCENARIOS: list[CodexScenario] = _build_catalogue()


class _FakeTurnHandle:
    def __init__(self, notifications: list[Any]) -> None:
        self._notifications = notifications

    def stream(self) -> Any:
        return iter(self._notifications)

    def interrupt(self) -> None:  # pragma: no cover - not exercised
        pass


class _FakeThread:
    def __init__(self, notifications: list[Any]) -> None:
        self._notifications = notifications

    def turn(self, _user_input: str) -> _FakeTurnHandle:
        return _FakeTurnHandle(self._notifications)


async def run_codex_scenario(scenario: CodexScenario, working_dir: str) -> dict[str, Any]:
    """Run ``scenario`` with fakes and return the TurnRecord/pending_turn dump."""
    import pytest

    config = parse_agent_config(type=AgentKind.CODEX, model=CODEX_MODEL)
    agent = CodexAgent(config)
    agent.working_directory = Path(working_dir)
    agent.codex_client = SimpleNamespace(close=lambda: None)
    agent.thread = _FakeThread(scenario.notifications)

    # Point CODEX_HOME at a sessions-less dir so sub-agent rollout recovery
    # short-circuits instead of polling the real ~/.codex.
    with patch.dict(os.environ, {"CODEX_HOME": working_dir}):
        if scenario.expects is not None:
            with pytest.raises(scenario.expects):
                await agent.communicate("do it")
            record = agent.pending_turn
            assert record is not None, f"{scenario.name}: pending_turn was not set on the failure path"
        else:
            record = await agent.communicate("do it")

    return record.model_dump(mode="json")
