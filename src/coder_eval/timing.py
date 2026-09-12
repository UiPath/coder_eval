"""Wall-clock arithmetic for a turn, defined once and shared.

A cycle-free leaf (the ``models/cli_match.py`` rationale): it sits outside
``agents/`` because ``EventCollector`` consumes it, and importing anything
under ``agents/`` pulls in every agent, which imports ``streaming/``.

NO harness subtracts tool execution from its own generation windows. Each
publishes the RAW window it measured, and ``subtract_tool_time`` below
takes the UNION of the tool intervals back out of them once, for all five, at
the single capture seam — the same place the head and the tail are already
computed. A reducer's only remaining timing decision is where its window
opens, which is the one genuinely harness-shaped part: two interleave a tool
into a single window outright (Antigravity, whose Step for the tool arrives and
only a later ``usage_metadata`` Step cuts the message, and Codex, whose
``_flush_message`` window extends to the last item's ``completed_at_ms``) while
the other three tile the turn contiguously, so a call open at a boundary runs
inside two windows. Central subtraction handles both without either reducer
knowing which it is.

There is a TypeScript twin, ``evalboard/lib/timing.ts::busyMs``, which
subtracts tool time from a task's WALL CLOCK to produce the Unaccounted
residual. It answers the same question about the same ``task.json``, so the
two must agree — neither owns the numbers: ``tests/_fixtures/timing_union_cases.json``
does, and both suites replay it.
"""

import math
import time
from collections.abc import Iterable
from datetime import datetime, timedelta

from coder_eval.models import AssistantMessage, CommandTelemetry, TranscriptMessage


class TurnClock:
    """One (wall, monotonic) pair per turn; every later stamp derives from it.

    A turn's bounds and its durations have to share a basis or they can
    disagree, and the disagreement lands in a field measured in milliseconds.
    Two concrete failures this removes:

    * Antigravity computed its window span on the MONOTONIC clock while
      unioning WALL-clock tool intervals and subtracting one from the other.
      That is the only reason its window could go negative at all, and the
      clamp that hid it was indistinguishable from a real instant generation.
    * Pi stamped with naive-LOCAL ``datetime.now()``, and claude-code did the
      same. A DST transition or an NTP step inside a turn lands directly in a
      generation window — an hour-long jump in a millisecond field. Nightly
      runs start at 04:18 and run for hours, so it is reachable rather than
      theoretical. A monotonic-derived stamp cannot express it.

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
    signature, and so a unit test can pass a fake straight in. An end-to-end
    test driving ``communicate()`` cannot: the state is built inside it, out of
    the caller's reach, so those replace this class through the agent module
    instead (``tests/_bracket_clock.py``). Both reach the same object.

    NOT for deadlines. Those stay on ``time.monotonic()`` directly: a deadline
    must not move when the wall clock steps.

    Antigravity, Pi and claude-code use it — for their window bounds and, since
    CE064, for their turn bracket. Codex and OpenCode do not. This docstring
    deliberately says no more than that: asserting a current property of two
    other modules from here is the drift that put a wrong OpenCode row in the
    parity table for months, and that table is the designated SSOT for
    per-harness composition. See the `clock basis for recorded stamps` row in
    docs/agents/HARNESS_PARITY.md, and the paragraph below it for why each
    unconverted harness stays that way.
    """

    def __init__(self) -> None:
        self._wall0 = datetime.now()
        self._mono0 = time.monotonic()

    def now(self) -> datetime:
        return self._wall0 + timedelta(seconds=time.monotonic() - self._mono0)


