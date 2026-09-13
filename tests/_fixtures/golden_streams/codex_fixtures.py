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
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from openai_codex.generated.v2_all import Turn, TurnCompletedNotification

from coder_eval.agents.codex_agent import CodexAgent
from coder_eval.models import AgentKind, parse_agent_config


CODEX_MODEL = "gpt-5-codex"

# How far after the replay's start the rebased timeline begins. Small, but
# non-zero so the first generation window opens AFTER the AgentStartEvent and
# the head is a measured interval instead of a clamped inversion.
_REPLAY_LEAD_MS = 2


# --- Notification factories (mirror test_codex_agent) -----------------------


# Fixed epoch milliseconds, so every derived duration is deterministic and the
# golden snapshots pin a real value rather than a scrubbed clock read. It is a
# BASE, not a wall-clock claim: ``_rebase_notifications`` shifts the whole
# timeline onto the replay's own clock before the scenario runs, so the SDK
# stamps and the agent's own event stamps are commensurable. Left absolute,
# a codex replay recorded a ``harness_startup_ms`` of ~126 DAYS — the agent
# events are stamped ``now()`` while these sat in 2027 — which is a number no
# presence-only assertion can catch.
_T0_MS = 1_800_000_000_000


def _item(
    method: str,
    root: SimpleNamespace,
    *,
    started_at_ms: int | None = None,
    completed_at_ms: int | None = None,
) -> SimpleNamespace:
    """One item notification.

    ``started_at_ms`` / ``completed_at_ms`` sit on the PAYLOAD, beside ``item``,
    which is where the agent reads them from
    (``getattr(notification.payload, ...)``). They default to None so a
    scenario that says nothing about timing behaves exactly as before.
    """
    return SimpleNamespace(
        method=method,
        payload=SimpleNamespace(
            item=SimpleNamespace(root=root),
            started_at_ms=started_at_ms,
            completed_at_ms=completed_at_ms,
        ),
    )


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

    # (b) commandExecution start+complete, then a reply, + tokenUsage +
    # turn/completed. The reply is not decoration: without it the emission is
    # tool-ONLY, its whole window is the command's execution, and the
    # generation-window subtraction correctly reports 0ms of model time —
    # which would make this scenario blind to a harness that stopped
    # measuring generation at all.
    cmd = _command("cmd_b")
    scenarios.append(
        CodexScenario(
            name="b_command_execution",
            notifications=[
                _item("item/started", cmd, started_at_ms=_T0_MS),
                _item("item/completed", cmd, completed_at_ms=_T0_MS + 250),
                _delta("done"),
                _item(
                    "item/completed",
                    _agent_message("done"),
                    completed_at_ms=_T0_MS + 400,
                ),
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
                # Real bounds, and they are load-bearing rather than decorative:
                # with none, `_flush_message` takes `_ms_to_dt(None)` for BOTH
                # ends, which is two adjacent `datetime.now()` reads. Those
                # collide at microsecond resolution often enough that this
                # scenario failed `assert_timing_captured`'s
                # `completed_at > started_at` roughly one run in twenty under
                # parallel load, naming a different scenario each time.
                _item("item/completed", _reasoning(text=""), started_at_ms=_T0_MS, completed_at_ms=_T0_MS + 40),
                _delta("final answer"),
                _item(
                    "item/completed",
                    _agent_message("final answer"),
                    started_at_ms=_T0_MS + 40,
                    completed_at_ms=_T0_MS + 300,
                ),
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
                _item("item/started", cmd_d, started_at_ms=_T0_MS),
                _token_usage(inp=90, out=15, cached=0),
                _item("item/completed", cmd_d, completed_at_ms=_T0_MS + 400),
                _turn_completed(),
            ],
        )
    )

    # (e) orphan tool: item/started with no item/completed -> closed unresolved.
    scenarios.append(
        CodexScenario(
            name="e_orphan_tool",
            notifications=[
                # A real ItemStartedNotification always carries this (the SDK
                # marks it required), so the orphan's start IS knowable.
                _item("item/started", _command("cmd_orphan"), started_at_ms=_T0_MS),
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
                _item("item/started", spawn, started_at_ms=_T0_MS),
                _item("item/completed", spawn, completed_at_ms=_T0_MS + 120),
                _item("item/started", wait, started_at_ms=_T0_MS + 130),
                _item("item/completed", wait, completed_at_ms=_T0_MS + 900),
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
                # Bounded for the same reason as (c) above.
                _item(
                    "item/completed",
                    _agent_message("partial"),
                    started_at_ms=_T0_MS,
                    completed_at_ms=_T0_MS + 200,
                ),
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


def _rebase_notifications(notifications: list[Any]) -> list[Any]:
    """Shift every SDK item stamp from ``_T0_MS`` onto the replay's own clock.

    The scenario catalogue is built once at import with an absolute base, which
    keeps every DERIVED duration deterministic (a 250 ms command stays 250 ms).
    But the agent stamps its own lifecycle events with ``datetime.now()``, so
    left absolute the two clocks are months apart and the recorded head and
    tail are nonsense. Rebasing keeps the deltas and fixes the era.

    The offset puts the first item a beat AFTER the replay starts, so the head
    is a small positive interval rather than an inversion clamped to 0.0.
    """
    offset = int(datetime.now().timestamp() * 1000) - _T0_MS + _REPLAY_LEAD_MS
    rebased: list[Any] = []
    for note in notifications:
        payload = getattr(note, "payload", None)
        started = getattr(payload, "started_at_ms", None)
        completed = getattr(payload, "completed_at_ms", None)
        if payload is None or (started is None and completed is None):
            rebased.append(note)
            continue
        rebased.append(
            SimpleNamespace(
                method=note.method,
                payload=SimpleNamespace(
                    item=payload.item,
                    started_at_ms=None if started is None else started + offset,
                    completed_at_ms=None if completed is None else completed + offset,
                ),
            )
        )
    return rebased


async def run_codex_scenario(scenario: CodexScenario, working_dir: str) -> dict[str, Any]:
    """Run ``scenario`` with fakes and return the TurnRecord/pending_turn dump."""
    import pytest

    config = parse_agent_config(type=AgentKind.CODEX, model=CODEX_MODEL)
    agent = CodexAgent(config)
    agent.working_directory = Path(working_dir)
    agent.codex_client = SimpleNamespace(close=lambda: None)
    agent.thread = _FakeThread(_rebase_notifications(scenario.notifications))

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
