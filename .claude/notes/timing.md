# Timing

> Conventions and authority order: see [README.md](README.md).

## TurnClock

A turn's bounds and its durations have to share a basis or they can disagree, and the
disagreement lands in a field measured in milliseconds. Two concrete failures the class
removes:

- Antigravity computed its window span on the MONOTONIC clock while unioning WALL-clock
  tool intervals and subtracting one from the other. That is the only reason its window
  could go negative at all, and the clamp that hid it was indistinguishable from a real
  instant generation.
- Pi stamped with naive-LOCAL `datetime.now()`, and claude-code did the same. A DST
  transition or an NTP step inside a turn lands directly in a generation window — an
  hour-long jump in a millisecond field. Nightly runs start at 04:18 and run for hours,
  so it is reachable rather than theoretical. A monotonic-derived stamp cannot express it.

It is an EXTRACTION, not an invention: antigravity already captured this exact pair at
the top of `communicate` and simply did not use it for later stamps.

Stamps stay naive local, matching what the rest of the telemetry and the persisted
`execution_started_at` already are, so no consumer changes.

Within a turn the derived stamp is monotonic-accurate and may drift from real wall time;
each turn re-anchors. That is intended — do not "fix" it by re-reading the wall clock,
which is the property being removed.

One per turn, never module-level and never reused: a long run would accumulate drift
between the pair and real wall time. The turn-state constructors take it as an argument
so the lifetime is visible in the signature, and so a unit test can pass a fake straight
in. An end-to-end test driving `communicate()` cannot — the state is built inside it, out
of the caller's reach — so those replace the class through the agent module instead
(`tests/_bracket_clock.py`). Both reach the same object.

Which harnesses use it — for their window bounds and, since **CE064**, for their turn
bracket — is stated in `docs/agents/HARNESS_PARITY.md` (the `clock basis for recorded
stamps` and `turn bracket` rows), the designated SSOT for per-harness composition.
Asserting it anywhere else is the drift that put a wrong OpenCode row in that table for
months.

## _require_same_awareness

Subtracting a naive and an aware stamp raises `TypeError: can't subtract offset-naive and
offset-aware datetimes` deep inside the arithmetic, surfaces out of
`EventCollector.build_turn_record`, and kills the turn with a message naming neither the
field nor the harness. The guard turns that into a statement of which pair disagreed and
which side is aware.

Unreachable from this repo today, and that is the point: every stamp in `agents/` and
`streaming/` is a naive `datetime.now()`, so this guards the SEAM rather than a live
defect. The exposure it is for is a third-party agent registered through the
`coder_eval.plugins` SPI, which lives outside `src/coder_eval/agents/` and which no lint
rule scoped to that directory could ever see. That is why it is a runtime guard and not
a rule.

Only the MIX raises. An agent internally consistent in UTC is not this function's
problem, and neither is one that is consistently naive.

## busy_ms

The union, not the sum. Tool intervals overlap in practice — Antigravity resolves several
calls from one `Step` and backgrounds anything over ten seconds; Codex spawns collab
agents that run concurrently — so adding their durations over-counts busy time by exactly
the overlap. Subtracting such a sum from a generation window understates generation and,
with enough concurrency, drives it negative: four concurrent 400 ms calls inside a
1000 ms window sum to 1600 ms, clamping the result to the `0.0` that "unknown timing says
unknown" exists to eliminate.

Clipping to `[lo, hi]` is the other half: a tool that opened before this window only spent
part of its life inside it, and only that part is not generation time here.

The spans are awareness-checked as well as the bounds, not instead of them: the clipping
compares each span against BOTH `lo` and `hi`, so a guard on the bounds alone would leave
the function uncovered by it.

An EMPTY span list is checked not at all, bounds included. The comprehension never runs,
nothing is compared and nothing is subtracted, so there is no pair for the guard to be
about — and raising there would reject a call that has always returned `0.0`.

