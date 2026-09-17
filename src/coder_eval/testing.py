"""The test harness a harness adapter shares with the in-tree suites: replay, identity, balance, conformance.

No ``pytest`` import: every check raises ``AssertionError``, and callers parametrize.
A plugin calls these from its own tests exactly as ``tests/`` does.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from coder_eval.models import AssistantMessage, Enforcement, HarnessContract, PermissionMode, TimingBasis, TurnRecord
from coder_eval.streaming.emitter import TurnEmitter, TurnOutcome
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
from coder_eval.timing import main_thread_tool_spans, union_ms


if TYPE_CHECKING:
    from coder_eval.models import TaskDefinition


@dataclass(frozen=True)
class Tick:
    """A stream element that moves the replay's ``ScriptedClock`` to ``at_ms`` after its origin."""

    at_ms: float


class ScriptedClock:
    """A clock that reads ``origin + at_ms`` and moves only on a ``Tick``."""

    def __init__(self, origin: datetime) -> None:
        self._origin = origin
        self._at_ms = 0.0

    def now(self) -> datetime:
        return self._origin + timedelta(milliseconds=self._at_ms)

    def _move_to(self, at_ms: float) -> None:
        self._at_ms = at_ms


@dataclass(frozen=True)
class Replay:
    """What a replayed turn produced; ``started_at`` / ``ended_at`` are its bracket stamps."""

    record: TurnRecord
    events: list[StreamEvent]
    outcome: TurnOutcome
    started_at: datetime
    ended_at: datetime


class _Recorder:
    def __init__(self) -> None:
        self.events: list[StreamEvent] = []

    def on_event(self, event: StreamEvent) -> None:
        self.events.append(event)


def replay[D: Callable[[Any], None]](
    stream: Iterable[Any],
    make_decoder: Callable[[TurnEmitter], D],
    *,
    clock: ScriptedClock,
    basis: TimingBasis = TimingBasis.TURN_CLOCK,
    model: str | None = "m",
    end: Callable[[D], TurnOutcome] | None = None,
) -> Replay:
    """Drive a decoder over ``stream`` through a real ``TurnEmitter`` on ``clock``.

    A ``Tick`` moves the clock; every other element is passed to the decoder. The turn
    ends with ``end(decoder)`` when given, else ``emitter.finalize(COMPLETED)``. An
    exception from the decoder propagates.
    """
    recorder = _Recorder()
    emitter = TurnEmitter(
        task_id="replay", iteration=1, prompt="go", model=model, basis=basis, clock=clock, sinks=[recorder]
    )
    emitter.begin()
    decoder = make_decoder(emitter)
    for element in stream:
        if isinstance(element, Tick):
            clock._move_to(element.at_ms)  # pyright: ignore[reportPrivateUsage]
        else:
            decoder(element)
    outcome = end(decoder) if end is not None else emitter.finalize(AgentEndStatus.COMPLETED)
    starts = [e for e in recorder.events if isinstance(e, AgentStartEvent)]
    ends = [e for e in recorder.events if isinstance(e, AgentEndEvent)]
    return Replay(
        record=outcome.record,
        events=recorder.events,
        outcome=outcome,
        started_at=starts[0].timestamp,
        ended_at=ends[-1].timestamp,
    )


def assert_identity_closes(record: TurnRecord, *, started_at: datetime, ended_at: datetime) -> None:
    """Assert head + Σ generation + UNION(tool) + tail equals the turn's span, to float precision.

    Main thread only on both sides, through production's own span selector. Also
    asserts the stored ``tool_union_ms`` equals the union computed here.

    Raises:
        AssertionError: a bucket is missing, the stored union disagrees, or the buckets
            do not tile the span.
    """
    span_ms = (ended_at - started_at).total_seconds() * 1000.0
    generation_ms = sum(
        m.generation_duration_ms
        for m in record.messages
        if isinstance(m, AssistantMessage) and m.parent_tool_use_id is None and m.generation_duration_ms is not None
    )
    tool_ms = union_ms(main_thread_tool_spans(record.messages, record.commands))
    head, tail = record.harness_startup_ms, record.harness_teardown_ms
    if head is None or tail is None:
        raise AssertionError(f"a turn that generated has a measured head and tail (head={head}, tail={tail})")
    stored = record.tool_union_ms
    if (stored is None and tool_ms > 0) or (stored is not None and not math.isclose(stored, tool_ms, abs_tol=1e-6)):
        raise AssertionError(
            f"TurnRecord.tool_union_ms is {record.tool_union_ms}, but the main-thread command spans union to "
            + f"{tool_ms:.4f} ms: the stored value and the selection rule have come apart"
        )
    bucket_sum = head + generation_ms + tool_ms + tail
    if not math.isclose(bucket_sum, span_ms, abs_tol=1e-6):
        raise AssertionError(
            f"the four buckets sum to {bucket_sum:.4f} ms against a {span_ms:.4f} ms turn "
            + f"(off by {bucket_sum - span_ms:+.4f} ms): head={head:.4f}, generation={generation_ms:.4f}, "
            + f"tool_union={tool_ms:.4f}, tail={tail:.4f}. A sum UNDER the turn means some interval is "
            + "booked nowhere; a sum OVER it means one is booked twice."
        )


