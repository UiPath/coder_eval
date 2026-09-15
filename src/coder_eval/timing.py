"""Wall-clock arithmetic for a turn, defined once and shared.

A cycle-free leaf: it sits outside ``agents/`` because ``EventCollector``
consumes it, and importing anything under ``agents/`` pulls in every agent,
which imports ``streaming/``.

NO harness subtracts tool execution from its own generation windows. Each
publishes the RAW window it measured, and ``subtract_tool_time`` takes the
UNION of the tool intervals back out of them once, for all five, at the single
capture seam. A reducer's only remaining timing decision is where its window
opens.

There is a TypeScript twin, ``evalboard/lib/timing.ts::busyMs``, answering the
same question about the same ``task.json``, so the two must agree — and neither
owns the numbers: ``tests/_fixtures/timing_union_cases.json`` does, and both
suites replay it.

Rationale: .claude/notes/timing.md § Where a reducer's window opens
"""

import math
import time
from collections.abc import Iterable
from datetime import datetime, timedelta

from coder_eval.models import AssistantMessage, CommandTelemetry, TranscriptMessage


class TurnClock:
    """One (wall, monotonic) pair per turn; every later stamp derives from it.

    A turn's bounds and its durations must share a basis, or they disagree in a
    field measured in milliseconds. Stamps stay NAIVE LOCAL, matching the rest of
    the telemetry and the persisted ``execution_started_at``.

    ONE PER TURN, never module-level and never reused across turns: a long run
    accumulates drift between the pair and real wall time. Within a turn the
    derived stamp is monotonic-accurate and may drift from real wall time; each
    turn re-anchors. That is intended — do not "fix" it by re-reading the wall
    clock, which is the property being removed.

    NOT for deadlines. Those stay on ``time.monotonic()`` directly: a deadline
    must not move when the wall clock steps.

    Which harnesses use it is the `clock basis for recorded stamps` row in
    docs/agents/HARNESS_PARITY.md, the SSOT for per-harness composition.

    Rationale: .claude/notes/timing.md § TurnClock
    """

    def __init__(self) -> None:
        self._wall0 = datetime.now()
        self._mono0 = time.monotonic()

    def now(self) -> datetime:
        return self._wall0 + timedelta(seconds=time.monotonic() - self._mono0)


def _require_same_awareness(a: datetime, b: datetime, *, field: str) -> None:
    """Raise if one stamp is timezone-aware and the other is naive.

    Only the MIX raises: an agent consistently aware, or consistently naive, is
    not this function's problem. It turns a bare ``TypeError`` raised deep in the
    arithmetic into a statement of which pair disagreed and which side is aware.

    Rationale: .claude/notes/timing.md § _require_same_awareness
    """
    if (a.tzinfo is None) == (b.tzinfo is None):
        return
    aware, naive = ("first", "second") if a.tzinfo is not None else ("second", "first")
    raise TypeError(
        f"{field}: one stamp is timezone-aware and the other is naive (the {aware} is aware, "
        + f"the {naive} is not), so the interval between them cannot be measured. Every stamp "
        + "this harness records is a naive local `datetime.now()`; if you are writing an agent "
        + "outside this repo (the `coder_eval.plugins` SPI), make its stamps naive local too "
        + "rather than normalizing here, so its tool spans and its window bounds keep one basis."
    )


def busy_ms(spans: list[tuple[datetime, datetime]], lo: datetime, hi: datetime) -> float:
    """Wall milliseconds inside ``[lo, hi]`` where at least ONE span was running.

    The UNION, not the sum: overlapping tool intervals would otherwise over-count
    the busy time by exactly the overlap and drive a generation window negative.
    Spans are clipped to ``[lo, hi]``, so a tool that opened earlier contributes
    only the part that ran inside this window.

    Raises ``TypeError`` (via ``_require_same_awareness``) if the bounds and the
    spans do not share a timezone awareness. An EMPTY span list returns ``0.0``
    and is not checked at all.

    Rationale: .claude/notes/timing.md § busy_ms
    """
    if not spans:
        return 0.0
    _require_same_awareness(lo, hi, field="busy_ms window")
    for span_start, span_end in spans:
        _require_same_awareness(lo, span_start, field="busy_ms window vs a tool span's start")
        _require_same_awareness(hi, span_end, field="busy_ms window vs a tool span's end")
    clipped = sorted((max(s, lo), min(e, hi)) for s, e in spans if min(e, hi) > max(s, lo))
    if not clipped:
        return 0.0
    total = 0.0
    open_start, open_end = clipped[0]
    for start, end in clipped[1:]:
        if start > open_end:  # disjoint — bank the run and start a new one
            total += (open_end - open_start).total_seconds() * 1000.0
            open_start, open_end = start, end
        else:  # overlapping or adjacent — extend the run
            open_end = max(open_end, end)
    return total + (open_end - open_start).total_seconds() * 1000.0