## union_ms

`busy_ms` with the window set to the spans' own bounds. It exists because two callers had
copy-pasted that same `min`/`max`/`busy_ms` tail — `tests/_fixtures/golden_streams/_scrub.py`
(the golden sensor) and `scripts/timing/decompose_run.py` (the live residual gate) — and
they answer the same question about the same recorded commands, so a divergence would let
one pass while the other failed. Each keeps its own stamp parsing and span building,
because their input shapes genuinely differ; only this tail is shared.

That it does not filter `end < start` holds only while every caller does. A new caller
that skips the check gets whatever `busy_ms` does with an inverted pair, which is to
discard it — silently, rather than by this function's stated contract.

## close_window

The shape all five reducers share, returning the RAW window; tool execution comes back
out centrally in `subtract_tool_time`.

`mark` is keyword-only and has NO default so that no reducer can open a window without
stating what it tiles from — the defect Pi shipped with, measuring from its own turn start
so that every inter-turn gap fell into no bucket at all. Note what the signature does and
does not buy: it constrains the call SHAPE, not the VALUE. A reducer can still pass the
wrong mark; what it cannot do is fail to have one.

`item_start` is this emission's own first stamp, when the harness has one. The `min()`
against `mark` is the tiling defense and nothing else: a stamp that went backwards must
never push the window start past the first item and invert the span. claude-code passes
none — its stream carries no per-emission item start — so its window opens exactly at the
mark.

It deliberately does not return `completed`. The window always ends at `now`, which the
caller passed in, so handing it back would be an argument returned unchanged —
redundancy dressed as symmetry.

## decompose_turn

`tool_spans` is what keeps the four buckets disjoint, and omitting it is a double-count
rather than a lost refinement. A tool is not confined to a generation window: Antigravity
force-closes an orphan at finalization, which stamps its completion inside the tail, and
it backgrounds anything over ten seconds, which can straddle either end. Such a span is
subtracted out of the windows AND counted in the tool bucket, so leaving it in the head or
tail books it twice — measured on the committed `antigravity_d_orphaned_tool` fixture as a
residual of -86% of wall clock.

The union and not the sum, because concurrent tool calls otherwise book their overlap
twice — measured: one live Pi turn overlapped a `Write` and a `Bash` by 18.4 ms.

`EventCollector` is the sole caller, and deliberately so: this is the one place the two
values are computed, after which they are persisted on `TurnRecord` and every later
consumer READS them rather than recomputing. The golden-stream sensor asserts on the
dumped record, and `scripts/timing/decompose_run.py` reads the stored fields — neither can
call this, because `task.json` carries no `AgentStartEvent` stamp to recompute a head from.

What the head CONTAINS differs per harness and is deliberately NOT split: a harness that
spawns its process per turn fuses boot, provider resolution, dispatch and TTFT — measured
on OpenCode, the process spawns in 3 ms and the first event lands at 3921 ms — while one
that spawns it once at startup has no boot inside the turn to fuse in. No stream carries a
marker between those parts. Naming these for the interval they MEASURE rather than for
what they contain is the whole point; `docs/agents/HARNESS_PARITY.md` holds the
per-harness composition.

### The golden-stream timing sensor

`tests/_fixtures/golden_streams/_scrub.py::assert_timing_captured` is a replay-based sensor
for what an AST rule cannot see, such as an SDK that returned `0.0`. It runs on the
UNSCRUBBED dump because `scrub()` keeps `None` and masks every other value: a scrubbed
snapshot shows that a field was set, never that it was set to something meaningful. A
force-closed `"unknown"` orphan is exempt from the command check because it was never
timed, and saying so is the honest record. A scenario that resolves no command passes that
check vacuously, which is correct rather than weak.

