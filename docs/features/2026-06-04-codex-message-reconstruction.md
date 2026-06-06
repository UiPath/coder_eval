# Codex `messages` reconstruction — plan

**Status:** planned (not yet implemented)
**Date:** 2026-06-04
**Context:** Follow-up to the standardized-agent-eventing refactor. Codex runs render an empty transcript on the evalboard because `TurnRecord.messages` is always `[]` for Codex. Claude populates it; Codex never did.

## The thing I was missing

The Codex SDK's async stream **does** give on-the-fly, per-message events — I was only consuming a subset of them.

`turn_handle.stream()` yields a notification per event. We currently branch on four methods:

- `item/started` → only `commandExecution` / `fileChange` (ToolStart)
- `item/completed` → only `commandExecution` / `fileChange` (ToolEnd + telemetry)
- `item/agentMessage/delta` → streamed text chunks (TextChunk)
- `thread/tokenUsage/updated`, `turn/completed`

But `item/started` and `item/completed` fire for **every** thread-item type, not just commands and file changes. The ones we silently drop are exactly the ones we need for `messages`:

| Item type (`ThreadItem.root.type`) | Carries | Maps to |
|---|---|---|
| `agentMessage` (`AgentMessageThreadItem`) | `id`, `text` (full assistant text) | `text` content block |
| `reasoning` (`ReasoningThreadItem`) | `id`, `content: list[str]`, `summary: list[str]` | `thinking` content block |
| `commandExecution` | already handled | `tool_use` block (id = telemetry `tool_id`) |
| `fileChange` | already handled | `tool_use` block (id = telemetry `tool_id`) |

`ItemStartedNotification` / `ItemCompletedNotification` both carry `item: ThreadItem`, `turn_id`, `thread_id`, and `startedAtMs` / `completedAtMs`. So we get the full assistant message text (not just deltas) and per-item wall-clock timing live on the stream.

**Belt-and-suspenders:** `turn/completed` carries a `Turn` with `items: list[ThreadItem]` — the complete, ordered item list for the turn. So even if streaming dropped something, the terminal Turn has the whole transcript. The plan uses the stream as primary and `Turn.items` as a fallback when the stream produced zero messages.

So: no missing SDK capability. We just weren't reading the agentMessage/reasoning items. The fix is purely in `codex_agent.py`.

## What the evalboard needs (consumer contract)

`evalboard/lib/runs.ts::parseMessages(turns)` reads, per turn:

- `turn.messages` — only entries with `role === "assistant"` are rendered.
- Each message's `content_blocks[]` with `block_type ∈ {thinking, text, tool_use}`:
  - `thinking` → `thinking` string
  - `text` → `text` string
  - `tool_use` → `tool_use_id`, `is_error`; **joined to `turn.commands` by `tool_id`** to pull tool name / params / result preview / duration.
- Per-message `started_at`, `completed_at`, `generation_duration_ms`, `model`, `message_id`, token fields.
- Grouping: consecutive messages sharing a `message_id` are collapsed into one rendered message (falls back to a 100ms wall-clock-gap heuristic when `message_id` is absent).

We already emit `turn.commands` with the right `tool_id`s, so tool_use blocks will resolve. We only need to synthesize the `AssistantMessage` records and attach them to `AgentEndEvent.messages` — the `EventCollector` already copies `messages=list(end.messages)` into the `TurnRecord` verbatim (collector.py:111). **No collector, orchestrator, or evalboard changes required.**

## Design

Build `list[AssistantMessage]` inside `_run_turn_with_streaming`, in item-completion order, mirroring Claude's transcript semantics. Symmetric with Claude: the agent owns message/token reconstruction; it rides on `AgentEndEvent`, and the collector reads it back without re-derivation (the "deferred token/message path" the eventing plan describes).

### Segmentation & per-message tokens (revised after inspecting the event stream)

The initial cut segmented only at `agentMessage` (text) boundaries and left
per-message tokens at 0. Inspecting the real stream (`CODEX_DEBUG_EVENTS`) showed
both were wrong:

- Codex runs an agentic loop of **one generation per step** — `reasoning → tool`
  repeated, then a final `reasoning → agentMessage`. Cutting only at text merged
  every step into one giant message with a misleading turn-long
  `generation_duration_ms`.
- `thread/tokenUsage/updated` fires **exactly once per generation**, AFTER that
  generation's items, carrying `last` (that response's token delta) and `total`
  (cumulative). The `last` deltas **sum to `total`**, so per-message tokens from
  `last` reconcile to the turn total with no double-count.

So the boundary and the per-message token source are the same event:

- Cut one `AssistantMessage` per `thread/tokenUsage/updated`, attributing its
  `last` delta (`input_tokens` normalized to non-cached = `input − cached_input`,
  plus `output_tokens`, `cache_read_tokens = cached_input`, `reasoning_tokens =
  reasoning_output_tokens`). This parallels Claude, whose per-message `input_tokens`
  is also the per-call prompt size; the authoritative turn total stays on
  `TurnRecord.token_usage`.
