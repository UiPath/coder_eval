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

Every harness is driven through ``coder_eval.testing.replay``: its decoder runs on a
real ``TurnEmitter`` whose ``ScriptedClock`` moves on each ``Tick`` (pi, antigravity,
claude-code), or on scripted CLI epoch stamps under ``cli_epoch_ms`` (opencode, and
codex, whose stamps are the SDK's epoch milliseconds).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from coder_eval.models import (
    AgentKind,
    AssistantMessage,
    parse_agent_config,
)
from coder_eval.streaming.events import (
    AgentEndStatus,
)
from coder_eval.testing import Replay, ScriptedClock, Tick, assert_identity_closes, replay


# The two CLI harnesses (opencode, codex) report their stamps as epoch
# milliseconds and convert them with the real ``datetime.fromtimestamp``. So
# the shared origin is DERIVED from an epoch value rather than written as a
# wall time: that is what puts a scripted ``now()`` read and a converted CLI
# stamp on ONE timeline, without patching the conversion itself.
EPOCH_MS = 1_800_000_000_000
BASE = datetime.fromtimestamp(EPOCH_MS / 1000.0)


def at(ms: float) -> datetime:
    return BASE + timedelta(milliseconds=ms)


# --------------------------------------------------------------------------
# pi — a scripted clock
# --------------------------------------------------------------------------


def _pi_replay(*, untile: bool = False) -> Replay:
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
    from coder_eval.agents.pi_agent import _PiDecoder

    payload = {"type": "turn_end", "message": {"role": "assistant", "usage": {"input": 10, "output": 5}}}

    class _Untiling(_PiDecoder):
        def on_turn_start(self) -> None:
            super().on_turn_start()
            if untile:
                self.gen_mark = None

    stream = [
        Tick(500),  # CLI boot: head
        {"type": "turn_start"},
        Tick(700),
        {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash", "args": {}},
        Tick(1200),
        {"type": "tool_execution_end", "toolCallId": "c1", "result": "ok"},
        Tick(2000),
        payload,
        Tick(2600),  # the inter-turn gap, which window 2 tiles back over
        {"type": "turn_start"},
        Tick(3000),
        payload,
        Tick(3500),  # process teardown after the last turn: tail
    ]
    return replay(stream, _Untiling, clock=ScriptedClock(BASE), end=lambda d: d.end(AgentEndStatus.COMPLETED))


# --------------------------------------------------------------------------
# opencode — a datetime subclass on the module
# --------------------------------------------------------------------------


def _opencode_replay() -> Replay:
    """The same two-window shape, driven through OpenCode's step stream on the CLI's own stamps.

    Under ``cli_epoch_ms`` the windows are bounded by each event's envelope
    ``timestamp`` and the tool by ``state.time``; only the bracket reads the clock.
    """
    from coder_eval.agents.opencode_agent import _OpenCodeDecoder
    from coder_eval.models import TimingBasis

    def event(event_type: str, at_ms: int, **part: Any) -> dict[str, Any]:
        return {"type": event_type, "timestamp": EPOCH_MS + at_ms, "part": part}

    finish = {"reason": "stop", "tokens": {"input": 10, "output": 5}}
    stream = [
        Tick(500),
        event("step_start", 500, messageID="m1"),  # Node boot before it: head
        Tick(1200),
        event(
            "tool_use",
            1200,
            callID="c1",
            tool="bash",
            state={"status": "completed", "time": {"start": EPOCH_MS + 700, "end": EPOCH_MS + 1200}},
        ),
        Tick(2000),
        event("step_finish", 2000, **finish),
        Tick(2600),
        event("step_start", 2600, messageID="m2"),
        Tick(3000),
        event("step_finish", 3000, **finish),
        Tick(3500),
    ]
    return replay(
        stream,
        _OpenCodeDecoder,
        clock=ScriptedClock(BASE),
        basis=TimingBasis.CLI_EPOCH_MS,
        end=lambda d: d.end(AgentEndStatus.COMPLETED),
    )


# --------------------------------------------------------------------------
# antigravity — an injected TurnClock, at the reducer
# --------------------------------------------------------------------------


def _antigravity_replay() -> Replay:
    """One interleaved window per generation, with a tool inside the first.

    Driven at the decoder: the fake conversation yields with no delay, so an
    end-to-end run cannot distinguish a window that opened at the turn's start
    from one that opened later. The first MODEL Step seeds the first window.
    """
    from coder_eval.agents.antigravity_agent import _AntigravityDecoder
    from tests._fixtures.golden_streams.antigravity_fixtures import _step, _tc, _usage

    stream = [
        Tick(700),  # dispatch before the first Step: head
        _step(
            "TOOL_CALL",
            "ACTIVE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", "c1", {"command_line": "ls"})],
        ),
        Tick(1200),
        _step(
            "TOOL_CALL",
            "DONE",
            target="TARGET_ENVIRONMENT",
            tool_calls=[_tc("run_command", "c1", {"command_line": "ls", "exit_code": 0})],
        ),
        Tick(2000),
        _step("THINKING", "DONE", thinking="plan", usage=_usage(100, 0, 5, 5)),
        Tick(3000),
        _step("TEXT_RESPONSE", "DONE", content="done", complete=True, usage=_usage(200, 0, 10, 0)),
        Tick(3500),
    ]
    return replay(
        stream,
        _AntigravityDecoder,
        clock=ScriptedClock(BASE),
        model="gemini-3.5-flash",
        end=lambda d: d.end(AgentEndStatus.COMPLETED),
    )