_BUCKETS = ("uncached_input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def assert_stream_balanced(events: Sequence[StreamEvent]) -> None:
    """Assert one turn's event stream is well formed.

    Exactly one ``AgentStartEvent``, first, and one ``AgentEndEvent``, last; every
    ``TurnStartEvent`` closed by a ``TurnEndEvent`` of the same ``turn_id`` before the
    next start and before the end, and no end without its start; every tool id started
    once is ended exactly once, and none ends unstarted; per token bucket, the sum of
    ``TurnEndEvent.tokens`` is at most ``AgentEndEvent.usage``.

    Raises:
        AssertionError: naming every violation found.
    """
    problems: list[str] = []
    if not events:
        raise AssertionError("the stream is empty")
    starts = [e for e in events if isinstance(e, AgentStartEvent)]
    ends = [e for e in events if isinstance(e, AgentEndEvent)]
    if len(starts) != 1 or not isinstance(events[0], AgentStartEvent):
        problems.append(f"{len(starts)} AgentStartEvent(s), first event is {type(events[0]).__name__}")
    if len(ends) != 1 or not isinstance(events[-1], AgentEndEvent):
        problems.append(f"{len(ends)} AgentEndEvent(s), last event is {type(events[-1]).__name__}")
    open_turn: str | None = None
    for event in events:
        if isinstance(event, TurnStartEvent):
            if open_turn is not None:
                problems.append(f"turn {event.turn_id!r} started while {open_turn!r} is open")
            open_turn = event.turn_id
        elif isinstance(event, TurnEndEvent):
            if open_turn != event.turn_id:
                problems.append(f"turn {event.turn_id!r} ended while the open turn is {open_turn!r}")
            open_turn = None
        elif isinstance(event, AgentEndEvent) and open_turn is not None:
            problems.append(f"turn {open_turn!r} is still open at the AgentEndEvent")
            open_turn = None
    tool_starts = Counter(e.tool.tool_id for e in events if isinstance(e, ToolStartEvent))
    tool_ends = Counter(e.tool.tool_id for e in events if isinstance(e, ToolEndEvent))
    problems += [f"tool {tid!r} started {n} times" for tid, n in tool_starts.items() if n != 1]
    problems += [f"tool {tid!r} ended {tool_ends[tid]} times" for tid in tool_starts if tool_ends[tid] != 1]
    problems += [f"tool {tid!r} ended without a start" for tid in tool_ends if tid not in tool_starts]
    if ends:
        usage = ends[-1].usage
        for bucket in _BUCKETS:
            reported = sum(getattr(e.tokens, bucket) for e in events if isinstance(e, TurnEndEvent) and e.tokens)
            if reported > getattr(usage, bucket):
                problems.append(f"TurnEndEvent {bucket} sum {reported} > AgentEndEvent.usage {getattr(usage, bucket)}")
    if problems:
        raise AssertionError("unbalanced event stream: " + "; ".join(problems))


_FIELDS = ("system_prompt", "plugin_skills", "permission_mode", "allowed_tools", "disallowed_tools")
_CONFIG_FIELD = {"plugin_skills": "plugins"}
_GATED_VALUES: dict[str, Any] = {
    "system_prompt": "CONFORMANCE-MARKER-7f3a",
    "plugins": [{"type": "local", "path": "/plugins/p"}],
    "permission_mode": "plan",
    "allowed_tools": ["Bash"],
    "disallowed_tools": ["Bash"],
}


def enforced_cells(contract: HarnessContract, kind: str) -> set[tuple[str, str]]:
    """``(kind, cell)`` for every ENFORCED field; ``permission_mode`` gives one ``permission_mode=<value>`` per mode."""
    cells: set[tuple[str, str]] = set()
    for field in _FIELDS:
        if getattr(contract, field) is not Enforcement.ENFORCED:
            continue
        if field == "permission_mode":
            cells |= {(kind, f"permission_mode={mode.value}") for mode in contract.permission_modes or ()}
        else:
            cells.add((kind, field))
    return cells


def _task(kind: str, **agent: Any) -> TaskDefinition:
    from coder_eval.models import AgentKind, FileExistsCriterion, SandboxConfig, TaskDefinition, parse_agent_config

    return TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt=None if kind == AgentKind.NONE.value else "do the task",
        agent=parse_agent_config(type=kind, **agent),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(description="c", path="out.txt")],
    )


