"""Wall-clock arithmetic for a turn, defined once and shared.

A cycle-free leaf (the ``models/cli_match.py`` rationale): it sits outside
``agents/`` because ``EventCollector`` consumes it, and importing anything
under ``agents/`` pulls in every agent, which imports ``streaming/``.

EVERY harness now subtracts tool execution from its generation windows before
publishing ``generation_duration_ms``, and all of them subtract the same
thing: the UNION of the intervals, clipped to the window. Two interleave a
tool into a single window outright — Antigravity (the Step for the tool
arrives and only a later ``usage_metadata`` Step cuts the message) and Codex
(``_flush_message``'s window is extended to the last item's
``completed_at_ms``). The other three reach the same place from the opposite
direction: their windows tile the turn contiguously, so a call open at a
window boundary runs inside two of them.

There is a TypeScript twin, ``evalboard/lib/timing.ts::busyMs``, which
subtracts tool time from a task's WALL CLOCK to produce the Unaccounted
residual. It answers the same question about the same ``task.json``, so the
two must agree — neither owns the numbers: ``tests/_fixtures/timing_union_cases.json``
does, and both suites replay it.
"""

import time
from datetime import datetime, timedelta


class TurnClock:
    """One (wall, monotonic) pair per turn; every later stamp derives from it.

    A turn's bounds and its durations have to share a basis or they can
    disagree, and the disagreement lands in a field measured in milliseconds.
    Two concrete failures this removes:

    * Antigravity computed its window span on the MONOTONIC clock while
      unioning WALL-clock tool intervals and subtracting one from the other.
      That is the only reason its window could go negative at all, and the
      clamp that hid it was indistinguishable from a real instant generation.
    * Pi stamped with naive-LOCAL ``datetime.now()``. A DST transition or an
      NTP step inside a turn lands directly in a generation window — an
      hour-long jump in a millisecond field. Nightly runs start at 04:18 and
      run for hours, so it is reachable rather than theoretical. A
      monotonic-derived stamp cannot express it.

    It is an EXTRACTION, not an invention: antigravity already captured this
    exact pair at the top of ``communicate`` and simply did not use it for
    later stamps.

    Stamps stay NAIVE LOCAL, matching what the rest of the telemetry and the
    persisted ``execution_started_at`` already are, so no consumer changes.

    Within a turn the derived stamp is monotonic-accurate and may drift from
    real wall time; each turn re-anchors. That is intended — do not "fix" it by
    re-reading the wall clock, which is the property being removed.

    ONE PER TURN, never module-level and never reused across turns: a long run
    would accumulate drift between the pair and real wall time. The turn-state
    constructors take it as an argument so the lifetime is visible in the
    signature, and so tests can inject a fake instead of monkeypatching a
    module global out from under the reducer.

    NOT for deadlines. Those stay on ``time.monotonic()`` directly: a deadline
    must not move when the wall clock steps.

    Codex and OpenCode deliberately do NOT use it. Their tool spans are the
    CLI's own epoch-millisecond stamps, unreachable from the host, so
    converting only the window bounds would put two bases inside one
    ``busy_ms`` subtraction — relocating the defect instead of removing it.
    """

    def __init__(self) -> None:
        self._wall0 = datetime.now()
        self._mono0 = time.monotonic()

    def now(self) -> datetime:
        return self._wall0 + timedelta(seconds=time.monotonic() - self._mono0)