# --------------------------------------------------------------------------
# codex — scripted SDK epoch-millisecond stamps
# --------------------------------------------------------------------------


def _codex_replay() -> Replay:
    """Two tiled windows, the first SPLIT across two sub-messages.

    Codex is the only harness that cuts one window into several messages
    (thinking and action, apportioned by output-token share), and they share
    one pair of bounds. The identity has to close over the GROUP, so this case
    drives that split deliberately rather than the simpler one-message shape.

    Under ``cli_epoch_ms`` every window and tool bound is the SDK's own epoch
    millisecond stamp on the item notification; only the bracket reads the clock.
    """
    from coder_eval.agents.codex_agent import CodexAgent, _CodexDecoder
    from coder_eval.models import TimingBasis

    def item(method: str, root: SimpleNamespace, **stamps: int) -> SimpleNamespace:
        payload = {f"{key}_at_ms": EPOCH_MS + at_ms for key, at_ms in stamps.items()}
        return SimpleNamespace(method=method, payload=SimpleNamespace(item=SimpleNamespace(root=root), **payload))

    def usage(output: int, reasoning: int) -> SimpleNamespace:
        last = SimpleNamespace(
            input_tokens=500, cached_input_tokens=0, output_tokens=output, reasoning_output_tokens=reasoning
        )
        return SimpleNamespace(
            method="thread/tokenUsage/updated", payload=SimpleNamespace(token_usage=SimpleNamespace(last=last))
        )

    reasoning = SimpleNamespace(type="reasoning", id="r1", content=["plan"], summary=[])
    command = SimpleNamespace(
        type="commandExecution", id="c1", command="ls", exit_code=0, aggregated_output="", duration_ms=None
    )
    stream = [
        # Window 1 — no mark yet, so it opens at its own first item (+500): the CLI
        # boot before that is head. Thinking + action, so the flush cuts two
        # sub-messages sharing the window, which holds the command.
        Tick(500),
        item("item/started", reasoning, started=500),
        Tick(700),
        item("item/started", command, started=700),
        Tick(1200),
        item("item/completed", command, completed=1200),
        item("item/completed", reasoning, completed=1500),
        Tick(2000),
        item("item/completed", SimpleNamespace(type="agentMessage", id="m1", text="answer"), completed=2000),
        usage(output=100, reasoning=80),
        # Window 2 — tiles back from the mark (+2000), covering the gap before its
        # own first item at +2600.
        Tick(2600),
        item("item/started", SimpleNamespace(type="agentMessage", id="m2", text="more"), started=2600),
        Tick(3000),
        item("item/completed", SimpleNamespace(type="agentMessage", id="m2", text="more"), completed=3000),
        usage(output=5, reasoning=0),
        Tick(3500),  # teardown after the last generation: tail
    ]
    agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="gpt-5.5"))
    return replay(
        stream,
        lambda emitter: _CodexDecoder(agent, emitter, turn_id="turn"),
        clock=ScriptedClock(BASE),
        basis=TimingBasis.CLI_EPOCH_MS,
        model="gpt-5.5",
        end=lambda decoder: decoder.end(AgentEndStatus.COMPLETED),
    )


# --------------------------------------------------------------------------
# claude-code — a scripted clock
# --------------------------------------------------------------------------


