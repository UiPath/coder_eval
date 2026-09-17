"""``coder_eval.testing``: the sensors a plugin shares with the in-tree harness suites."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import pytest

from coder_eval.models import AgentKind, CommandTelemetry, TokenUsage
from coder_eval.plugins import ensure_plugins_loaded
from coder_eval.streaming.emitter import Generation, TurnEmitter
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    StreamEvent,
    ToolEndEvent,
    ToolStartEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from coder_eval.testing import (
    ScriptedClock,
    Tick,
    assert_identity_closes,
    assert_stream_balanced,
    conformance,
    enforced_cells,
    replay,
)
from coder_eval.timing import close_window


ORIGIN = datetime(2026, 9, 16, 9, 0, 0)


def at(ms: float) -> datetime:
    return ORIGIN + timedelta(milliseconds=ms)


class _Decoder:
    """A minimal reducer: a window per ``end``, tiled from the previous end unless ``untiled``."""

    def __init__(self, emitter: TurnEmitter, *, untiled: bool = False) -> None:
        self.emitter = emitter
        self.untiled = untiled
        self.mark: datetime | None = None
        self.turn_start: datetime | None = None

    def __call__(self, event: dict[str, Any]) -> None:
        kind = event["type"]
        if kind == "start":
            self.turn_start = self.emitter.now()
            self.emitter.begin_inner_turn(event["id"])
        elif kind == "tool_start":
            self.emitter.open_tool(event["id"], "Bash", {})
        elif kind == "tool_end":
            from coder_eval.streaming.events import ToolEndStatus

            self.emitter.close_tool(event["id"], status=ToolEndStatus.OK)
        elif kind == "end":
            now = self.emitter.now()
            assert self.turn_start is not None
            mark = self.turn_start if self.untiled or self.mark is None else self.mark
            tokens = TokenUsage(output_tokens=1)
            self.emitter.add_generation(
                message_id=None,
                window=close_window(mark=mark, now=now, item_start=self.turn_start),
                parts=[Generation(blocks=[], tokens=tokens)],
            )
            self.mark = now
            self.emitter.end_inner_turn(tokens=tokens)


_STREAM: list[Any] = [
    Tick(500),
    {"type": "start", "id": "t1"},
    Tick(700),
    {"type": "tool_start", "id": "c1"},
    Tick(1200),
    {"type": "tool_end", "id": "c1"},
    Tick(2000),
    {"type": "end"},
    Tick(2600),
    {"type": "start", "id": "t2"},
    Tick(3000),
    {"type": "end"},
    Tick(3500),
]


class TestReplay:
    def test_ticks_script_the_bracket_and_the_decoder_sees_the_rest(self) -> None:
        result = replay(_STREAM, _Decoder, clock=ScriptedClock(ORIGIN))
        assert (result.started_at, result.ended_at) == (at(0), at(3500))
        assert result.outcome.status is AgentEndStatus.COMPLETED
        assert result.record is result.outcome.record
        assert [c.tool_id for c in result.record.commands] == ["c1"]
        assert isinstance(result.events[0], AgentStartEvent) and isinstance(result.events[-1], AgentEndEvent)

    def test_a_scripted_clock_before_any_tick_reads_its_origin(self) -> None:
        assert ScriptedClock(ORIGIN).now() == ORIGIN

    def test_end_decides_how_the_turn_ends(self) -> None:
        result = replay([{"type": "start", "id": "t"}], _Decoder, clock=ScriptedClock(ORIGIN), end=_fail)
        assert result.outcome.status is AgentEndStatus.CRASHED

    def test_a_decoder_exception_propagates(self) -> None:
        with pytest.raises(KeyError):
            replay([{"no": "type"}], _Decoder, clock=ScriptedClock(ORIGIN))


def _fail(decoder: _Decoder) -> Any:
    return decoder.emitter.fail(AgentEndStatus.CRASHED, "stopped")


class TestAssertIdentityCloses:
    def test_a_tiled_replay_closes(self) -> None:
        result = replay(_STREAM, _Decoder, clock=ScriptedClock(ORIGIN))
        assert_identity_closes(result.record, started_at=result.started_at, ended_at=result.ended_at)

    def test_an_untiled_replay_is_caught(self) -> None:
        result = replay(_STREAM, lambda e: _Decoder(e, untiled=True), clock=ScriptedClock(ORIGIN))
        with pytest.raises(AssertionError, match="booked nowhere"):
            assert_identity_closes(result.record, started_at=result.started_at, ended_at=result.ended_at)

    def test_a_record_without_a_head_fails(self) -> None:
        result = replay([], _Decoder, clock=ScriptedClock(ORIGIN))
        with pytest.raises(AssertionError, match="head and tail"):
            assert_identity_closes(result.record, started_at=result.started_at, ended_at=result.ended_at)


def _tool(tool_id: str) -> CommandTelemetry:
    return CommandTelemetry(tool_name="Bash", tool_id=tool_id, timestamp=ORIGIN)


def _clean() -> list[StreamEvent]:
    return [
        AgentStartEvent(task_id="t"),
        TurnStartEvent(task_id="t", turn_id="a"),
        ToolStartEvent(task_id="t", turn_id="a", tool=_tool("c1")),
        ToolEndEvent(task_id="t", turn_id="a", tool=_tool("c1")),
        TurnEndEvent(task_id="t", turn_id="a", tokens=TokenUsage(output_tokens=3)),
        AgentEndEvent(task_id="t", usage=TokenUsage(output_tokens=3)),
    ]


class TestAssertStreamBalanced:
    def test_a_clean_stream_passes(self) -> None:
        assert_stream_balanced(_clean())

    @pytest.mark.parametrize(
        ("mutate", "match"),
        [
            (lambda ev: ev[:-1], "0 AgentEndEvent"),
            (lambda ev: [ev[0], *ev], "2 AgentStartEvent"),
            (lambda ev: ev[1:], "first event is TurnStartEvent"),
            (lambda ev: [*ev[:4], ev[5]], "still open"),
            (lambda ev: [ev[0], ev[1], ev[1], *ev[2:]], "started while"),
            (lambda ev: [ev[0], ev[4], ev[5]], "ended while the open turn is None"),
            (lambda ev: [*ev[:3], *ev[4:]], "ended 0 times"),
            (lambda ev: [*ev[:4], ev[3], *ev[4:]], "ended 2 times"),
            (lambda ev: [ev[0], ev[1], ev[3], *ev[4:]], "ended without a start"),
            (
                lambda ev: [*ev[:5], AgentEndEvent(task_id="t", usage=TokenUsage(output_tokens=2))],
                "output_tokens sum 3 > AgentEndEvent.usage 2",
            ),
        ],
    )
    def test_each_violation_is_named(
        self, mutate: Callable[[list[StreamEvent]], list[StreamEvent]], match: str
    ) -> None:
        with pytest.raises(AssertionError, match=match):
            assert_stream_balanced(mutate(_clean()))

    def test_an_empty_stream_fails(self) -> None:
        with pytest.raises(AssertionError, match="empty"):
            assert_stream_balanced([])


def _contract(kind: AgentKind) -> Any:
    from coder_eval.agents.registry import AgentRegistry

    ensure_plugins_loaded()
    registration = AgentRegistry.get(kind)
    assert registration is not None
    return registration.agent_class.contract


class TestEnforcedCells:
    def test_claude_code_has_one_cell_per_permission_mode(self) -> None:
        cells = enforced_cells(_contract(AgentKind.CLAUDE_CODE), "claude-code")
        assert ("claude-code", "system_prompt") in cells
        assert {c for c in cells if c[1].startswith("permission_mode=")} == {
            ("claude-code", f"permission_mode={m.value}") for m in _contract(AgentKind.CLAUDE_CODE).permission_modes
        }

    def test_the_noop_harness_enforces_nothing(self) -> None:
        assert enforced_cells(_contract(AgentKind.NONE), "none") == set()

    @pytest.mark.parametrize("kind", [k for k in AgentKind if k not in (AgentKind.UNKNOWN, AgentKind.NONE)])
    def test_every_in_tree_harness_enforces_its_system_prompt_cell(self, kind: AgentKind) -> None:
        assert (kind.value, "system_prompt") in enforced_cells(_contract(kind), kind.value)


async def _noop() -> None:
    return None


class TestConformance:
    async def test_the_noop_harness_conforms_with_no_probes(self) -> None:
        await conformance("none", {})

    async def test_a_missing_probe_fails(self) -> None:
        with pytest.raises(AssertionError, match="missing"):
            await conformance("pi", {})

    async def test_an_extra_probe_fails(self) -> None:
        probes: dict[tuple[str, str], Callable[[], Awaitable[None]]] = {("none", "system_prompt"): _noop}
        with pytest.raises(AssertionError, match="extra"):
            await conformance("none", probes)

    async def test_an_unregistered_kind_fails(self) -> None:
        with pytest.raises(AssertionError, match="not registered"):
            await conformance("no-such-harness", {})
