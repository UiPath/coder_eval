"""The four-bucket identity, to the millisecond, on every harness.

    head + Σ generation + UNION(tool) + tail == the turn's own span

This is the committed MAGNITUDE sensor, and it exists because nothing else in
the suite is one:

* the golden corpus masks ``generation_duration_ms``, both window bounds, both
  ``execution_*_at`` stamps and both head/tail fields to a placeholder
  (``_scrub.py::SCRUB_KEYS``), so a snapshot records that a window was measured
  and never what it measured — a timing value can move by seconds with every
  golden test still green;
* ``_scrub.py::assert_timing_captured``'s own identity check is ONE-SIDED
  (``overshoot <= ...``), so an UNDERCOUNT — a bucket claiming less time than
  it should, which is the defect class this whole area keeps producing — passes
  it silently. It cannot be made two-sided either: the replays run in ~0.3 ms of
  synthetic wall clock, where a relative bound is vacuous;
* ``scripts/timing/decompose_run.py --max-residual-pct`` IS two-sided, but needs
  live ``task.json`` files.

Magnitudes are only real where a scripted clock makes them real, so each case
drives the harness's own REDUCER with a clock it moves by hand, then feeds the
messages and commands it produced through a real ``EventCollector`` — the same
seam production uses to compute the head and the tail. Every number asserted is
therefore one the harness computed, against a span the test declared.

Three clock-injection styles are needed, and all three already exist in the
per-harness suites (this module reuses their idiom rather than inventing a
fourth):

* an injected ``TurnClock`` — pi and antigravity take ``clock=`` / build one
  through a patched ``TurnClock`` factory;
* a ``datetime`` SUBCLASS monkeypatched onto the module — opencode, which also
  calls ``datetime.fromtimestamp`` through the same global (see
  ``tests/test_opencode_agent.py``'s ``_SteppedClock`` for why a stub breaks);
* ``time.monotonic`` AND ``datetime`` both patched — claude-code. Its window is
  wall-derived now, but ``turn_start_time`` and the turn deadline still read
  ``time.monotonic()``, so patching only one leaves the reducer straddling a
  real clock and a scripted one.

Codex is the fifth and takes its stamps from SDK epoch milliseconds rather than
from any host clock, so its case scripts those stamps directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from coder_eval.models import (
    AgentKind,
    AssistantMessage,
    CommandTelemetry,
    TokenUsage,
    TranscriptMessage,
    TurnRecord,
    parse_agent_config,
)
from coder_eval.streaming.callbacks import CompositeStreamCallback
from coder_eval.streaming.collector import EventCollector, main_thread_tool_spans
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    ToolEndEvent,
    ToolEndStatus,
)
from coder_eval.timing import union_ms


# The two CLI harnesses (opencode, codex) report their stamps as epoch
# milliseconds and convert them with the real ``datetime.fromtimestamp``. So
# the shared origin is DERIVED from an epoch value rather than written as a
# wall time: that is what puts a scripted ``now()`` read and a converted CLI
# stamp on ONE timeline, without patching the conversion itself.
EPOCH_MS = 1_800_000_000_000
BASE = datetime.fromtimestamp(EPOCH_MS / 1000.0)


def at(ms: float) -> datetime:
    return BASE + timedelta(milliseconds=ms)


@dataclass(frozen=True)
class Turn:
    """What one harness case produces: a scripted span plus what it recorded.

    ``started_ms`` / ``ended_ms`` are the turn's own bounds — where the
    ``AgentStartEvent`` and ``AgentEndEvent`` land. Everything else came out of
    the reducer.
    """

    started_ms: float
    ended_ms: float
    messages: list[TranscriptMessage]
    commands: list[CommandTelemetry]


def _record(turn: Turn) -> TurnRecord:
    """Reduce a scripted turn through the production collector seam.

    Deliberately the real ``EventCollector`` rather than a direct
    ``decompose_turn`` call: the head and the tail are only as correct as the
    arguments that seam builds for them, and those (the main-thread generation
    filter, the command span set) are half of what this file is asserting.
    """
    collector = EventCollector()
    collector.on_event(AgentStartEvent(task_id="t", prompt="go", iteration=1, timestamp=at(turn.started_ms)))
    for command in turn.commands:
        collector.on_event(ToolEndEvent(task_id="t", turn_id="turn", tool=command, status=ToolEndStatus.OK))
    collector.on_event(
        AgentEndEvent(
            task_id="t",
            status=AgentEndStatus.COMPLETED,
            iteration=1,
            user_input="go",
            messages=turn.messages,
            usage=TokenUsage(),
            duration_seconds=(turn.ended_ms - turn.started_ms) / 1000.0,
            timestamp=at(turn.ended_ms),
        )
    )
    return collector.build_turn_record()


def assert_identity_closes(turn: Turn) -> None:
    """head + Σ generation + UNION(tool) + tail == the scripted span, EXACTLY.

    ``pytest.approx`` rather than an order-of-magnitude bound: every input is
    scripted, so the only slack is float representation. A bound wide enough to
    absorb a real defect is the sensor this module exists to replace.

    MAIN THREAD ONLY on both sides, and both through production's own helpers:
    a sub-agent's generations bubble into the same stream, and the spawning
    Agent call's own interval already spans them and their tools.
    """
    record = _record(turn)
    span_ms = turn.ended_ms - turn.started_ms

    generation_ms = sum(
        m.generation_duration_ms or 0.0
        for m in record.messages
        if isinstance(m, AssistantMessage) and m.parent_tool_use_id is None
    )
    # The PRODUCTION selector, not a re-derivation of it. Unioning every command
    # would assert a different identity than the collector computes: production,
    # the golden sensor, the live residual gate and the HTML report all exclude
    # a sub-agent's own tools (the spawning Agent call's interval already spans
    # them). No case here has a child command yet, so a local copy stayed green
    # while quietly testing something else — and the first sub-agent case added
    # would have reported a false regression.
    tool_ms = union_ms(main_thread_tool_spans(record.messages, record.commands))
    assert record.harness_startup_ms is not None, "a turn that generated has a measured head"
    assert record.harness_teardown_ms is not None, "a turn that generated has a measured tail"
    bucket_sum = record.harness_startup_ms + generation_ms + tool_ms + record.harness_teardown_ms

    assert bucket_sum == pytest.approx(span_ms), (
        f"the four buckets sum to {bucket_sum:.4f} ms against a {span_ms:.4f} ms turn "
        f"(off by {bucket_sum - span_ms:+.4f} ms): head={record.harness_startup_ms:.4f}, "
        f"generation={generation_ms:.4f}, tool_union={tool_ms:.4f}, tail={record.harness_teardown_ms:.4f}. "
        "They tile the turn, so a sum UNDER it means some interval is booked nowhere — the "
        "defect class the golden corpus cannot see — and a sum OVER it means one is booked twice."
    )


# --------------------------------------------------------------------------
# pi — an injected TurnClock
# --------------------------------------------------------------------------


class _InjectedClock:
    """A ``TurnClock`` stand-in the test moves by hand, in ms from ``BASE``.

    Injected rather than monkeypatched: pi and antigravity derive every wall
    stamp from their per-turn clock, so patching the module's ``datetime``
    would no longer reach them and the case would quietly measure the real
    clock and pass by accident.
    """

    def __init__(self, at_ms: float = 0.0) -> None:
        self.at_ms = at_ms

    def now(self) -> datetime:
        return at(self.at_ms)


def _pi_turn(*, untile: bool = False) -> Turn:
    """Two tiled windows around a tool, with a real head and a real tail.

    The tool closes INSIDE the first window rather than across the boundary —
    that case is pinned by ``tests/test_pi_agent.py``. What this adds is the
    two ends: pi's first window opens at its first ``turn_start``, so the CLI
    boot before it is head, and the turn runs on past the last ``turn_end``.

    ``untile`` reproduces the defect pi actually shipped with — each window
    measured from its OWN ``turn_start`` instead of from the previous flush —
    so that the sensor can be shown to catch it. See
    ``test_the_sensor_sees_a_window_that_stops_tiling``.
    """
    from coder_eval.agents.pi_agent import _PiTurnState

    payload = {"message": {"role": "assistant", "usage": {"input": 10, "output": 5}, "stopReason": "stop"}}
    clock = _InjectedClock()
    state = _PiTurnState(task_id="t", iteration=1, user_input="go", model="m", clock=clock)
    commands: list[CommandTelemetry] = []
    state.bind(lambda e: commands.append(e.tool) if isinstance(e, ToolEndEvent) else None)

    clock.at_ms = 500  # CLI boot: head
    state.on_turn_start()
    clock.at_ms = 700
    state.on_tool_execution_start({"toolCallId": "c1", "toolName": "bash", "args": {}})
    clock.at_ms = 1200
    state.on_tool_execution_end({"toolCallId": "c1", "result": "ok"})
    clock.at_ms = 2000
    state.on_turn_end(payload)
    clock.at_ms = 2600  # the inter-turn gap, which window 2 tiles back over
    state.on_turn_start()
    if untile:
        state.gen_mark = None
    clock.at_ms = 3000
    state.on_turn_end(payload)

    return Turn(
        started_ms=0.0,
        ended_ms=3500.0,  # process teardown after the last turn: tail
        messages=list(state.messages),
        commands=commands,
    )


# --------------------------------------------------------------------------
# opencode — a datetime subclass on the module
# --------------------------------------------------------------------------


class _SteppedDatetime(datetime):
    """A clock the test moves by hand, in ms from ``BASE``.

    Subclasses ``datetime`` rather than stubbing it, because the reducer also
    calls ``datetime.fromtimestamp`` through the same module global to convert
    the CLI's epoch stamps, and that must keep resolving to the real
    implementation — the CLI's stamps and the reducer's own ``now()`` reads
    have to land on ONE timeline for the arithmetic to mean anything.
    """

    at_ms = 0.0

    @staticmethod
    def now(tz: Any = None) -> datetime:  # type: ignore[override]
        return at(_SteppedDatetime.at_ms)


def _opencode_turn(monkeypatch: pytest.MonkeyPatch) -> Turn:
    """The same two-window shape, driven through OpenCode's step stream."""
    from coder_eval.agents import opencode_agent as opencode_module
    from coder_eval.agents.opencode_agent import _OpenCodeTurnState

    monkeypatch.setattr(opencode_module, "datetime", _SteppedDatetime)
    state = _OpenCodeTurnState(task_id="t", iteration=1, user_input="go", model="m")
    commands: list[CommandTelemetry] = []
    state.bind(lambda e: commands.append(e.tool) if isinstance(e, ToolEndEvent) else None)
    finish = {"reason": "stop", "tokens": {"input": 10, "output": 5}}

    _SteppedDatetime.at_ms = 500  # Node boot: head
    state.on_step_start({"messageID": "m1"})
    _SteppedDatetime.at_ms = 1200
    state.on_tool_use(
        {
            "callID": "c1",
            "tool": "bash",
            "state": {"status": "completed", "time": {"start": EPOCH_MS + 700, "end": EPOCH_MS + 1200}},
        }
    )
    _SteppedDatetime.at_ms = 2000
    state.on_step_finish(finish)
    _SteppedDatetime.at_ms = 2600
    state.on_step_start({"messageID": "m2"})
    _SteppedDatetime.at_ms = 3000
    state.on_step_finish(finish)

    return Turn(started_ms=0.0, ended_ms=3500.0, messages=list(state.messages), commands=commands)


