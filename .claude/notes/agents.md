# Agents

> Conventions and authority order: see [README.md](README.md).

## Token accounting and the reconciliation message

- **Sub-agent token accounting**: There is NO separate per-sub-agent field. Every
  sub-agent generation is captured as a `parent_tool_use_id`-tagged `AssistantMessage`
  in the turn transcript, so per-sub-agent usage is derived by grouping those messages
  on that id (the evalboard's `aggregateSubAgentUsage` does exactly this). Claude
  bubbles its sub-agent's intermediate generations into the parent stream natively, and
  the **terminal** generation (delivered as the Agent tool result, never streamed) is
  synthesized into one via `_synthesize_subagent_terminal_message` from
  `tool_use_result.usage`. Codex reconstructs all child generations from the child
  rollout; both harnesses' turn totals already include sub-agent cost.
  `CommandTelemetry.result_summary` is stored **untruncated** (no 200-char cap) so
  sub-agent returns are preserved whole. Set `CODER_EVAL_RAW_SDK_LOG=1` to dump every
  raw SDK event to the task log for inspection.

- **Reconciliation message (stream self-reconciles to the turn total)**: The per-message
  stream consistently under-reports the authoritative turn total — a fixed prompt slice
  (~512 input tokens on Claude) is billed on no SDK-emitted message, and sub-agent
  input/cache only partially bubbles up. So `EventCollector.build_turn_record` appends
  one synthetic `ReconciliationMessage` (`role="reconciliation"`, in the
  `TranscriptMessage` union) per turn, carrying the per-bucket residual = `token_usage`
  − Σ(assistant message buckets). The invariant: **summing the four token buckets across
  `TurnRecord.messages` (assistant + reconciliation) equals `token_usage` exactly**, for
  both Claude and Codex (Codex's stream is already complete after
  `_recover_subagent_tool_calls`, so its residual is usually 0 and no entry is emitted).
  This is what lets the evalboard SUM the message stream as the source of truth instead
  of reading a separate aggregate ("agent tokens"): `selectTokenTotals` returns the
  stream sum whenever a reconciliation entry is present, and the timeline renders it as
  its own row. It is agent-agnostic (booked at the single `EventCollector` seam),
  carries no cost (cost stays on `token_usage`), and is excluded from generation/turn
  counts and the cost simulator. The LiteLLM open-weight actual-cost join
  (`litellm_cost.apply_actual_cost`) deliberately writes cost at the TURN level only
  (`token_usage.total_cost_usd` = the real OpenRouter bill) plus the per-call
  `TurnRecord.provider_call_costs` audit record; it does NOT touch the message token
  buckets, so `EventCollector` stays the single writer and this invariant holds on every
  backend. The Python `token_usage`/`total_token_usage` aggregate is unchanged and still
  authoritative for budget/judges/reports. The residual is almost always positive; a
  NEGATIVE one means the captured generations over-report some bucket, which is why the
  note's wording is branched — a `-512` entry must not read as "billed but not
  surfaced".

### The result_tokens measure and CE043

`result_tokens` approximates the size of the tool result the model received, derived from the
UNTRUNCATED summary content. It is deliberately cache-independent — available identically
whether prompt caching was on or off — because the alternative, inferring result size from
prompt-cache growth, is unavailable when caching is disabled. Approximate rather than the
API's exact count, but deterministic and always present.

The measure is only meaningful while the summary stays whole. An agent that truncates a
command's output before recording it, as one once did, silently under-reports that command's
result — which is what CE043 forbids. One-line summaries of non-command tool items are
intentionally brief and out of scope; trimming for DISPLAY belongs in the renderers.


## Harness run-limit parity

- **Harness run-limit parity**: a shared `BaseAgentConfig` field must mean the same
  thing on every backend, so a divergence is either fixed or documented — never silent.
  **`run_limits.max_tool_calls` is the `TurnMonitor`'s cap, in resolved tool calls, on
  every harness**: no adapter counts it; each stops at its next `should_stop` poll and
  finalizes cleanly as `tool_calls_exhausted` (no crash, no retry).

  The **known unfixed divergences** — which config fields each harness does and does not
  enforce, and the per-harness `agent.plugins[].path` depth (claude-code REQUIRES a
  plugin root holding `skills/` and silently loads NOTHING from a bare skills directory,
  which is the costly direction: no error, every positive row of an activation suite
  scores 0, and the suite reports recall 0.0, reading exactly like a skill that never
  triggers; held to the plugin-root shape for `SKILL_SOURCE_PATH` by CE045) — are the
  table's to state, not this file's. Full table + rationale:
  docs/agents/HARNESS_PARITY.md.

  The agent-field half of parity is now the `HarnessContract` each agent class declares:
  a field, `permission_mode` value or tool name a harness cannot honor is a resolution
  error, and `make parity-table` renders the contract (CE069 checks it), so the page can no
  longer drift from the adapters. The run-limit half is still the hand-written table above;
  Plan 2 moves it onto the contract.

## Shared turn lifecycle

Every adapter drives the same skeleton, on the base class: `_begin_turn()` resets the
pending slot and bumps the iteration counter, `_end_turn_ok()` marks the turn clean, and
`_mark_stopped()` closes the agent. Before raising on a mid-turn failure an adapter sets
`pending_turn` to a `crashed=True` `TurnRecord` and raises bare, which is what lets the
orchestrator drain the partial record and un-bump the iteration.

The record is BUILT before `_end_turn_ok()` on every harness: a failure inside the
reduction is a failed turn, and `_end_turn_ok` would already have cleared the rollback
flag `discard_pending_turn` needs.

Three exit paths converge on `finalize`, and it is idempotent on all of them, because the
protocol allows EXACTLY ONE `AgentEndEvent` per `communicate()`: the clean return, the
crash/timeout kernel, and the outer `except` that can fire *after* a normal finalize (a
failure while building the record). The first call wins, so a late crash cannot emit a
second terminal event into the caller's `stream_callback` — it still raises, so the
failure is not swallowed.

`TurnEndStatus` mirrors `AgentEndStatus` value-for-value precisely so the conversion in
`finalize` is total: an unmapped future member raises loudly instead of silently
bucketing to COMPLETED.

Status precedence is the same everywhere: timeout > stopped_early > tool_calls_exhausted >
completed. `stopped_early` outranks the cap because an armed criterion deciding the
outcome is the more specific reason to have cut the run; the `TurnMonitor` evaluates the
armed criteria before the cap, so an armed stop wins a tie and the first latched reason is
final.

## Why a post-stop exception is not a crash

Once the loop has broken on purpose — any `should_stop` reason, including the tool-call
cap — an exception raised while tearing the stream down must NOT be escalated. Escalating
triggers the orchestrator's retry with the monitor's decision still latched, so the retry
stops at its first poll having spent nothing useful. `ended_cleanly` is the guard.

## Why the constructors declare every kwarg

`create_agent` calls `agent_class(config, route=route, **kwargs)` through a
`cast(Any, ...)`, so pyright checks nothing at the call site; a `**_` sink would mean
nothing checks it at runtime either. The orchestrator depends on that `TypeError` as a
signal: a kwarg forwarded into a constructor that does not declare it must be loud, not
silently dropped. `cost_log_tags` is declared on the base `Agent.__init__` and every
subclass forwards it, so the factory passes it on every LiteLLM route without a
capability gate.

`route` is accepted for factory parity and deliberately unused by the CLI-driven
harnesses: those CLIs own their own provider configuration.

## First-generation window seeding

`harness_startup_ms` is defined as the wall clock from the turn starting until the harness
first observed model output, and that instant is also where the first generation window
opens — which is what keeps the head and the generation disjoint so the four-bucket
identity closes.

Without the re-seed the mark is stamped when the turn state is built, BEFORE
`AgentStartEvent` is emitted, so the head is a small negative that `decompose_turn` clamps
to `0.0` — a clamped inversion published as "measured, and instant", the exact confusion
CE058 exists to prevent. The fault is not the clamp: the head was measured against the
WRONG INSTANT. Everything the harness spent booting, resolving a provider and reaching its
first token was booked as msg0's generation instead — ~3.6 s per turn on claude-code,
~4.7 s on Antigravity against a later-window median of 3.3 s.

**ONCE PER TURN, and that is the whole contract.** The seeding hook runs for every stream
event; re-seeding on each would stop the windows tiling and drop the gap before the next
emission — a tool result landing, then the next request going out — into no bucket at all,
which is the defect Pi shipped with. The flag needs no reset: a fresh turn state (and a
fresh `TurnClock`) is built per `communicate()`, so it is per-attempt by construction. If
a future harness reuses a turn state, the reset belongs there.

A turn that never observes model output keeps the turn-entry mark and clamps to `0.0`
exactly as before. That is the correct degradation, not a gap.

**Antigravity gates on the Step SOURCE.** `StepSource` carries `SYSTEM` and `USER` besides
`MODEL`, and `StepType` carries `SYSTEM_MESSAGE` / `COMPACTION` / `FINISH`; the SDK's event
processor queues every `step_update` verbatim, so a turn can legitimately open with one.
Seeding on such a Step would put the mark BEFORE the model spoke and hand the remainder
back to msg0's generation — the defect the seeding exists to remove. The same gate guards
text streaming.

What differs between claude-code and Antigravity is not in-process versus subprocess —
both spawn a binary. Antigravity spawns its `localharness` ONCE, in `start()`, and holds
it across every `communicate()`, so there is no boot inside a turn for the head to
contain: it is dispatch plus time to first token. claude-code spawns a fresh CLI per turn
and fuses that boot in. The head means the same thing on both; only its COMPOSITION
differs, which is a real property of the harness rather than a measurement artifact.

**One route is operator-reachable and worth knowing.** claude-code sets
`include_partial_messages=True` BEFORE spreading `**self.config.sdk_options`, so
`-D agent.sdk_options.include_partial_messages=false` turns the raw stream off, and with
it the re-seed — the head silently returns to the clamped `0.0` it used to publish.
Nothing warns; the degradation is safe but the number changes meaning.

## Per-harness generation marks

Where each reducer OPENS its window — the one genuinely harness-shaped timing decision
left. The central rule this sits under (raw windows, one subtraction seam, and why
interleaving versus tiling does not matter to it) is
[timing.md § Where a reducer's window opens](timing.md); do not restate it here.

- **claude-code** tiles from the previous emission's arrival. The mark is deliberately
  NOT advanced when a tool result arrives: resetting it there opened the next window at
  the instant the RESULT landed rather than at the previous window's close, so everything
  between the tool finishing and its result reaching the handler (SDK transport, CLI
  processing, next-request dispatch) fell into no bucket. Measured on
  `tasks/dataset_example.yaml`: a 21.5 ms `Write` followed by a 2511.7 ms round trip —
  21% of an 11.7 s turn accounted to nothing. A tool-heavy shape hides this because the
  tool union absorbs the interval; a fast tool leaves it exposed.
- **OpenCode** tiles from the previous `step_finish`. The CLI announces a step only once
  it is already producing one, so a window bounded by `step_start` drops the model time
  that PRODUCED the step. Measured on `tasks/hello_date` with a live claude-haiku-4.5: two
  gaps of 857 ms and 851 ms carrying no tool (the `Write` inside them took 7 ms), 24% of
  the turn's wall clock — enough on its own to hold OpenCode above the evalboard's 25%
  "Unaccounted" red threshold.
- **Pi** tiles from the previous `turn_end`. It was the only harness measuring from its
  own `turn_start`, so the wall clock between one `turn_end` and the next `turn_start` —
  the model time that PRODUCED that turn — fell into no bucket at all.
- **Codex** tiles from the previous flush's end. The SDK stamps an item with the moment it
  began EXECUTING, so seeding from it discarded the gap between the last item's completion
  and this one's start. Measured on `tasks/hello_date`: a `Write` emission spanning 2 ms
  reported 98 output tokens while 2694 ms of real generation sat in the preceding gap;
  across that turn only 15.8% of the 17 s wall clock was accounted for.
- **Antigravity** does not tile at all — it interleaves the tool INTO the window. Do NOT
  "simplify" it to resetting the mark when a tool ends — that loses
  real model time: measured on run `2026-09-09_04-18-50`, task
  `skill-rpa-uia-google-search`, a harness-local `Read` closed 8 ms after it opened while
  6.4 s of model time separated the two flushes around it. Publishing the RAW window
  handles that case AND its opposite (a 43 s `Bash`, where the model time really is the
  flush-to-DONE remainder).

On every harness the mark advances ONLY after a flush that actually appended a message: a
step that never finished published nothing, so tiling past it would attribute its time to
whichever step finishes next.

Pi and OpenCode also clear the step's own start stamp at flush, because it has been SPENT
into the message. It is passed to `close_window` as `item_start`, whose `min()` pulls the
window open to cover it; left in place, a second `turn_end`/`step_finish` with no
intervening start — a duplicate or replayed line, which these reducers promise to survive
— reopens the next window back at the previous step's start and publishes that whole span
a second time (reproduced on Pi: 3000 ms of generation for a 2000 ms turn). Pi clears its
text and tool-id lists for the same reason: otherwise the previous turn's text is
re-emitted as its own assistant message and the same `tool_use_ids` are re-listed, so one
tool call appears to belong to two generations.

There is no per-step span list to reset any more, and that whole class of defect went with
it: the central subtraction sees every span at once and clips each to the window it
overlaps, so a call closing in the gap before a step start needs nobody to remember it.
The rule that used to live there was wrong once on OpenCode — clearing at `step_start`
wiped the span before `step_finish` could subtract it, a 100% overstatement of that window.

## Why a clean exit can still be a crash

An exit code of 0 with no telemetry is indistinguishable from a real pass in every
aggregate, and file-based criteria can still score it SUCCESS. Worse, a turn with no
tokens is one whose `max_total_tokens` / `max_usd` gates could never have tripped no
matter how much the run actually billed. So the CLI harnesses crash rather than score:

- **Vocabulary drift** — a clean exit that recognized NO event from the harness's known
  set. This has happened: OpenCode once parsed the `session.next.*` server vocabulary
  instead of the CLI's own and scored SUCCESS 1.0 with zero turns and zero tokens.
- **Finished steps with no tokens** (OpenCode) — the same outcome one layer down. Keying
  on "recognized nothing" alone left it reachable: a `step_finish` carrying no `tokens`
  key recognizes three events, books an all-zero `TokenUsage`, and `EventCollector` maps
  that to `token_usage=None` — a COMPLETED turn with no tokens, no cost, no warning. The
  arm keys on a step the CLI reported as FINISHED, its own claim that a generation
  completed, rather than on `usage.is_empty()` alone, which would also condemn a stream
  cut before any step could finish.
- **A terminal provider error** (Pi, OpenCode) — `pi -p` exits 0 after exhausting its
  internal retries, so without the crash the turn books as a clean COMPLETED
  (`FinalStatus.FAILURE`, category "failed"), silently depressing the measured pass rate.
  Crashing routes it to `FinalStatus.ERROR`, which is excluded from outcomes.
- **A CLI that closed its stream but would not exit** within the grace period.

Every arm is gated on `stopped_early` / `tool_calls_exhausted`, because an intentional cut
can land before the clearing event arrives. Pi's error case shows why: `error_message` is
set at an error `turn_end` and cleared only by a LATER non-error `turn_end`, but a
`should_stop` cut (an early stop or the tool-call cap) can fire at the next `turn_start`, leaving a stale error
from a turn Pi was still retrying. Without the guard that clean, budget-exhausted cut
would crash and burn retries, contradicting the documented "finalizes cleanly as
`tool_calls_exhausted`, no crash" contract.

OpenCode has one escape hatch, `require_token_telemetry`, for a provider or auth mode that
reports no usage at all — where crashing every turn makes the harness unusable rather than
merely imprecise. It deliberately does NOT cover vocabulary drift: that arm has silently
zeroed a whole run before, and no provider quirk explains it.

## Token accounting, per harness

Keep the buckets straight: `uncached_input_tokens` is the FRESH prompt slice only, because
cost bills it at the input rate and the cache buckets separately.

- **claude-code** prefers `ResultMessage.model_usage`, the SDK's cumulative per-model
  billing; summed and priced at list rates it equals `total_cost_usd` exactly, and it
  captures sub-agent consumption the assistant-message stream does not. The per-call
  telemetry sum is the fallback (exact only when every token-bearing emission carries an
  id), and the `usage` snapshot the last resort. Cost is backfilled from the rate card
  when a turn was timed out or killed, so there is no terminal `ResultMessage` — the
  tokens are already captured, so this is pure pricing.
- **Codex/OpenAI** report `input_tokens` INCLUSIVE of the cached prefix, and bill no
  separate cache-write fee. So the fresh slice is `input - cached`, `cache_creation` is 0,
  and `cache_read` is `cached`.
- **OpenCode** is the one stream where the convention must be decided per step: `input`
  is either already the fresh slice (flat, `total = input + output + reasoning + cache`)
  or inclusive of the cache (nested, the OpenAI `prompt_tokens` convention). The stream's
  own `total` arbitrates, so a CLI upgrade that flips the convention re-classifies itself
  instead of silently mis-booking a bucket. With no cache traffic the conventions agree.
- **Pi** reports the fresh slice directly, so it maps straight across.
- **Gemini/Antigravity** reports `prompt` (with `cached` a subset), `candidates` and
  `thoughts`. Fresh input is `prompt - cached`, cache_read is `cached`, cache_creation is
  0 (no cache-write fee), and output is `candidates + thoughts` — Gemini bills thinking as
  output.

`reasoning` bills at the output rate everywhere but is reported apart from `output`, so it
is folded into the turn total while the per-message record keeps it separately.

## Why token-shape drift warns instead of raising

A bare `int()` raises on anything non-numeric, which `communicate`'s `except Exception`
turns into an `AgentCrashError` (`max_retries=2`) — so ONE mistyped bucket burns three
full attempts and lands the task as ERROR. That is the opposite of the policy every
neighbouring field follows. A changed type in the `tokens` dict is drift, so it is
reported once per turn and the turn survives on the buckets it could read. It is also what
makes the "never raises on bad input" line-handler contract true.

The event-vocabulary check cannot see inside `usage`, which is why the CLI harnesses carry
extra guards: a renamed or absent `usage` object, an all-zero bucket set, and a
`totalTokens` that no longer reconciles with the summed buckets each warn once. Without
them a CLI upgrade silently zeroes the run's tokens and cost and blinds the budget gates.

## Cost: the stream versus the rate card

A non-zero cost the CLI reported always wins — it is the provider's own accounting, and on
OpenRouter per-request routing makes it strictly better than a static headline rate. The
rate card fills two gaps that would otherwise book tokens with no money:

- the stream reported no cost at all (a provider or auth mode that omits it, or a turn
  that died before its first finished step), or
- it reported `0` for tokens the rate card prices above zero. OpenCode reports 0 when its
  own model registry has no price for the model, or under subscription-style auth; a true
  $0 and a present-but-always-zero cost field are indistinguishable from the stream alone,
  and understating cost silently defeats `max_usd`, which is the worse failure. A
  genuinely free model has an all-zero rate entry (or none), so it still resolves to 0.

The Claude SDK's own `costUSD` is a client-side estimate assuming Anthropic pricing, so it
is wrong for an open-weight model behind LiteLLM and is repriced from the token buckets at
the model's real rate. The buckets are untouched, so the reconciliation invariant holds —
only the cost scalar changes. An unpriced model sets the cost to `None` (an honest N/A)
**and warns**. When the task sets `max_usd`, the `TurnMonitor` then raises
`BudgetUnenforceableError` at the turn end, so the row finishes `ERROR` and is never a
silent skip.

## Codex rollout rebuild

Codex runs every sub-agent on its own child thread whose events never reach the parent
stream, and that thread persists with *Limited* rollout policy, which drops
`commandExecution` events. So neither the live stream nor `thread.read` surfaces the
sub-agent's shell commands, and `thread/tokenUsage/updated` only ever reports the PARENT
thread.

But the child rollout ALWAYS persists the raw `function_call` / `local_shell_call` /
`custom_tool_call` ResponseItems and a `token_count` event with the child's cumulative
usage. Recovery mines that file, emitting one `CommandTelemetry` per inner call plus one
nested `parent_tool_use_id`-tagged `AssistantMessage` carrying that generation's real
tokens — which `_fold_subagent_tokens` then sums into the turn total, reaching the same
end state Claude gets naturally.

Recovery runs on a turn-cap stop, and that is deliberate: it is the only writer of those
tagged messages, so skipping it drops the child threads' spend from the run's cost
entirely. A cap is a routine ending, so paying ~2 s of rollout polling beats under-
reporting spend on every capped run that spawned a sub-agent. The recovered calls land in
the trajectory beyond the cap's count, the same way a force-closed orphan does — the cap
bounds what the model was allowed to DO, not what the record may explain. It is still
skipped on a cooperative stop: an armed gate has already decided the run, and children may
have no rollout yet.

The rollout file can lag the parent's `turn/completed` by a beat (the recorder flushes on
a background task), so the lookup polls ~2 s — but bails immediately when
`<home>/sessions` does not exist at all, since no flush can ever land there.

The thread-cumulative baseline is the other half. `ThreadTokenUsage.total` counts the whole
THREAD, and the Codex thread is created once per task and reused, so by turn N it still
carries turns 1..N-1. The orchestrator sums per-turn usages, so handing it the cumulative
figure books turn 1 again on turn 2 and turns 1-2 again on turn 3 — a sum of prefix sums,
inflating an N-turn task by roughly (N+1)/2. Subtracting the previous turn's snapshot
leaves this turn. A total that moved BACKWARDS means the thread restarted, so the snapshot
is already turn-local and is returned whole rather than clamped to zero.

On a crash the SDK total never arrives and the per-generation tokens on the flushed
messages are used instead — but the baseline must still advance past them, or the next
turn's delta re-books everything the crashed turn already reported.

## Why the generation is split into sub-messages

Codex flushes one generation as up to two `AssistantMessage`s — thinking and action —
sharing one `message_id` and one pair of bounds. The FIRST carries the generation's
input/cache, because those are per-CALL billing figures that must not be split; the rest
carry 0 so per-`message_id` sums do not double-count. Generation TIME is different: it is
a property of the content, so it is apportioned by output-token share, with the last part
taking the remainder. Concentrating it on the first reported the thinking row as the
entire generation and the action row as instant.

Sharing the bounds is what makes the split safe: the central subtraction groups the two
parts and takes the tool overlap out ONCE rather than once per part.

This weighs by output tokens while the evalboard's own mixed-emission split weighs by
CONTENT SIZE. Deliberate, not an oversight to unify — here the SDK hands over a real
per-spec token count, so there is no need to approximate one from content length.

## Antigravity Step interleaving and the background poll

`receive_steps()` exhausts with a tool call still open when the model kicks off a
`run_command` as a background task and goes idle without waiting for it.
`Conversation.wait_for_wakeup()` is an unimplemented stub on the Local harness connection
(always returns False regardless of pending state), so the agent polls for progress
itself — gated on that orphaned-tool signal, so a normal turn takes the branch zero times.

The poll loop is bounded by a FRACTION of the turn's `timeout`, not by `timeout` itself: a
check against the identical value races the `ThreadedWatchdog` non-deterministically for
who fires first, while a smaller fraction is a strictly earlier, non-racing deadline whose
whole purpose is to win that race. 0.8 leaves the watchdog a fifth of the budget as margin
for this loop's own exit bookkeeping. Without the bound, a tool call spuriously left
ACTIVE with no real background job behind it finalized immediately and graded whatever the
agent had produced; bounding it only by a fixed cycle count disconnected from `timeout`
(120 × 5 s = 600 s, double the framework's default `turn_timeout: 300`) makes the graceful
path unreachable — the watchdog always wins, and the same spurious-orphan turn burns the
full turn timeout before crashing with zero criteria evaluated.

The cycle cap is the SOLE bound when a task sets no timeout at all. It is deliberately not
"break after N consecutive empty polls": `receive_steps()` returns identically empty
whether a backgrounded job is still running or will never resolve, and there is no signal
that tells the two apart except waiting. A count small enough to matter would abort real
slow jobs (confirmed cases needed up to ~60 consecutive 5 s empty polls before
succeeding); one large enough to be safe barely improves on a flat cap.

`has_orphaned_tool_call` is an ALLOWLIST on ACTIVE, not a denylist on "not yet closed".
`StepStatus` also has WAITING_FOR_USER (the harness blocked on a question no one will
answer in a headless eval), CANCELED and UNKNOWN — none of which the closed set ever marks
done, and none of which the poll loop should wait out, since they will never become DONE
on their own.

## The receive_steps re-entrancy window

`receive_steps()` is two nested async generators: the public one delegates to the
connection layer, which guards re-entrancy with a flag cleared only in its OWN `finally`.
`aclosing` closes the outer generator deterministically, but a `GeneratorExit` thrown into
a delegating generator does not synchronously propagate into the inner one it was
mid-iterating — confirmed live: the inner `finally` ran only after the outer's frame
unwound AND the loop processed the abandoned generator's finalizer, i.e. on a LATER event
loop turn. So a cooperative-stop `break` can leave the connection "receiving" for a short
bounded window, and the next `receive_steps()` call raises `RuntimeError` inside it.
Retrying with `asyncio.sleep(0)` gives the already-scheduled finalizer a turn, mirroring
the SDK's own handling of this exact error in `Conversation.send()`. Retrying is preferred
over the SDK's `wait_for_idle()` fallback, which discards steps already queued and would
silently drop real content.

## Why the tool-call id falls back the way it does

`call.id` is typed optional, and the fallback must be BOTH stable across a step's own
ACTIVE → DONE re-emissions — so an id-less call's DONE step closes the SAME id its ACTIVE
step opened, rather than minting a fresh one from an already-advanced counter and
stranding the ACTIVE entry as a permanent orphan that stalls the poll loop for its full
budget — AND unique across trajectories, since a sub-agent trajectory can reuse the same
low `step_index` values as the main one. It mirrors the SDK's own
`trajectory_id:step_index` scheme rather than inventing a separate one; the call index
further disambiguates multiple id-less calls within one step, which the SDK's scheme does
not.

## Why only a RESOLVED tool is timed

On Codex, when both SDK stamps are present ``timestamp`` becomes the tool's own START
rather than the completion instant, which would place the call after its own execution.
Ordering is unaffected either way: ``TurnRecord.commands`` is sorted on
``sequence_number``, not on ``timestamp``.

An orphan force-closed by the end-of-turn sweep was never observed finishing, so the
instant the sweep runs is not a completion. Stamping it manufactures both an
`execution_completed_at` and the `duration_ms` derived from it, and the pair then reads as
a measured span that the central subtraction takes back out of a generation window it
never occupied. `execution_started_at` IS kept: the harness really did emit that start,
and one bound alone forms no span. Unknown status and unknown duration are one fact
(CE058) — claude-code's `_finalize_commands` leaves the same field `None` for the same
reason, rather than coercing it to `0.0`, which put an invented measurement on both sides
of `avg_command_time_ms`.

## Tool-name and argument normalization

Every criterion is written against the canonical (Claude) vocabulary, so each harness maps
its native tool names and per-tool argument keys onto it. Without the map a
`command_executed` with `tool_name: Bash` matches NOTHING on that harness, and the
shell-aware `parameters["command"]` extraction in `criteria/command_executed.py` degrades
to raw-JSON matching — so the same task scores differently per harness. Unknown names pass
through unchanged.

The Claude-to-native maps the uniform tool fields use are derived by inverting these, never
written twice.

Three cases are worth knowing:

- **OpenCode's tool set varies by MODEL within the one harness.** A live 174-task run
  showed DeepSeek using `write`/`edit` 199 times and `apply_patch` 0, while GPT-5.6 used
  `apply_patch` 120 times and `write`/`edit` 0. Unmapped, every `tool_name: Write` / `Edit`
  criterion scores 0 on a GPT-family model that edited the file correctly.
- **OpenCode has MOVED its file-path key.** A live capture emitted `filePath` while the
  tool schemas registered by the installed CLI read `path`. Both spellings are mapped, so
  telemetry stays canonical across the CLI versions a run might use; neither collides with
  a legitimate parameter of those tools.
- **Pi's search tool is `find`** (glob-by-pattern), not `glob`; there is no `glob` tool in
  its built-in set, so mapping `find` to the canonical `Glob` is what keeps
  `command_executed` and `commands_efficiency` comparable.

Antigravity additionally strips the result payload out of a tool call's arguments: the
harness folds result fields into the same `args` dict at DONE. Beyond a static key list,
any key that FIRST appears at DONE is treated as a result — which matters because
`skill_triggered` substring-searches every parameter value, so a leaked result could
false-positive.

## The uniform fields, per harness

`permission_mode`, `allowed_tools` and `disallowed_tools` stay on `BaseAgentConfig` as one
interface, and each harness honors them where the pinned CLI or SDK has a verified mechanism
(spike of 2026-09-16). Their meaning is the same everywhere: an allowlist permits only the
named tools, a deny always wins, and `plan` denies the Write, Edit and Bash equivalents
(`READ_ONLY_DENIED_TOOLS`, one declaration for every adapter). An empty `allowed_tools: []`
restricts nothing, because Claude Code passes `[]` as "no `--allowedTools` flag"; the same
YAML must not mean "all tools" on one harness and "no tools" on the others.

One meaning per field is not enough; each VALUE needs one too (`c/harness-architecture-comparison.md`
§ 6, P0-1 and P0-2). Two defects made that concrete. The inverse tool maps were read with
`.get(name, ())`, so a typo or a name the harness lacks silently restricted nothing. And
`permission_mode: default` meant "ask for approval" on Claude Code but "run autonomously" on the
other harnesses. So the contract lists the `permission_modes` a harness honors, and `tool_names` is a
`ToolNameMap` that is total and closed over `CANONICAL_TOOL_NAMES`: a canonical name the harness has
no tool for maps to `()` explicitly, and a missing row fails at adapter import. `Task` and `Agent` both
name the subagent tool (`TOOL_NAME_ALIASES`), so `from_inverse` gives `Task` the natives of `Agent`;
otherwise the older spelling, which the corpus still uses, would restrict nothing. Pi, OpenCode and
Antigravity honor only `plan` and `bypassPermissions` until a native mechanism with the Claude Code
meaning of `default` / `acceptEdits` is verified.

- **Pi** (0.85.1): `--tools <csv>` is an allowlist and `--exclude-tools <csv>` a denylist over
  the lowercase built-ins. The denied set is subtracted before `--tools` is emitted, and an
  allowlist that maps to nothing becomes `--no-tools`.
- **OpenCode** (1.18.30): the `permission` config accepts `"*": "deny"` plus per-key
  `allow` / `deny`, and `--auto` approves only what is not explicitly denied — so `plan` is
  explicit denies and `--auto` is passed on every run. `instructions` files are read by
  `session/instruction.ts::system()` and spread into the SYSTEM messages, so
  `system_prompt` is a temp file listed there (append). The file lives outside the sandbox
  for the same reason as the skill paths below. The permission keys are coarser than tool
  names (`edit` governs every write-shaped tool); that four-entry table is the one literal.
  `"*"` also matches non-tool permissions (`external_directory`, `doom_loop`), which the CLI
  merges before config rules and `--auto` used to approve, so an allowlist re-allows them.
  OpenCode applies the LAST matching rule, so our rules are placed after inherited ones.
- **Antigravity** (0.1.8): `hooks/policy.py` buckets specific rules above wildcard ones and
  deny above allow, so rule order does not matter. `finish` is always allowed under an
  allowlist because the harness ends a turn with it; whether `deny_all()` reaches it could
  not be probed offline, and allowing it is the safe direction.
- **Codex**: no mechanism (next section), so all three rows are unsupported.

## Codex runs full-access on every permission mode

`coder_eval` owns the isolation boundary either way — a docker container or an ephemeral
per-task tempdir — so Codex's own in-process OS sandbox is redundant. Worse, it actively
breaks on the paths the harness relies on: inside the container Landlock is unavailable,
on constrained CI agents the bwrap re-exec is denied, and on Windows there is no OS
sandbox at all. In each case a read-only or workspace-write run fails its writes silently
and scores 0 with no loud error. Dropping to full-access matches claude-code and
Antigravity, which run with no in-agent OS sandbox; it also keeps network on, so tool
installs work without extra sandbox config.

Codex's contract therefore marks `permission_mode` unsupported, so a Codex task that sets
any mode is rejected at resolution rather than believing plan/acceptEdits/default confine
it. Adversarial or untrusted evals belong on the docker driver; the tempdir/host driver is
a working directory, not a confinement boundary.

Tool restriction is not available either. `strings` on the pinned codex-cli 0.39.0 binary
shows `enabled_tools` / `disabled_tools` only inside `RawMcpServerConfig` (beside
`bearer_token_env_var`, `startup_timeout_sec`); there is no top-level key. The adapter's old
top-level `config.enabled_tools` forward therefore never restricted a tool, and was deleted.

Approval mode is `deny_all` on every permission mode too. The SDK offers only two:
`auto_review`, which puts a SERVER-SIDE reviewer in the loop that can spuriously return
"declined" under gateway load — files silently not written, a failure mode Claude has no
analog for, since its Write/Edit permissions are decided client-side — and `deny_all`,
which despite the name means "run autonomously, never prompt, no reviewer": in-sandbox
operations execute directly and only escalations BEYOND the sandbox are refused. An eval
harness never wants a reviewer that can flake.

## Codex login-shell PATH restoration

Codex issues every shell command through a LOGIN shell — `bash -lc` on Linux, `zsh -lc` on
macOS. A login shell re-sources the system profile chain (`/etc/profile`,
`/etc/zprofile`'s path_helper), which unconditionally RESETS PATH and silently drops the
mock-CLI prepend passed through the app-server environment, so bare commands resolve to
the REAL CLIs — real-tenant contamination.

The per-user dotfiles are sourced AFTER that chain, so a generated per-task HOME gets the
last word and re-prepends the mock dirs. Per-task rather than the user's real dotfiles so
parallel tasks with different mocks cannot collide. zsh selects its dotfiles by `ZDOTDIR`
rather than `HOME`, which is why both are pointed at the generated dir, and why all three
zsh files re-prepend: `/etc/zprofile` resets PATH BETWEEN `.zshenv` and `.zprofile`, and a
sourced user file may reset it again — a duplicate PATH entry is harmless, a lost prepend
is contamination.

The env `HOME` exists ONLY so bash selects the generated file; the profile's first act is
to export the ORIGINAL home back, so git, npm and every `$HOME`-relative reference keep
working. Codex state (auth, rollout sessions) is pinned separately via `CODEX_HOME`, which
is created first because the binary hard-errors on an explicitly set path that does not
exist — hosts that auth via `CODEX_API_KEY` never ran `codex login`.

`.bash_profile` mimics bash's first-found chain; the `.profile` twin sources only
`.profile`, since the bash-specific files may contain bashisms a POSIX shell would choke
on. **Known residual gap:** a NESTED bash/sh login shell inside a command re-reads the real
profiles and loses the prepend again. Nested zsh keeps it, because `ZDOTDIR` stays
exported. No-op on Windows, where Codex shells through PowerShell (`-NoProfile`) or
`cmd /c`, neither of which re-sources a profile chain.

## Reaping the CLI harnesses

`opencode run` leaves a local server child alive after the CLI exits, and it INHERITS the
stdout pipe — so EOF never arrives on its own, `readline()` would block to the turn
deadline, and signalling only the CLI pid orphans the child. Each invocation therefore runs
in its own session, so its pgid is the CLI's pid and the group holds only what that
invocation spawned; each read races against process exit, and a bounded drain collects the
tail. Sessions are persisted on disk, so killing a turn's server does not lose `--session`
continuity.

stderr is drained CONCURRENTLY from the moment the CLI starts. Reading it only after exit
deadlocks the pair: a child that fills the ~64 KiB stderr pipe blocks on write, stops
emitting stdout, and never exits, so the turn hangs to its deadline. `docker_runner` dodges
this by merging stderr into stdout; here that would corrupt the nd-JSON, so stderr gets its
own reader.

`_reap_orphaned_cli` is not merely a leak guard. Two exits reach `communicate`'s `finally`
with the child ALIVE — the `except Exception` crash and an external cancellation — and
neither passes through the graceful `kill()`. `AgentCrashError` is RETRIED, so attempt 2
would spawn a SECOND CLI against the same sandbox and session while attempt 1 is still
editing the files the criteria are about to score, and whichever writer won would decide
the task's result. It is deliberately synchronous: it runs while a `CancelledError` is
propagating, where any await can itself be cut short. `docker_runner` kills its container
from `finally` for the same reason.

The single-line read limit is raised because one nd-JSON event can carry a whole tool
result, which blows past `StreamReader`'s default 64 KiB cap and raises `ValueError`
mid-stream, killing the read loop.

Pi keeps its session dir across `kill()` and removes it only in `stop()`: the
orchestrator's mid-turn backstop calls `kill()`, and dropping the dir there would break
resume across a retried turn. `_cleanup` always calls `stop()` after any `kill()`, so the
tempdir is still reclaimed.

`_TERM_GRACE_SECONDS` is re-declared at the same value in both nd-JSON harnesses rather
than shared: the CLI-driver hoist that would unify their teardown constants and reducers is
a tracked follow-up. The shared plugin→skills resolver already lives in `agents/_skills.py`,
and `STDOUT_LINE_LIMIT_BYTES`, which IS canonical, is imported.

## The system_prompt_semantics marker

Each adapter declares how it treats `agent.system_prompt`: `append` (claude-code's
`claude_code` preset, Codex's `developer_instructions`, Antigravity's
`TemplatedSystemInstructions`, Pi's `--append-system-prompt`), `replace` (a claude-code
judge sub-agent, where the configured prompt IS the entire system prompt), or `unknown`
(OpenCode, which has no CLI knob at all).

It is recorded per run because runs from BEFORE the marker existed did not share one
regime: claude-code used replace-on-set / empty-on-unset, and Codex silently DROPPED the
field. **A trend dashboard must not pool scores across that boundary**, and an absent
marker reads as a pre-marker run — which is why every adapter spreads the base
`get_environment_info()` first rather than emitting the marker conditionally (CE046).

The class default is the `system_prompt_semantics` field of the agent's `HarnessContract`
(the base emits `"unknown"` when the contract marks `system_prompt` unsupported).
claude-code's is the only one derived per config rather than fixed, so it is computed
from the resolved prompt value and never recomputed independently — the persisted regime
cannot disagree with what was sent.

## Skills, per harness

A `plugins:` entry is a Claude-plugin root, and only the SKILLS half of it is honored
anywhere — a plugin's agents, hooks, commands and MCP servers have no equivalent outside
claude-code and are dropped. The manifest's `skills` field is read rather than `skills/`
being hardcoded, so a plugin that relocates its skills keeps working.

- **OpenCode** maps each root to `skills.paths` via `OPENCODE_CONFIG_CONTENT`, which the
  CLI merges as a final local-scope layer. That was chosen over writing
  `<sandbox>/.opencode/skills/` because it writes nothing into the sandbox that is later
  preserved as a run artifact and inspected by file criteria, and does not depend on how
  the CLI resolves a project root from `--dir`. Verified orthogonal to `--pure`, which
  skips external *plugins*, not configured skill paths. An inherited value is appended to
  rather than clobbered, since the host may legitimately configure OpenCode the same way.
- **Pi** passes each as `--skill <dir>`.
- **Codex** symlinks (or copies, on Windows) each skill dir into `.agents/skills/`, which
  the CLI auto-discovers from the working directory upward.
- **Antigravity** takes search paths natively via `skills_paths` — but those only drive
  DISCOVERY. The file-tool allowlist is `workspaces` alone, so the skill roots must appear
  there too, or the agent discovers a skill and every read of its `SKILL.md` is denied as
  out-of-workspace.

A bare skills directory is used as-is only when the root declares no `skills/` subdir.
That is deliberately not a fallback for a root that HAS one: `skills.paths` is scanned
recursively and a repo root can contain self-referential symlinks (`UiPath/skills` has
`plugins/uipath -> ..`), which resolves skills through an arbitrary path and silently drops
duplicate names.

Every way this can come up empty is logged loudly — an unresolved env var, a missing dir,
a root with no `<name>/SKILL.md` under it. A plugin whose skills never reach the agent
still *looks* like a normal run, which is precisely the failure the logging closes: the
run measures the model WITHOUT the skill under test.

## Why the registry rejects a re-registration

Re-registering the SAME classes is legitimate (an idempotent built-in reload). Re-
registering a kind with a DIFFERENT implementation is a silent shadow: which agent runs
would depend on entry-point discovery order, which is not stable across environments — a
reproducibility hole. Two plugins must not claim the same `agent.type`.

The registry is keyed by the kind STRING so a built-in `AgentKind` member and a
plugin-supplied raw string collide on one key (`AgentKind` is a `StrEnum`), which is what
lets a plugin register a brand-new kind that is not an enum member. Its imports are
`TYPE_CHECKING`-only so it imports nothing from `coder_eval` at runtime, keeping the edge
one-way — the plugin loader and the models layer import the registry, never the reverse.
`create_agent` deliberately does not import `coder_eval.plugins` itself for the same
reason; callers reach a config through `parse_agent_config`, which loads them.

## The threaded watchdog

`asyncio.wait_for` is not enough for these harnesses: the Claude SDK wraps its subprocess
in anyio cancel scopes that suppress `asyncio.CancelledError`, so cooperative cancellation
does not reliably stop a stuck CLI, and a blocking SDK call lands a cancel only at an await
point. A `threading.Timer` on a daemon OS thread fires even when the event loop is starved
or stuck on subprocess I/O.

SIGKILL is what actually releases stdout/stdin and unblocks the anyio readers so the async
generator unwinds. claude-code therefore pre-constructs its transport when a timeout is
set — the SDK's default path creates it internally and never exposes the subprocess
handle. That handle is captured in the watchdog CLOSURE rather than read from the agent,
so a stale watchdog from an earlier turn cannot kill a later turn's subprocess.

Timeout detection checks both the watchdog flag AND the wall clock: the flag-only check
races the watchdog, misreporting a timeout as a generic error when the handler is entered
just before the flag flips. On the happy path only the flag is trusted, so a wall-clock
drift during post-loop cleanup cannot reclassify a successful turn as a timeout.

`kill_sync` runs on the watchdog thread and must not await. Antigravity's cancel and
disconnect are async-only, so its hook only records intent — the genuine teardown happens
via the asyncio-task cancel the watchdog also delivers and the subsequent exit-stack close.

## Pi

`agent_start` can appear MORE THAN ONCE per invocation, because Pi auto-retries a
transient provider error internally — so `agent_end` (which carries `willRetry`) is NOT
terminal. `agent_settled`, or stdout EOF, is; emitting the single `AgentEndEvent` on the
first `agent_end` would cut the turn off mid-retry.

That retry loop is also why `on_turn_start` closes a dangling `TurnStartEvent` before
opening the next one. A generation aborted mid-turn — a provider error before the
assistant message completed, the defining `willRetry` case — otherwise leaves the stream
carrying N starts and N-1 ends, breaking the one-pair-per-inner-turn contract renderers
depend on. `finalize` closes only the LAST open turn, so it cannot cover this.

The session id is sanitized because dataset-row tasks have path-shaped ids
(`suite/row_3`, set in `task_loader`) and Pi derives its session file from the id under
`--session-dir` — so a raw `/` resolves to a non-existent subdir and fails the row before
any work is done.

## The sdk_options pass-through

`sdk_options` forwards SDK fields the framework does not model. Validation is an ALLOW rule —
a key must be a real SDK field AND not framework-owned — so the user-visible set is the
difference of the two. The denylist is explicit rather than derived, so the reason each key is
withheld stays next to the code.

What is withheld: anything `coder_eval` already owns as a typed field (setting it here would
silently shadow the typed one), anything transport- or lifecycle-critical, and anything
security-critical. Hooks, MCP servers, the permission-prompt tool, the tool callback and
sub-agent definitions all run BEFORE any allowed-tools gate — `agent_judge` forces
`setting_sources=[]` for exactly that reason, and letting `hooks` through would re-open the
hole. Session lifecycle is owned by the orchestrator's "advance the session id only on a clean
turn" logic. Budgeting overlaps the run limits the orchestrator enforces with explicit final
statuses, and two independent budget guards would disagree on counts. Telemetry is required to
recover per-emission output tokens around an upstream bug, so turning it off would silently
drop per-message accounting.

The classification is kept from failing open as the SDK grows: a test asserts EVERY field on
the SDK's options type is classified, either typed-mirrored or framework-owned, so a new SDK
release adding an unclassified field fails loudly instead of silently passing through.