def _require_same_awareness(a: datetime, b: datetime, *, field: str) -> None:
    """Raise if one stamp is timezone-aware and the other is naive.

    Subtracting the two raises ``TypeError: can't subtract offset-naive and
    offset-aware datetimes`` deep inside the arithmetic below, which surfaces
    out of ``EventCollector.build_turn_record`` and kills the turn with a
    message naming neither the field nor the harness. This turns that into a
    statement of which pair disagreed and which side is aware.

    Unreachable from this repo today, and that is the point: every stamp in
    ``agents/`` and ``streaming/`` is a naive ``datetime.now()`` (verified by
    grep — zero ``timezone.utc`` / ``astimezone`` / ``tzinfo`` hits), so this
    guards the SEAM rather than a live defect. The exposure it is actually for
    is a third-party agent registered through the ``coder_eval.plugins`` SPI,
    which lives outside ``src/coder_eval/agents/`` and which no lint rule
    scoped to that directory could ever see. That is why this is a runtime
    guard and not a rule.

    Only the MIX raises. An agent that is internally consistent in UTC is not
    this function's problem, and neither is one that is consistently naive.
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

    Every stamp reaching this function is a naive ``datetime.now()`` today —
    that is true of all of ``agents/`` and ``streaming/`` — so a mixed pair
    means an agent has started recording aware stamps, and
    ``_require_same_awareness`` names which pair rather than letting a bare
    ``TypeError`` escape from the arithmetic. The spans are checked as well as
    the bounds, not instead of them: the clipping below compares each span
    against BOTH ``lo`` and ``hi``, so a guard on the bounds alone would leave
    this function uncovered by it.

    An EMPTY span list is checked NOT AT ALL, bounds included. The comprehension
    never runs, nothing is compared and nothing is subtracted, so there is no
    pair for the guard to be about — and raising there would reject a call that
    has always returned ``0.0``.
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

    ``busy_ms`` with the window set to the spans' own bounds. It exists because
    two callers had copy-pasted that same ``min``/``max``/``busy_ms`` tail —
    ``tests/_fixtures/golden_streams/_scrub.py`` (the golden sensor) and
    ``scripts/timing/decompose_run.py`` (the live residual gate) — and they
    answer the same question about the same recorded commands, so a divergence
    would let one pass while the other failed. Each keeps its OWN stamp parsing
    and span building, because their input shapes genuinely differ; only this
    tail is shared.

    It does NOT filter ``end < start``. EVERY caller drops those while building
    its span list — ``main_thread_tool_spans`` below (shared by
    the collector and the report layer), ``_scrub.py`` and
    ``decompose_run.py`` — so guarding again here would be a second rule about
    the same input in a second place. That reasoning holds only while it stays
    true of every caller: a new one that skips the check gets whatever
    ``busy_ms`` does with an inverted pair, which is to discard it, but
    silently rather than by this function's stated contract.
    """
    if not spans:
        return 0.0
    return busy_ms(spans, min(s for s, _ in spans), max(e for _, e in spans))