# --------------------------------------------------------------------------
# antigravity — an injected TurnClock, at the reducer
# --------------------------------------------------------------------------


def _antigravity_turn() -> Turn:
    """One interleaved window per generation, with a tool inside the first.

    Driven at ``_AntigravityTurnState`` rather than through ``communicate()``:
    the fake conversation yields with no delay, so an end-to-end run cannot
    distinguish a window that opened at the turn's start from one that opened
    later. The state's own ``_gen_mark_wall`` is stamped at construction, so
    constructing it AFTER the scripted agent start is what gives this harness a
    measurable head at all.
    """
    from coder_eval.agents.antigravity_agent import AntigravityAgent, _AntigravityTurnState
    from tests._fixtures.golden_streams.antigravity_fixtures import _step, _tc, _usage

    agent = AntigravityAgent(parse_agent_config(type=AgentKind.ANTIGRAVITY, model="gemini-3.5-flash"))
    collector = EventCollector()
    clock = _InjectedClock(at_ms=500)  # dispatch before the first Step: head
    state = _AntigravityTurnState(
        agent=agent,
        emit=CompositeStreamCallback([collector]),
        task_id="t",
        turn_id="turn",
        collector=collector,
        user_input="go",
        iteration=1,
        model="gemini-3.5-flash",
        turn_start_time=0.0,
        clock=clock,
    )

    clock.at_ms = 700
    state.process_step(
        _step(
            "TOOL_CALL",
            "ACTIVE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", "c1", {"command_line": "ls"})],
        )
    )
    clock.at_ms = 1200
    state.process_step(
        _step(
            "TOOL_CALL",
            "DONE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", "c1", {"command_line": "ls", "exit_code": 0})],
        )
    )
    clock.at_ms = 2000
    state.process_step(_step("THINKING", "DONE", thinking="plan", usage=_usage(100, 0, 5, 5)))
    clock.at_ms = 3000
    state.process_step(_step("TEXT_RESPONSE", "DONE", content="done", complete=True, usage=_usage(200, 0, 10, 0)))

    return Turn(started_ms=0.0, ended_ms=3500.0, messages=list(state.messages), commands=list(state.commands))