def busy_ms(spans: list[tuple[datetime, datetime]], lo: datetime, hi: datetime) -> float:
    """Wall milliseconds inside ``[lo, hi]`` where at least ONE span was running.

    The union, not the sum. Tool intervals overlap in practice — Antigravity
    resolves several calls from one ``Step`` and backgrounds anything over ten
    seconds; Codex spawns collab agents that run concurrently — so adding
    their durations over-counts the busy time by exactly the overlap.
    Subtracting such a sum from a generation window understates generation
    and, with enough concurrency, drives it negative: four concurrent 400 ms
    calls inside a 1000 ms window sum to 1600 ms, clamping the result to the
    ``0.0`` that "unknown timing says unknown" exists to eliminate.

    Clipping to ``[lo, hi]`` is the other half: a tool that opened before this
    window only spent part of its life inside it, and only that part is not
    generation time here.
    """
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

    ``busy_ms`` with the window set to the spans' own bounds. It exists because
    two callers had copy-pasted that same ``min``/``max``/``busy_ms`` tail —
    ``tests/_fixtures/golden_streams/_scrub.py`` (the golden sensor) and
    ``scripts/timing/decompose_run.py`` (the live residual gate) — and they
    answer the same question about the same recorded commands, so a divergence
    would let one pass while the other failed. Each keeps its OWN stamp parsing
    and span building, because their input shapes genuinely differ; only this
    tail is shared.

    It does NOT filter ``end < start``. Both callers already drop those while
    building their span lists, so guarding again here would be a second rule
    about the same input in a second place; keeping it at the caller preserves
    today's behaviour exactly.
    """
    if not spans:
        return 0.0
    return busy_ms(spans, min(s for s, _ in spans), max(e for _, e in spans))


def close_window(
    *,
    mark: datetime,
    now: datetime,
    item_start: datetime | None = None,
    closed_spans: list[tuple[datetime, datetime]],
    open_started_ats: list[datetime],
) -> tuple[datetime, float]:
    """Close one generation window at ``now``: its ``(started, generation_ms)``.

    The shape four reducers had copy-pasted. Codex, opencode, pi and
    antigravity all call it; claude-code is the one exception and carries the
    only ``# noqa: CE061``, because it subtracts tool time once at finalization
    across every emission rather than per flush — a call issued by an earlier
    emission is still running when the next window closes.

    ``mark`` is where the window opens: the previous flush's close, which is
    what makes the windows TILE the turn contiguously instead of leaving the
    model time that PRODUCED an item attributed to nothing. It is keyword-only
    and has NO default so that no reducer can open a window without stating
    what it tiles from — which is the defect pi shipped with, measuring from
    its own turn start so that every inter-turn gap fell into no bucket at all.
    Note what the signature does and does not buy: it constrains the call
    SHAPE, not the VALUE. A reducer can still pass the wrong mark; what it
    cannot do is fail to have one.

    ``item_start`` is this emission's own first stamp, when the harness has
    one. The ``min()`` against ``mark`` is the tiling defense and nothing else:
    a stamp that went backwards must never push the window start PAST the first
    item and invert the span.

    A call still OPEN at this boundary counts against the window too, bounded
    at ``now``. Subtracting only CLOSED intervals publishes the part of a
    straddling call that ran inside this window as generation, while the call's
    own ``duration_ms`` counts it again.

    NO DOUBLE SUBTRACTION, and this is the rationale that used to sit copy-
    pasted at four call sites: when that open call later closes, the reducer
    appends its FULL interval to the next window's ``closed_spans``, where
    ``busy_ms`` clips it to the post-boundary remainder. Each millisecond of
    tool time is therefore subtracted from exactly one window.

    The UNION is subtracted, never the sum (see ``busy_ms``), and the result is
    clamped at ``0.0`` — an inverted window (``now`` before ``mark``, two
    clocks disagreeing) is a measured zero, not a negative generation.

    It deliberately does NOT return ``completed``. The window always ends at
    ``now``, which the caller passed in, so handing it back would be an
    argument returned unchanged — redundancy dressed as symmetry. Call sites
    write ``completed_at=now`` directly.
    """
    started = min(mark, item_start) if item_start is not None else mark
    bounded = [(s, now) for s in open_started_ats if s < now]
    span_ms = (now - started).total_seconds() * 1000.0
    return started, max(0.0, span_ms - busy_ms(closed_spans + bounded, started, now))


def decompose_turn(
    first_started_at: datetime | None,
    last_completed_at: datetime | None,
    agent_started_at: datetime | None,
    agent_ended_at: datetime | None,
    tool_spans: list[tuple[datetime, datetime]] | None = None,
) -> tuple[float | None, float | None]:
    """Wall ms before the first generation window opens, and after the last closes.

    The turn's two unexplained ends. Between them the windows tile (each
    harness's generation mark runs to the next) and tool execution is already
    subtracted inside them, so head + generation + UNION(tool) + tail is the
    whole turn — the union and not the sum, because concurrent tool calls
    otherwise book their overlap twice (``busy_ms`` above, and measured: one
    live Pi turn overlapped a ``Write`` and a ``Bash`` by 18.4 ms).

    ``tool_spans`` is what keeps those four buckets DISJOINT, and omitting it
    is a double-count rather than a lost refinement. A tool is not confined to
    a generation window: Antigravity force-closes an orphan at finalization
    (``antigravity_agent.py``), which stamps its completion inside the tail,
    and it backgrounds anything over ten seconds, which can straddle either
    end. Such a span is subtracted out of the windows AND counted in the tool
    bucket, so leaving it in the head or tail books it twice — measured on the
    committed ``antigravity_d_orphaned_tool`` fixture as a residual of -86% of
    wall clock. So the head and tail exclude tool time by the same rule and
    the same helper the windows use.

    ``EventCollector`` is the SOLE caller, and deliberately so: this is the one
    place the two values are computed, after which they are persisted on
    ``TurnRecord`` and every later consumer READS them rather than recomputing.
    The golden-stream sensor asserts on the dumped record, and
    ``scripts/timing/decompose_run.py`` reads the stored fields — neither can
    call this, because ``task.json`` carries no ``AgentStartEvent`` stamp to
    recompute a head from.

    What the head CONTAINS differs per harness and is deliberately NOT split.
    On an in-process SDK the first window already covers dispatch and
    time-to-first-token, so this reads ~0; on a subprocess harness it fuses CLI
    boot, provider resolution, dispatch and TTFT, and the stream carries no
    marker between them — measured on OpenCode, the process spawns in 3 ms and
    the first event lands at 3921 ms. Naming these for the interval they
    MEASURE rather than for what they contain is the whole point; see
    docs/agents/HARNESS_PARITY.md for the per-harness composition.

    ``None`` means never measured — a turn that produced no generation, or a
    snapshot taken before the terminal event. Never 0.0, which would claim a
    measurement was taken and came back instant (CE058). A measured inversion
    (the two clocks disagreeing) IS a real zero and clamps, because both ends
    were observed.

    NOTE the four-bucket identity has a second implementation in TypeScript —
    the evalboard's Unaccounted cell (``_sections.tsx``) subtracts the same
    buckets from the same wall clock, as ``pricing.ts`` mirrors ``pricing.py``.
    It does not recompute a head or a tail (it reads the stored fields), so a
    change HERE needs a TS change only when it alters what the buckets mean;
    adding a fifth bucket means touching that cell and ``sumHarnessOverhead``.
    """
    spans = tool_spans or []
    head = tail = None
    if first_started_at is not None and agent_started_at is not None:
        elapsed = (first_started_at - agent_started_at).total_seconds() * 1000.0
        head = max(elapsed - busy_ms(spans, agent_started_at, first_started_at), 0.0)
    if last_completed_at is not None and agent_ended_at is not None:
        elapsed = (agent_ended_at - last_completed_at).total_seconds() * 1000.0
        tail = max(elapsed - busy_ms(spans, last_completed_at, agent_ended_at), 0.0)
    return head, tail