def close_window(*, mark: datetime, now: datetime, item_start: datetime | None = None) -> tuple[datetime, float]:
    """Open one generation window at ``mark`` and close it at ``now``: its ``(started, span_ms)``.

    The shape all five reducers share. What it returns is the RAW window —
    tool execution is taken back out of it once, centrally, in
    ``subtract_tool_time`` below, which is the only place
    that arithmetic lives. It used to happen here too, per flush, and in
    claude-code at finalization; the per-reducer bookkeeping that required
    (a span list, its reset rule, the set of still-open calls) is where every
    timing defect on this branch actually lived.

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
    item and invert the span. claude-code passes none — its stream carries no
    per-emission item start — so its window opens exactly at the mark.

    The result is clamped at ``0.0``: an inverted window (``now`` before
    ``mark``, two clocks disagreeing) is a measured zero, not a negative
    generation.

    It deliberately does NOT return ``completed``. The window always ends at
    ``now``, which the caller passed in, so handing it back would be an
    argument returned unchanged — redundancy dressed as symmetry. Call sites
    write ``completed_at=now`` directly.
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

    The head means ONE thing on all five: wall clock from the turn starting
    until the harness first observed model output. Every reducer opens its first
    generation window at that same instant, which is what keeps the two buckets
    disjoint. What the head CONTAINS still differs and is deliberately NOT
    split: a harness that spawns its process PER TURN fuses that boot, provider
    resolution, dispatch and TTFT — measured on OpenCode, the process spawns in
    3 ms and the first event lands at 3921 ms — while one that spawns it once at
    startup and holds it across turns has no boot inside the turn to fuse in. No
    stream carries a marker between those parts. Naming these for the
    interval they MEASURE rather than for what they contain is the whole point;
    see docs/agents/HARNESS_PARITY.md for the per-harness composition.

    Every stamp reaching this function is a naive ``datetime.now()`` — that is
    true of all of ``agents/`` and ``streaming/`` today — so a mixed pair means
    an agent has started recording aware stamps, and ``_require_same_awareness``
    says so rather than letting a bare ``TypeError`` escape and kill the turn.

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
    with ``reports_stats.turn_time_buckets``, which answers the same question
    about a finished ``TurnRecord`` — a second typed copy of this rule is how
    two report surfaces come to publish two different tool totals for one run.
    (``scripts/timing/decompose_run.py`` keeps its own, over raw ``task.json``
    dicts rather than models; that is the sanctioned third reader, and
    ``tests/test_timing_close_window.py::TestTheThreeToolUnionsAgree`` pins all
    three together.)

    Sub-agent tools are excluded, and that used to be the gap: ``_overhead_ms``
    filtered its GENERATIONS to the main thread and then passed EVERY command,
    so its claim to keep all four buckets measuring one thread was true only by
    luck. It held because a child nests inside the parent Agent call, whose own
    interval the union already covers — but Codex's recovered child tools carry
    the CHILD's clock, so nothing made it true by construction. The evalboard's
    twin (``toolExecutionMs``) does filter, so the two agreed by accident.

    A sub-agent's tool ids are reachable only through the messages that own
    them: a child generation carries ``parent_tool_use_id``, and its
    ``tool_use_ids`` are the calls it made.

    An inverted pair (``end`` before ``start``) is dropped here rather than
    passed on. ``busy_ms`` would discard it anyway, but ``timing.union_ms``
    documents that it does NOT filter them because its callers do — so this is
    the caller keeping that true.
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


def subtract_tool_time(
    messages: list[TranscriptMessage],
    spans: list[tuple[datetime, datetime]],
) -> list[TranscriptMessage]:
    """Take tool execution back out of the generation windows it overlapped.

    THE one place this happens. Five reducers used to do it themselves — four
    through ``close_window`` as they flushed, claude-code once at finalization —
    while the head and tail were already computed centrally, right here. That
    asymmetry was the complexity, and every timing defect this branch fixed
    lived in the per-reducer bookkeeping around the subtraction rather than in
    the subtraction itself: when to reset a span list, when to clear a start
    stamp, when to advance a mark. A reducer now publishes the RAW window and
    keeps only the genuinely harness-shaped decision, which is where its window
    opens.

    NON-MUTATING, and the reason is aliasing rather than repeated calls. Every
    agent builds its terminal event as ``AgentEndEvent(messages=list(...))`` —
    that copies the LIST, not the message objects — so writing in place would
    reach back into the agent's own live state from the collector, which is
    exactly the layering "the collector is the sole capture seam" exists to
    prevent. ``model_copy`` keeps it one-directional. It is also unconditionally
    safe for any caller that builds a record twice: ``EarlyStopWatcher`` holds
    one collector across a turn's tool-call rounds and calls
    ``build_turn_record`` on every one.

    GROUPED BY IDENTICAL BOUNDS, not by ``message_id``. Codex splits one window
    across two sub-messages (thinking and action) that share ``started_at`` and
    ``completed_at`` and divide the window by output-token share; subtracting
    the group's overlap from each part separately would subtract it twice and
    stop the parts summing to the window. Bounds identity covers that, and it
    also covers OpenCode and Pi, which can legitimately carry
    ``message_id is None`` — so keying on the id would silently collapse every
    id-less message of a turn into one group.

    MAIN THREAD ONLY. A sub-agent generation (``parent_tool_use_id`` set) is
    skipped: its own tools are not in this span set, and the Agent call that
    spawned it already covers its whole run.

    A ``generation_duration_ms`` of ``None`` means no window was ever measured
    (codex's rollout rebuild, claude's synthesized sub-agent terminal), so there
    is nothing to subtract from and it passes through untouched — never
    coerced to ``0.0`` (CE058). Every non-``AssistantMessage`` entry — a
    simulation ``UserMessage``, the appended ``ReconciliationMessage`` — passes
    through by identity.

    A window entirely covered by tool execution reaches ``0.0``, and that is a
    measurement rather than an absence.

    THE GROUP'S RAW TOTAL MUST EQUAL THE SPAN ITS BOUNDS DESCRIBE, and this
    function raises if it does not. That equality is the contract that lets
    ``generation_duration_ms`` stay a PUBLISHED field rather than one the
    collector derives from the bounds: a reducer publishes the raw window it
    measured, so the duration is ``completed_at - started_at`` (or, for a group
    Codex split across two sub-messages, sums to it). Deriving it here instead
    was considered and cut — it would cost five reducers, a regeneration of
    every golden and a rewrite of CE059, whose exemption keys on the kwarg being
    present at the call site — and this assertion is the sensor that makes
    deferring that safe. A mismatch means a reducer narrowed or widened a window
    without moving its bounds, which is the drift
    ``tests/_fixtures/golden_streams/_scrub.py::assert_timing_captured``'s
    "bounds that span it" check catches one replay at a time.

    It OVERLAPS with CE061 and is deliberately kept anyway. All five reducers
    build the window with ``timing.close_window(mark=…, now=…)`` and write
    ``started_at=started, completed_at=now``, and CE061 — now exemption-free —
    forces that shape statically, so the equality is largely true by
    construction. What this adds is the runtime half: a reducer that bypasses
    ``close_window`` in a way an import-level check cannot see, and a
    third-party agent registered through the ``coder_eval.plugins`` SPI, which
    lives outside ``src/coder_eval/agents/`` where no lint rule reaches it. It
    is not load-bearing on its own.

    RAISING KILLS THE TURN, and that is accepted — the same trade
    ``timing._require_same_awareness`` makes at this seam. The condition is
    unreachable without a reducer bug; all five are exercised by the golden
    corpus and by the ms-exact identity contract.
    """
    # (index, raw window ms) per group. The raw value is captured HERE, where
    # the message is already narrowed to AssistantMessage, so the apportioning
    # loop below needs no second narrowing.
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
        # Nothing to apportion, and dividing by it is a ZeroDivisionError. A
        # group already at zero stays at zero.
        if raw_total <= 0:
            continue
        bounds_ms = (completed - started).total_seconds() * 1000.0
        if not math.isclose(raw_total, bounds_ms, rel_tol=1e-9, abs_tol=1e-6):
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
            # group's net exactly, rather than drifting by the rounding.
            share = net - assigned if n == len(members) - 1 else round(net * (raw / raw_total), 6)
            out[index] = out[index].model_copy(update={"generation_duration_ms": share})
            assigned += share
    return out