The bounds half of the generation check is not redundant. Two harnesses derive the
duration from a MONOTONIC clock and the bounds from the wall clock, so a reducer can report
a healthy duration beside two stamps that collapsed to one instant. CE059 catches that
statically only when both bounds are the same `ast.Name`; two different names holding the
same value pass CE059, and this check catches them.

The head/tail check is keyed on assistant messages with a MEASURABLE window, because that
is what the collector measures the head and tail against. Both halves of that key are
load-bearing. The `expect_generation_window` flag is the wrong key: `codex_e_orphan_tool`
streams a generation whose window subtracts to zero, so it clears the flag while still
having a head and a tail to report. "Any assistant message" is too weak:
`codex_g_items_rebuild` rebuilds its transcript from the rollout after the turn ended, with
`generation_duration_ms=None` and placeholder `now()` bounds, so there is nothing to measure
an end against and `None` for both is the honest answer.

The head/tail check asserts PRESENCE only, which is all the fixtures support: the replays
run in ~0.3 ms of synthetic wall clock, so head and tail are microseconds and any bound or
ordering check is noise. A `>= 0` check is worse than noise — `decompose_turn` clamps with
`max(..., 0.0)`, so it would restate the implementation and could never fail.

The four-bucket identity is the one assertion here that catches a DOUBLE-COUNT rather than
an absence (the orphaned-tool case under § decompose_turn). It is off for the
`FICTIONAL_DURATIONS` scenarios, which inject integer-millisecond SDK item durations of
17-900 ms while the replay takes ~0.3 ms of real wall clock, so no rebasing can make the
two commensurable.

Before the identity, the stored `tool_union_ms` is compared with a union that
`_tool_union_ms` recomputes. That helper validates the dump into a `TurnRecord` and calls
the collector's own `main_thread_tool_spans` and `union_ms`, so it cannot drift on the
selection rule (the sub-agent-id derivation, the stamp parse, the `end >= start` filter).
It still builds its own span set and union — the bookkeeping where the per-reducer defects
lived (lint-rules.md § CE063) — so it verifies the producer's bookkeeping instead of
reading the producer's answer.

It is a scenario-level floor, not a per-entry rule, because no per-entry form works against
the real snapshots. `claude_d_subagent_terminal` holds two content-bearing assistant
messages of which exactly one is legitimately `None` (the synthesized sub-agent generation,
delivered as a tool result and never streamed), so no scenario-level flag can say "this one
but not that one". "Never exactly 0.0" conflicts with the clamps that legitimately produce a
measured zero. The per-message contract lives in each agent's own unit tests; this is the
cross-harness floor.

## main_thread_tool_spans

The span set the generation subtraction, the head and the tail are all measured against,
so they cannot disagree about which calls exist. Shared with
`result_metrics.turn_time_buckets`, which answers the same question about a finished
`TurnRecord` — a second typed copy of this rule is how two report surfaces come to publish
two different tool totals for one run. (`scripts/timing/decompose_run.py` keeps its own,
over raw `task.json` dicts rather than models; that is the sanctioned third reader, and
`tests/test_timing_close_window.py::TestTheThreeToolUnionsAgree` pins all three together.)

Sub-agent tools are excluded. `_overhead_ms` once filtered its GENERATIONS to the main
thread and then passed EVERY command, so its claim to keep all four buckets measuring one
thread was true only by luck: a child nests inside the parent Agent call, whose own
interval the union already covers — but Codex's recovered child tools carry the CHILD's
clock, so nothing made it true by construction. The evalboard's twin (`toolExecutionMs`)
does filter, so the two agreed by accident.

A sub-agent's tool ids are reachable only through the messages that own them: a child
generation carries `parent_tool_use_id`, and its `tool_use_ids` are the calls it made.

## _WINDOW_TOLERANCE_MS

One millisecond, the coarsest unit a field named `_ms` can honestly be published in: a
producer that records microsecond-precision bounds and rounds its duration to whole
milliseconds is within its rights, and crashing its turns over 0.001 ms would relocate a
defect rather than remove one. The exposure the check is for is a third-party agent
registered through the `coder_eval.plugins` SPI, which is exactly the producer most likely
to round — so the tolerance has to admit it.