- Tool-call items `item/started` **before** their generation's `tokenUsage` but may
  `item/completed` **after** it, so `tool_use` blocks are recorded at `item/started`
  and held by reference in `blocks_by_id`; the later `item/completed` patches
  `is_error` even after the message has been flushed.
- `reasoning`/`agentMessage` items complete before their generation's `tokenUsage`,
  so they're recorded at `item/completed` as usual.

### Accumulation (the `commands`-list pattern)

`messages` is passed **into** `_run_turn_with_streaming` as a mutable list (exactly like `commands` already is) so that on a mid-turn crash the partial transcript survives — `_finalize` reads the `communicate`-scope `messages` list and puts it on `AgentEndEvent`, whether the turn completed or raised.

Maintain a single ordered `content_blocks: list[ContentBlock]` "open buffer" while iterating notifications:

- `item/completed` `reasoning` → append `ContentBlock(block_type="thinking", thinking="\n".join(content or summary))`.
- `item/completed` `commandExecution` / `fileChange` → append `ContentBlock(block_type="tool_use", tool_use_id=<same id used for the telemetry>, is_error=<status>)`. (Done alongside the existing telemetry/ToolEnd emission.)
- `item/completed` `agentMessage` → append `ContentBlock(block_type="text", text=item.text)`, then **flush**: cut an `AssistantMessage` from the open buffer and start a new one. This keeps trailing text attached to its preceding tool calls/reasoning, matching Claude's "tool_use then text in one message" shape.
- At stream end → flush any remaining open buffer as a final `AssistantMessage` (covers turns that end on a tool call with no closing text).

Each flush:
- Re-sequences its blocks (`sequence = 0..n`).
- Sets `tool_use_ids = [b.tool_use_id for b in blocks if tool_use]`.
- `started_at` = first block's item `startedAtMs`; `completed_at` = last block's `completedAtMs`; `generation_duration_ms` = delta (fall back to `datetime.now()` / 0.0 when an item lacked timing).
- `message_id = f"codex-{iteration}-msg-{n}"` so the evalboard treats each as a distinct message (no accidental collapse).
- `model = self._effective_model()`.
- **Per-message tokens from the `last` delta.** Each generation's `thread/tokenUsage/updated.last` is attributed to its message (see the segmentation section above); the deltas sum to `TurnRecord.token_usage` (the cumulative `total`), so there's no double-count. (Superseded the original "leave at 0" plan after inspecting the stream.)

### Fallback

If the stream yielded **zero** messages but `turn_result.items` is non-empty, build the transcript from `turn_result.items` using the same item→block mapping. Guarantees a transcript even if the agentMessage arrived only in the terminal Turn.

### Wiring

- Extend `_run_turn_with_streaming(...)` to also populate the passed-in `messages` list (return signature unchanged; mutate in place like `commands`).
- In `communicate`, declare `messages: list[AssistantMessage] = []` next to `commands`, pass it in.
- `_finalize` adds `messages=messages` to the `AgentEndEvent(...)` construction. Crash/timeout paths already call `_finalize`, so partial transcripts are preserved.

## Files touched

- `src/coder_eval/agents/codex_agent.py` — only file. Add `AssistantMessage` / `ContentBlock` imports from `coder_eval.models`; handle `agentMessage` + `reasoning` items; build/flush `AssistantMessage` records; thread `messages` through `communicate` → `_finalize` → `AgentEndEvent`; add the `turn_result.items` fallback.

No changes to: `collector.py`, `events.py` (`AgentEndEvent.messages` already exists), `orchestrator.py`, evalboard.

## Verification

1. `set -a && source .env`, run the codex hello-world task into `tmp/`.
2. Inspect `task.json`: `turns[0].messages` non-empty; assistant messages present; each `tool_use` block's `tool_use_id` is also present as a `tool_id` in `turns[0].commands`; reasoning rendered as thinking blocks.
3. Run the evalboard against `tmp/` and confirm the Codex transcript renders messages + tool calls + reasoning at `/runs/<run>/codex_hello_world`.
4. Re-run claude hello-world to confirm no regression (it builds messages independently).
5. Confirm `token_usage` / cost unchanged (we add no token attribution).

## Risk notes

- Pure addition on the Codex path; Claude untouched.
- Turn-level token correctness untouched; per-message tokens come from `last` deltas that sum to the turn `total` (verified: per-message sums == `TurnRecord.token_usage`).
- Tool-join correctness depends on reusing the **same** `tool_id` for both the telemetry/`CommandTelemetry` and the `tool_use` block — must read the id once and reuse.
- Lint/tests deferred per the standing instruction; will run `make verify` before any commit, and commit only when you say so.