def _claude_replay(stream: list[Any]) -> Replay:
    from coder_eval.agents.claude_code_agent import ClaudeCodeAgent, _ClaudeDecoder

    agent = ClaudeCodeAgent(parse_agent_config(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits"))
    return replay(
        stream,
        lambda emitter: _ClaudeDecoder(agent, emitter, effective_model=None),
        clock=ScriptedClock(BASE),
        end=lambda decoder: decoder.end(AgentEndStatus.COMPLETED),
    )


def _claude_turn() -> Replay:
    """A tool call between two emissions, with a real head and a real tail.

    The first `message_start` re-seeds the window, so the CLI spawn and the
    query build before it are head rather than msg0's generation. That a LATER
    one must not re-seed is asserted directly in `tests/test_agent_telemetry.py`;
    here it shows up as the windows still tiling.

    The result is scripted at the instant the tool ends, so the interval between
    the emission that ISSUED the call and the result is the tool's execution
    exactly. `_claude_slow_result_turn` is the case that discriminates a window
    that stops tiling across the result; this one keeps the coincident shape so
    the two read as a pair.
    """
    from tests._fixtures.golden_streams.claude_fixtures import AssistantMessage as SdkAssistantMessage
    from tests._fixtures.golden_streams.claude_fixtures import ToolUseBlock, UserMessage, message_start

    return _claude_replay(
        [
            # The stream puts `message_start` before the emission it announces, and
            # the FIRST one is what re-seeds the window.
            Tick(800),
            message_start("m1"),
            Tick(1000),
            SdkAssistantMessage(
                [ToolUseBlock("c1", "Bash", {"command": "ls"})],
                usage={"input_tokens": 10, "output_tokens": 5},
                message_id="m1",
            ),
            Tick(1800),  # the tool ran for the whole gap
            UserMessage("c1", False, "ok"),
            Tick(2000),
            message_start("m2"),  # does NOT re-seed: once per turn
            Tick(2500),
            SdkAssistantMessage([], usage={"input_tokens": 10, "output_tokens": 5}, message_id="m2"),
            Tick(3000),
        ]
    )


def _claude_slow_result_turn() -> Replay:
    """A FAST tool followed by a SLOW result round trip — the shape that hid a defect.

    The tool runs for 20 ms and its result round trip takes 2000 ms, which is
    `tasks/dataset_example.yaml`, where a 21.5 ms ``Write`` met a 2511.7 ms round
    trip and 21% of the turn was accounted to nothing. The window after the
    result must tile from the previous emission, not open when the result lands.
    """
    from tests._fixtures.golden_streams.claude_fixtures import AssistantMessage as SdkAssistantMessage
    from tests._fixtures.golden_streams.claude_fixtures import ToolUseBlock, UserMessage, message_start

    return _claude_replay(
        [
            Tick(500),
            message_start("m1"),
            Tick(980),
            SdkAssistantMessage(
                [ToolUseBlock("c1", "Write", {"file_path": "out.txt"})],
                usage={"input_tokens": 10, "output_tokens": 5},
                message_id="m1",
            ),
            Tick(1000),
            UserMessage("c1", False, "written"),
            # The live stream delivers a SECOND user message ~2 s later with NO
            # tool-result block; a mark reset on it would drop those 2 s.
            Tick(3000),
            SimpleNamespace(content=[], tool_use_result=None),
            message_start("m2"),
            Tick(3400),
            SdkAssistantMessage([], usage={"input_tokens": 10, "output_tokens": 5}, message_id="m2"),
            Tick(3800),
        ]
    )


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


def test_pi_buckets_tile_the_turn():
    result = _pi_replay()
    assert_identity_closes(result.record, started_at=result.started_at, ended_at=result.ended_at)


def test_opencode_buckets_tile_the_turn():
    result = _opencode_replay()
    assert_identity_closes(result.record, started_at=result.started_at, ended_at=result.ended_at)


def test_antigravity_buckets_tile_the_turn():
    result = _antigravity_replay()
    assert_identity_closes(result.record, started_at=result.started_at, ended_at=result.ended_at)


def test_codex_buckets_tile_the_turn():
    result = _codex_replay()
    generations = [m for m in result.record.messages if isinstance(m, AssistantMessage)]
    assert len(generations) == 3, "window 1 splits into a thinking and an action part; window 2 is one"
    assert generations[0].started_at == generations[1].started_at == at(500)
    assert generations[2].started_at == generations[1].completed_at == at(2000)
    assert [c.duration_ms for c in result.record.commands] == [500.0]
    assert_identity_closes(result.record, started_at=result.started_at, ended_at=result.ended_at)


def test_claude_code_buckets_tile_the_turn():
    result = _claude_turn()
    assert [c.duration_ms for c in result.record.commands] == [800.0]
    assert_identity_closes(result.record, started_at=result.started_at, ended_at=result.ended_at)


def test_a_slow_tool_result_round_trip_is_not_lost():
    """The discriminating case: a fast tool whose result takes 2 s to come back.

    Re-adding `last_event_wall = self.emitter.now()` to `on_user_message` fails
    THIS and leaves every other case in the file green, which is exactly what
    happened in production.
    """
    result = _claude_slow_result_turn()
    assert [c.duration_ms for c in result.record.commands] == [20.0]
    assert_identity_closes(result.record, started_at=result.started_at, ended_at=result.ended_at)


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
    healthy = _pi_replay()
    mutated = _pi_replay(untile=True)

    def _generation_ms(result: Replay) -> float:
        return sum(m.generation_duration_ms or 0.0 for m in result.record.messages if isinstance(m, AssistantMessage))

    assert _generation_ms(healthy) - _generation_ms(mutated) == pytest.approx(600.0)
    with pytest.raises(AssertionError, match="booked nowhere"):
        assert_identity_closes(mutated.record, started_at=mutated.started_at, ended_at=mutated.ended_at)
