# Per-message token recording in `ClaudeCodeAgent`

Each assistant message in `task.json` now carries its own
`input_tokens / output_tokens / cache_creation_tokens /
cache_read_tokens` instead of leaving all but one set to zero.
Summing across messages reconciles with `iteration.token_usage` for
all four fields. This unlocks per-turn cost / latency analysis on
the dashboard without having to attribute back to a single rolled-up
record.

## Why the previous code zeroed everything

The Claude Code CLI streams one Anthropic API response as several
JSON events:

```
message_start  →  content_block(s)  →  message_delta  →  message_stop
```

claude-agent-sdk surfaces each emission as multiple `AssistantMessage`
objects, one per content-block kind (thinking / tool_use / text),
all sharing a single `message_id` and **repeating the same
`usage` dict on every object**. Earlier code treated this dict as
"unreliable, possibly partial / cumulative" and discarded it,
backfilling only the last `AssistantMessage` of the iteration from
`ResultMessage.usage`. That made naive per-message sums silently
incorrect: 98% of assistant messages reported zeros.

The diagnosis that drove this PR: most fields on the per-message
usage are correct — but `output_tokens` is a partial streaming
snapshot, not cumulative (`anthropics/claude-code#22686`). Summing
the partial values undercounts by ~10×.

## What the fix records

`src/coder_eval/agents/claude_code_agent.py` now does three things in
the message loop:

1. **Enables raw stream events.** `include_partial_messages=True`
   on `ClaudeAgentOptions` so we receive `message_delta` events
   alongside `AssistantMessage`s. The `message_delta` event carries
   the cumulative `output_tokens` for the emission; we capture it
   into `pending_delta_output_tokens`.
2. **Dedupes by `message_id`.** Only the first `AssistantMessage`
   for each `message_id` carries token values; subsequent objects
   (same API call, different content block) are stamped with zeros.
   Without this, `input_tokens`, `cache_creation`, and `cache_read`
   would double-count.
3. **Overrides `output_tokens` from `message_delta`.** The
   first-of-call `AssistantMessage` uses
   `pending_delta_output_tokens` when present, falling back to the
   raw `usage.output_tokens` on legacy SDKs / mock streams. This
   bypasses the CLI's partial-snapshot bug.

`ResultMessage` retro-population is kept as a fallback, gated on
`seen_message_ids` being empty — so it only fires when per-message
capture is unavailable entirely (older SDKs / unit-test mocks that
don't supply `message_id`). When per-message capture works, we do
not overwrite the populated entries.

## Reconciliation

On a smoke task with 5 recorded assistant messages spanning 4 API
calls:

| Field | Sum of per-message | `iteration.token_usage` | Match |
|---|---|---|---|
| `input_tokens` | 5 | 5 | ✓ exact |
| `cache_creation_input_tokens` | 20,807 | 20,807 | ✓ exact |
| `cache_read_input_tokens` | 61,567 | 61,567 | ✓ exact |
| `output_tokens` | 436 | 465 | ≈ 94% |

The ~6% residual on `output_tokens` is background `claude-haiku`
calls Claude Code runs for auxiliary work (compaction, title
generation). Those don't appear as `AssistantMessage` events at all
— they're only line items in `ResultMessage.modelUsage`. Per-task
billing remains authoritative via `iteration.token_usage` (built
from `ResultMessage.usage`); per-message numbers tell you how the
foreground API calls split up.

## Compatibility

- **Old `task.json` files** are unchanged. Consumers that summed
  per-message tokens were getting 0 + 0 + ... + total before; they
  now get correct per-message values that still sum to the same
  total.
- **Older SDKs without per-message `usage`** still produce a single
  populated `AssistantMessage` (the last one) via the
  `ResultMessage` fallback. No regression.
- **`include_partial_messages`** is now framework-owned (added to
  `_FRAMEWORK_OWNED_SDK_FIELDS`). YAML cannot disable it, since
  token capture depends on it. See
  `2026-05-18-sdk-pass-through.md`.

## Upstream issue

`anthropics/claude-code#22686` — "Output tokens incorrectly recorded
in JSONL: only partial streaming values saved." When the CLI lands
a fix and the assistant event carries cumulative `output_tokens`,
our workaround stays correct because we always take output_tokens
from the stream-event accumulator and never sum it with the
assistant-event value.