It still catches everything it is for. The defect class is a reducer that NARROWED or
WIDENED a window without moving its bounds — subtracting its own tool time, most plausibly
— which is tens to thousands of milliseconds, three to six orders of magnitude above this.

## subtract_tool_time

Five reducers used to do the subtraction themselves — four through `close_window` as they
flushed, claude-code once at finalization — while the head and tail were already computed
centrally. That asymmetry was the complexity, and every timing defect this branch fixed
lived in the per-reducer bookkeeping around the subtraction rather than in the subtraction
itself: when to reset a span list, when to clear a start stamp, when to advance a mark. A
reducer now publishes the RAW window and keeps only the genuinely harness-shaped decision,
which is where its window opens.

Non-mutating for aliasing reasons rather than repeated calls. Every agent builds its
terminal event as `AgentEndEvent(messages=list(...))` — that copies the LIST, not the
message objects — so writing in place would reach back into the agent's own live state
from the collector, which is exactly the layering "the collector is the sole capture seam"
exists to prevent. It is also unconditionally safe for a caller that builds a record
twice: `EarlyStopWatcher` holds one collector across a turn's tool-call rounds and calls
`build_turn_record` on every one.

Grouping by identical bounds rather than `message_id`: Codex splits one window across two
sub-messages (thinking and action) that share `started_at` and `completed_at` and divide
the window by output-token share, so subtracting the group's overlap from each part
separately would subtract it twice and stop the parts summing to the window. Bounds
identity also covers OpenCode and Pi, which can legitimately carry `message_id is None` —
keying on the id would silently collapse every id-less message of a turn into one group.

### Why the raw total must equal the bounds

That equality is what lets `generation_duration_ms` stay a PUBLISHED field rather than one
the collector derives from the bounds. Deriving it instead was considered and cut — it
would cost five reducers, a regeneration of every golden and a rewrite of CE059, whose
exemption keys on the kwarg being present at the call site — and the assertion is the
sensor that makes deferring that safe. A mismatch means a reducer narrowed or widened a
window without moving its bounds, which is the drift
`tests/_fixtures/golden_streams/_scrub.py::assert_timing_captured`'s "bounds that span it"
check catches one replay at a time.

It OVERLAPS with CE061 and is kept anyway. All five reducers build the window with
`close_window(mark=…, now=…)` and write `started_at=started, completed_at=now`, and CE061
— now exemption-free — forces that shape statically, so the equality is largely true by
construction. What the runtime check adds is the half an import-level check cannot see: a
reducer that bypasses `close_window`, and a third-party agent registered through the
`coder_eval.plugins` SPI, which lives outside `src/coder_eval/agents/` where no lint rule
reaches it. It is not load-bearing on its own.

Raising kills the turn, and that is accepted — the same trade `_require_same_awareness`
makes at this seam. The condition is unreachable without a reducer bug; all five are
exercised by the golden corpus and by the ms-exact identity contract.

### Why the zero-total skip runs before the equality check

`close_window` clamps an inverted window — `now` before `mark`, two clocks disagreeing —
to `0.0` while the bounds it writes still say `completed_at < started_at`, so the bounds
span is NEGATIVE and the equality fails. That is a measured inversion, the case
`decompose_turn` deliberately clamps because both ends were observed; raising on it would
kill turns on exactly the shape the clamp exists to tolerate. The cost is that a `0.0`
published beside a POSITIVE window slips through — a shape no in-tree reducer produces,
and one that reads downstream as "measured, and instant" rather than as a crashed turn.

### Why the apportioned shares are not rounded