def union_ms(spans: list[tuple[datetime, datetime]]) -> float:
    """Wall milliseconds at least ONE span was running, over their full extent.

    ``busy_ms`` with the window set to the spans' own bounds.

    It does NOT filter ``end < start``. EVERY caller drops those while building
    its span list, so a new caller that skips the check gets ``busy_ms``'s
    silent discard rather than this function's stated contract.

    Rationale: .claude/notes/timing.md § union_ms
    """
    if not spans:
        return 0.0
    return busy_ms(spans, min(s for s, _ in spans), max(e for _, e in spans))


def close_window(*, mark: datetime, now: datetime, item_start: datetime | None = None) -> tuple[datetime, float]:
    """Open one generation window at ``mark`` and close it at ``now``: its ``(started, span_ms)``.

    The shape all five reducers share, and what it returns is the RAW window —
    tool execution comes back out centrally, in ``subtract_tool_time``.

    ``mark`` is where the window opens: the previous flush's close, which is what
    makes the windows TILE the turn contiguously. It is keyword-only with NO
    default so that no reducer can open a window without stating what it tiles
    from.

    ``item_start`` is this emission's own first stamp, when the harness has one;
    the ``min()`` against ``mark`` stops a backwards stamp inverting the span.

    The span is clamped at ``0.0``: an inverted window is a measured zero, not a
    negative generation. ``completed`` is deliberately not returned — it is
    always ``now``, which the caller already has.

    Rationale: .claude/notes/timing.md § close_window
    """
    started = min(mark, item_start) if item_start is not None else mark
    return started, max(0.0, (now - started).total_seconds() * 1000.0)


def decompose_turn(
    first_started_at: datetime | None,
    last_completed_at: datetime | None,
    agent_started_at: datetime | None,
    agent_ended_at: datetime | None,
    tool_spans: list[tuple[datetime, datetime]] | None = None,
) -> tuple[float | None, float | None]:
    """Wall ms before the first generation window opens, and after the last closes.

    The turn's two unexplained ends. Between them the windows tile and tool
    execution is already subtracted, so head + generation + UNION(tool) + tail
    is the whole turn.

    ``tool_spans`` keeps the four buckets DISJOINT: a tool is not confined to a
    generation window, so a span left in the head or tail is booked twice.

    ``EventCollector`` is the SOLE caller: computed once, persisted on
    ``TurnRecord``, READ by everyone later.

    The head means ONE thing on all five harnesses: wall clock from the turn
    start until the harness first observed model output. What it CONTAINS
    differs per harness (docs/agents/HARNESS_PARITY.md).

    ``None`` means never measured — never ``0.0`` (CE058). A measured inversion
    IS a real zero and clamps, because both ends were observed.

    Raises ``TypeError`` (via ``_require_same_awareness``) on a mixed pair.

    Rationale: .claude/notes/timing.md § decompose_turn
    """
    spans = tool_spans or []
    head = tail = None
    if first_started_at is not None and agent_started_at is not None:
        _require_same_awareness(agent_started_at, first_started_at, field="harness_startup_ms")
        elapsed = (first_started_at - agent_started_at).total_seconds() * 1000.0
        head = max(elapsed - busy_ms(spans, agent_started_at, first_started_at), 0.0)
    if last_completed_at is not None and agent_ended_at is not None:
        _require_same_awareness(last_completed_at, agent_ended_at, field="harness_teardown_ms")
        elapsed = (agent_ended_at - last_completed_at).total_seconds() * 1000.0
        tail = max(elapsed - busy_ms(spans, last_completed_at, agent_ended_at), 0.0)
    return head, tail


def main_thread_tool_spans(
    messages: Iterable[TranscriptMessage], commands: Iterable[CommandTelemetry]
) -> list[tuple[datetime, datetime]]:
    """Bounded execution intervals of the MAIN THREAD's tool calls.

    The span set the generation subtraction, the head and the tail are all
    measured against, so they cannot disagree about which calls exist. Shared
    with ``result_metrics.turn_time_buckets``;
    ``tests/test_timing_close_window.py::TestTheThreeToolUnionsAgree`` pins this,
    that, and ``scripts/timing/decompose_run.py`` together.

    Sub-agent tools are excluded — a child's calls are already covered by the
    parent Agent call's own interval. A sub-agent's tool ids are reachable only
    through the messages that own them: a child generation carries
    ``parent_tool_use_id``, and its ``tool_use_ids`` are the calls it made.

    An inverted pair (``end`` before ``start``) is dropped here, which is what
    keeps ``union_ms``'s "every caller filters" contract true.

    Rationale: .claude/notes/timing.md § main_thread_tool_spans
    """
    sub_agent_tool_ids = {
        tool_id
        for m in messages
        if isinstance(m, AssistantMessage) and m.parent_tool_use_id is not None
        for tool_id in m.tool_use_ids
    }
    return [
        (c.execution_started_at, c.execution_completed_at)
        for c in commands
        if c.execution_started_at is not None
        and c.execution_completed_at is not None
        and c.execution_completed_at >= c.execution_started_at
        and c.tool_id not in sub_agent_tool_ids
    ]