def _expect_rejected(task: Callable[[], TaskDefinition], match: str) -> None:
    import re

    from coder_eval.orchestration.harness_contract import HarnessContractError, validate_harness_contract

    try:
        validate_harness_contract(task())
    except HarnessContractError as error:
        if not re.search(match, str(error)):
            raise AssertionError(f"rejected, but {str(error)!r} does not match {match!r}") from error
        return
    raise AssertionError(f"expected a HarnessContractError matching {match!r}; the task resolved")


def rejections(kind: str) -> list[tuple[str, Callable[[], None]]]:
    """The resolution-time rejections ``kind``'s contract implies, as named checks that raise ``AssertionError``."""
    from coder_eval.agents.registry import AgentRegistry
    from coder_eval.plugins import ensure_plugins_loaded

    ensure_plugins_loaded()
    registration = AgentRegistry.get(kind)
    if registration is None:
        raise AssertionError(f"agent kind {kind!r} is not registered")
    contract = registration.agent_class.contract
    checks: list[tuple[str, Callable[[], None]]] = []
    for field in _FIELDS:
        if getattr(contract, field) is Enforcement.UNSUPPORTED:
            config_field = _CONFIG_FIELD.get(field, field)
            value = _GATED_VALUES[config_field]
            checks.append(
                (
                    f"unsupported {config_field}",
                    lambda c=config_field, v=value: _expect_rejected(
                        lambda: _task(kind, **{c: v}), rf"agent\.{c}.*{kind!r}"
                    ),
                )
            )
    if contract.permission_mode is Enforcement.ENFORCED:
        for mode in PermissionMode:
            if mode not in (contract.permission_modes or frozenset()):
                checks.append(
                    (
                        f"undeclared permission_mode={mode.value}",
                        lambda m=mode: _expect_rejected(
                            lambda: _task(kind, permission_mode=m), "has no documented meaning"
                        ),
                    )
                )
    if Enforcement.ENFORCED in (contract.allowed_tools, contract.disallowed_tools):
        checks.append(
            (
                "misspelled tool name",
                lambda: _expect_rejected(lambda: _task(kind, allowed_tools=["Bassh"]), "did you mean 'Bash'"),
            )
        )
    return checks


async def conformance(kind: str, probes: Mapping[tuple[str, str], Callable[[], Awaitable[None]]]) -> None:
    """Assert ``kind`` rejects what its contract marks unsupported and honors every enforced cell.

    Runs every check from ``rejections(kind)``, asserts ``probes`` covers exactly
    ``enforced_cells(contract, kind)``, then awaits every probe.

    Raises:
        AssertionError: a rejection is missing, a probe is missing or extra, or a probe fails.
    """
    from coder_eval.agents.registry import AgentRegistry

    for _name, check in rejections(kind):
        check()
    registration = AgentRegistry.get(kind)
    if registration is None:
        raise AssertionError(f"no agent is registered for {kind!r}")
    expected = enforced_cells(registration.agent_class.contract, kind)
    if set(probes) != expected:
        raise AssertionError(
            f"probes for {kind!r} do not match its enforced cells: missing {sorted(expected - set(probes))}, "
            + f"extra {sorted(set(probes) - expected)}"
        )
    for cell in sorted(probes):
        await probes[cell]()