# --------------------------------------------------------------------------
# codex — scripted SDK epoch-millisecond stamps
# --------------------------------------------------------------------------


def _codex_turn() -> Turn:
    """Two tiled windows, the first SPLIT across two sub-messages.

    Codex is the only harness that cuts one window into several messages
    (thinking and action, apportioned by output-token share), and they share
    one pair of bounds. The identity has to close over the GROUP, so this case
    drives that split deliberately rather than the simpler one-message shape.

    Its stamps are the SDK's own epoch milliseconds, unreachable from any host
    clock, so ``_flush_message`` is driven with them set by hand — the idiom
    ``tests/test_codex_agent.py::TestFlushMessageWindowBounds`` already uses.
    """
    from coder_eval.agents.codex_agent import CodexAgent, _CodexTurnState, _ms_to_dt
    from coder_eval.models import ContentBlock

    agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="gpt-5.5"))
    collector = EventCollector()
    state = _CodexTurnState(
        agent,
        emit=CompositeStreamCallback([collector]),
        task_id="t",
        turn_id="turn",
        collector=collector,
        commands=[],
        messages=[],
        user_input="go",
        iteration=1,
        turn_start_time=0.0,
    )
    command = CommandTelemetry(
        tool_name="bash",
        tool_id="c1",
        timestamp=_ms_to_dt(EPOCH_MS + 700),
        execution_started_at=_ms_to_dt(EPOCH_MS + 700),
        execution_completed_at=_ms_to_dt(EPOCH_MS + 1200),
        duration_ms=500.0,
        result_status="success",
    )
    state.commands.append(command)

    # Window 1 — no mark yet, so it opens at its own first item (+500): the CLI
    # boot before that is head. Thinking + text, so the flush cuts two
    # sub-messages sharing the window.
    state.open_blocks = [
        ContentBlock(block_type="thinking", sequence=0, thinking="plan"),
        ContentBlock(block_type="text", sequence=0, text="answer"),
    ]
    state.open_start_ms = EPOCH_MS + 500
    state.open_end_ms = EPOCH_MS + 2000
    state._flush_message(
        SimpleNamespace(input_tokens=500, cached_input_tokens=0, output_tokens=100, reasoning_output_tokens=80)
    )

    # Window 2 — tiles back from the mark (+2000), covering the gap before its
    # own first item at +2600.
    state.open_blocks = [ContentBlock(block_type="text", sequence=0, text="more")]
    state.open_start_ms = EPOCH_MS + 2600
    state.open_end_ms = EPOCH_MS + 3000
    state._flush_message(SimpleNamespace(input_tokens=10, cached_input_tokens=0, output_tokens=5))

    return Turn(started_ms=0.0, ended_ms=3500.0, messages=list(state.messages), commands=[command])