#: How far a published window may sit from the span its own bounds describe.
#: ONE MILLISECOND — the coarsest unit a field named ``_ms`` can honestly be
#: published in, so a producer that rounds is admitted while the defect class
#: (a reducer narrowing or widening a window without moving its bounds) is tens
#: to thousands of ms, orders of magnitude above it.
#: Rationale: .claude/notes/timing.md § _WINDOW_TOLERANCE_MS
_WINDOW_TOLERANCE_MS = 1.0


def subtract_tool_time(
    messages: list[TranscriptMessage],
    spans: list[tuple[datetime, datetime]],
) -> list[TranscriptMessage]:
    """Take tool execution back out of the generation windows it overlapped.

    THE one place this happens. Reducers publish RAW windows and never subtract.

    Returns a NEW list; messages are copied, never mutated — agents alias their
    own live message objects into ``AgentEndEvent``, so writing in place would
    reach back into agent state.

    Windows group by identical ``(started_at, completed_at)``, so the two
    sub-messages Codex splits one window into share a single subtraction.
    Sub-agent generations (``parent_tool_use_id`` set) are skipped. A
    ``generation_duration_ms`` of ``None`` passes through untouched — never
    coerced to ``0.0`` (CE058) — as does every non-``AssistantMessage`` entry.
    A window fully covered by tool execution reaches ``0.0``: a measurement.

    Raises ``ValueError`` if a group's published total does not equal the span
    its bounds describe. That equality is what lets ``generation_duration_ms``
    stay a PUBLISHED field; CE061 forces the same shape statically. Raising
    kills the turn, which is accepted.

    Rationale: .claude/notes/timing.md § subtract_tool_time
    """
    # (index, raw window ms) per group, captured while the message is already
    # narrowed to AssistantMessage.
    groups: dict[tuple[datetime, datetime], list[tuple[int, float]]] = {}
    for index, message in enumerate(messages):
        if not isinstance(message, AssistantMessage):
            continue
        raw = message.generation_duration_ms
        if raw is None or message.parent_tool_use_id is not None:
            continue
        groups.setdefault((message.started_at, message.completed_at), []).append((index, raw))

    out = list(messages)
    for (started, completed), members in groups.items():
        raw_total = sum(raw for _, raw in members)
        # Nothing to apportion, and the loop below divides by it — a group already
        # at zero stays at zero.
        #
        # ORDER IS LOAD-BEARING: this skip runs BEFORE the equality check below,
        # because `close_window` clamps an inverted window to 0.0 while its bounds
        # still say `completed_at < started_at` — a measured inversion the check
        # would otherwise raise on.
        # Rationale: .claude/notes/timing.md § subtract_tool_time
        if raw_total <= 0:
            continue
        bounds_ms = (completed - started).total_seconds() * 1000.0
        if not math.isclose(raw_total, bounds_ms, rel_tol=1e-9, abs_tol=_WINDOW_TOLERANCE_MS):
            raise ValueError(
                f"generation_duration_ms: a group of {len(members)} message(s) bounded "
                + f"{started} -> {completed} ({bounds_ms:.6f} ms) publishes {raw_total:.6f} ms of "
                + "generation. A reducer publishes the RAW window it measured, so its duration is "
                + "`completed_at - started_at` (or, across the sub-messages Codex splits one window "
                + "into, sums to it) — tool execution comes back out HERE, once, for every harness. "
                + "A disagreement means the reducer narrowed or widened a window without moving its "
                + "bounds, which makes the duration and the bounds two answers to one question and "
                + "breaks the four-bucket identity. Build the window with `timing.close_window` and "
                + "write `completed_at=now` (CE061), rather than adjusting the duration in place."
            )
        net = max(raw_total - busy_ms(spans, started, completed), 0.0)
        assigned = 0.0
        for n, (index, raw) in enumerate(members):
            # The last member takes the remainder so the parts reconstruct the
            # group's net exactly. NOT rounded: rounding an earlier share up could
            # push `assigned` past `net` and hand the last member a NEGATIVE
            # duration.
            share = net - assigned if n == len(members) - 1 else net * (raw / raw_total)
            out[index] = out[index].model_copy(update={"generation_duration_ms": share})
            assigned += share
    return out