The last member takes the remainder, so the parts reconstruct the group's net exactly
without rounding — while rounding each earlier share UP could push `assigned` past `net`
and hand the last member a NEGATIVE duration. That needs a net of well under a microsecond
(a window almost entirely covered by tool execution) and so had never been seen, but a
negative generation is an invariant break, not a rounding artifact.

## _overhead_ms

Measured against `AssistantMessage` entries only: a simulation turn interleaves
`UserMessage` entries, and a reconciled turn ends with a `ReconciliationMessage` that
carries no timestamps at all, so indexing the raw list would measure the wrong thing or
raise.

A message whose `generation_duration_ms` is `None` is skipped. That field is the
codebase's own marker for "no window was measurable here", and every producer of one
stamps `started_at == completed_at == datetime.now()` at *append* time as an admitted
placeholder — Codex's rollout rebuild (`_messages_from_items`), both Codex sub-agent
recovery builders, and Claude's `_synthesize_subagent_terminal_message`. Reading those
stamps as window bounds turns a placeholder into a measurement: a Codex turn rebuilt from
its rollout stamps every message at turn END, which would book the entire turn as harness
startup. It is the same exemption CE059 makes for the same reason.

`min` / `max` rather than the first and last list entries, because the list is not ordered
by time — Codex appends recovered sub-agent messages after the parent's last flush.
Positional access made the result depend on append order, which nothing enforces.

Main thread only, the same rule its two sibling call sites already apply
(`codex_agent._token_usage_from_messages` and `scripts/timing/decompose_run.py`). A
sub-agent's generations carry the spawning Agent call's `parent_tool_use_id`, and the
identity these two values complete sums generation over the main thread ONLY — the parent
tool call's own interval already spans the sub-agent's whole run. Bracketing the span with
a sub-agent message therefore shrinks the head or the tail by time no other bucket claims,
and Codex's recovered child messages carry the CHILD's clock, so the bracket can move
either way.

## Why the subtraction and the head/tail may run in either order

`EventCollector.build_turn_record` calls `subtract_tool_time` before `_overhead_ms`, and
that order is NOT load-bearing: `_overhead_ms` reads only each message's bounds, its
main-thread flag, and whether its duration is `None` — none of which `subtract_tool_time`
changes. What IS load-bearing is that both are handed the SAME span set.

## The TypeScript twin

`evalboard/lib/timing.ts::busyMs` subtracts tool time from a task's WALL CLOCK to produce
the Unaccounted residual. It answers the same question about the same `task.json`, so the
two must agree — and neither owns the numbers: `tests/_fixtures/timing_union_cases.json`
does, and both suites replay it.

The four-bucket identity has a second implementation there too: the evalboard's Unaccounted
cell (`_sections.tsx`) subtracts the same buckets from the same wall clock, as `pricing.ts`
mirrors `pricing.py`. It does not recompute a head or a tail (it reads the stored fields),
so a change in `timing.py` needs a TS change only when it alters what the buckets mean;
adding a fifth bucket means touching that cell and `sumHarnessOverhead`.

## Why timing.py is a cycle-free leaf

It sits outside `agents/` because `EventCollector` consumes it, and importing anything
under `agents/` pulls in every agent, which imports `streaming/`. The same reasoning as
`models/cli_match.py`.

## Where a reducer's window opens

NO harness subtracts tool execution from its own generation windows. Each publishes the
RAW window it measured, and `subtract_tool_time` takes the UNION of the tool intervals
back out of them once, for all five, at the single capture seam — the same place the head
and the tail are already computed.

A reducer's only remaining timing decision is where its window opens, which is the one
genuinely harness-shaped part: two interleave a tool into a single window outright
(Antigravity, whose Step for the tool arrives and only a later `usage_metadata` Step cuts
the message, and Codex, whose `_flush_message` window extends to the last item's
`completed_at_ms`) while the other three tile the turn contiguously, so a call open at a
boundary runs inside two windows. Central subtraction handles both without either reducer
knowing which it is.