# --------------------------------------------------------------------------
# claude-code — BOTH the monotonic and the wall clock patched
# --------------------------------------------------------------------------


def _claude_turn(monkeypatch: pytest.MonkeyPatch) -> Turn:
    """A tool call between two emissions, with a real head and a real tail.

    Both module globals are patched off one counter. The window itself is
    wall-derived, but ``turn_start_time`` and the deadline still read
    ``time.monotonic()``, so patching only one leaves the reducer straddling a
    real clock and a scripted one.

    The first `message_start` re-seeds the window, so the CLI spawn and the
    query build before it are head rather than msg0's generation. That a LATER
    one must not re-seed is asserted directly in
    `tests/test_agent_telemetry.py`; here it shows up as the windows still
    tiling.

    Note where its windows do NOT tile: the tool result resets both marks, so
    the interval between the emission that ISSUED the call and the result is
    left outside every window. That gap is the tool's own execution, which is
    exactly what the tool bucket claims — which is why the identity still
    closes to the millisecond.
    """
    from coder_eval.agents import claude_code_agent as claude_module
    from coder_eval.agents.claude_code_agent import ClaudeCodeAgent, _ClaudeTurnState
    from coder_eval.streaming.events import AgentEndStatus as _AgentEndStatus
    from tests._fixtures.golden_streams.claude_fixtures import AssistantMessage as SdkAssistantMessage
    from tests._fixtures.golden_streams.claude_fixtures import ToolUseBlock, UserMessage, message_start

    class _Stepped(datetime):
        at_ms = 0.0

        @staticmethod
        def now(tz: Any = None) -> datetime:  # type: ignore[override]
            return at(_Stepped.at_ms)

    def _monotonic() -> float:
        return _Stepped.at_ms / 1000.0

    monkeypatch.setattr(claude_module, "datetime", _Stepped)
    monkeypatch.setattr(claude_module, "time", SimpleNamespace(monotonic=_monotonic))

    agent = ClaudeCodeAgent(parse_agent_config(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits"))
    collector = EventCollector()
    commands: list[CommandTelemetry] = []

    _Stepped.at_ms = 500  # the turn state is built here; the head runs past it
    state = _ClaudeTurnState(
        agent,
        emit=CompositeStreamCallback(
            [
                collector,
                SimpleNamespace(on_event=lambda e: commands.append(e.tool) if isinstance(e, ToolEndEvent) else None),
            ]
        ),
        collector=collector,
        task_id="t",
        user_input="go",
        iteration=1,
        max_turns=None,
        log=agent._log,
        turn_start_time=_monotonic(),
        deadline=None,
    )

    # The stream really does put `message_start` before the emission it
    # announces — the recorded corpus shows it and the SDK guarantees it — and
    # the FIRST one is what re-seeds the window, so an ordering this case got
    # wrong would silently stop exercising the re-seed at all.
    _Stepped.at_ms = 800
    state.on_stream_event(message_start("m1"))
    _Stepped.at_ms = 1000
    state.on_assistant_message(
        SdkAssistantMessage(
            [ToolUseBlock("c1", "Bash", {"command": "ls"})],
            usage={"input_tokens": 10, "output_tokens": 5},
            message_id="m1",
        )
    )
    _Stepped.at_ms = 1800  # the tool ran for the whole gap
    state.on_user_message(UserMessage("c1", False, "ok"))
    _Stepped.at_ms = 2000
    state.on_stream_event(message_start("m2"))  # does NOT re-seed: once per turn
    _Stepped.at_ms = 2500
    state.on_assistant_message(SdkAssistantMessage([], usage={"input_tokens": 10, "output_tokens": 5}, message_id="m2"))
    state.finalize(_AgentEndStatus.COMPLETED)

    return Turn(started_ms=0.0, ended_ms=3000.0, messages=list(state.sdk_messages), commands=commands)


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


def test_pi_buckets_tile_the_turn():
    assert_identity_closes(_pi_turn())


def test_opencode_buckets_tile_the_turn(monkeypatch: pytest.MonkeyPatch):
    assert_identity_closes(_opencode_turn(monkeypatch))


def test_antigravity_buckets_tile_the_turn():
    assert_identity_closes(_antigravity_turn())


def test_codex_buckets_tile_the_turn():
    assert_identity_closes(_codex_turn())


def test_claude_code_buckets_tile_the_turn(monkeypatch: pytest.MonkeyPatch):
    assert_identity_closes(_claude_turn(monkeypatch))


def test_every_built_in_harness_has_a_case():
    """A sensor that silently covers four of five is worse than one naming the gap.

    Derived from ``AgentKind`` rather than from a hand-written list, so a sixth
    built-in harness fails here instead of shipping unmeasured. A harness whose
    reducer genuinely cannot be driven without a live process belongs in an
    exemption set carrying its reason — not in a weaker end-to-end assertion.

    From the ENUM and not from ``AgentRegistry``, which is open: a third-party
    plugin agent registers there too, and an out-of-tree harness is not this
    repo's to cover (``coder_eval_uipath``'s delegate-sdk is the live example).
    """
    covered = {name for name in globals() if name.startswith("test_") and name.endswith("_buckets_tile_the_turn")}
    # NONE is the agentless task double — no reducer, no generation window at
    # all; UNKNOWN is a load-failure placeholder that never runs.
    built_in = set(AgentKind) - {AgentKind.NONE, AgentKind.UNKNOWN}
    missing = {kind for kind in built_in if f"test_{kind.value.replace('-', '_')}_buckets_tile_the_turn" not in covered}
    assert not missing, f"no ms-exact identity case for {sorted(m.value for m in missing)}"


def test_the_sensor_sees_a_window_that_stops_tiling():
    """The gating mutation check, as a committed test rather than an attestation.

    A window seeded from its own turn start instead of from the previous
    flush's close is the defect pi shipped with, and the whole point of this
    module is that the SUITE notices it rather than a reviewer reproducing it
    by hand. The golden corpus cannot: it masks every value involved.

    Asserted on the MAGNITUDE as well as on the failure, because "it raised"
    would also pass if the mutation broke the case in some unrelated way. The
    600 ms is the scripted gap between one ``turn_end`` and the next
    ``turn_start`` — real model time, which untiling books to nothing.
    """
    healthy = _pi_turn()
    mutated = _pi_turn(untile=True)

    def _generation_ms(turn: Turn) -> float:
        return sum(m.generation_duration_ms or 0.0 for m in turn.messages if isinstance(m, AssistantMessage))

    assert _generation_ms(healthy) - _generation_ms(mutated) == pytest.approx(600.0)
    with pytest.raises(AssertionError, match="booked nowhere"):
        assert_identity_closes(mutated)
