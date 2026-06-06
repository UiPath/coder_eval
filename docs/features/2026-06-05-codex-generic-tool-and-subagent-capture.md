# Codex generic tool capture + collab sub-agent attribution

**Status:** implemented
**Date:** 2026-06-05
**Context:** Follow-up to the Codex message-reconstruction work. Two gaps surfaced
while comparing Codex against Claude on the sub-agent telemetry path.

## The two bugs

### 1. Only `commandExecution` / `fileChange` items were captured

`_run_turn_with_streaming` branched explicitly on two `ThreadItem.root.type`
values. Every other tool-bearing item — `mcpToolCall`, `dynamicToolCall`,
`webSearch`, `imageGeneration`, `imageView`, `collabAgentToolCall` — was
**silently dropped**: no `ToolStartEvent`/`ToolEndEvent`, no `CommandTelemetry`,
no `tool_use` block. So MCP calls, web searches and (critically) Codex's native
sub-agent spawns never appeared in the transcript or counted toward
`command_executed` / `commands_efficiency`.

### 2. Codex sub-agents were invisible

Codex does **not** use Claude's `Task` tool. Its multi-agent feature spawns a
sub-agent via a `collabAgentToolCall` item with `tool == 'spawnAgent'`, on a new
child thread; the parent then issues a `wait` collab call that returns the
child's result in `agents_states[thread].message`. Because `collabAgentToolCall`
fell into bug #1, the evalboard showed `sub_agents = 0` and the Agent call was
not expandable — even though delegation had actually happened.

Confirmed first-hand by adding raw SDK event logging to the Codex agent
(`_log_notification_raw`, gated on `CODER_EVAL_RAW_SDK_LOG`, mirroring the Claude
agent): the spawn item carried the full prompt + spawned `model`, and the `wait`
item carried `message='5050'` on the child thread.

## The fix

### Generic tool capture

The stream loop now treats **any** item whose type is not transcript content as a
tool call. Content/metadata types live in `_CONTENT_ITEM_TYPES`
(`reasoning`, `agentMessage`, `userMessage`, `contextCompaction`,
`entered/exitedReviewMode`, `hookPrompt`, `plan`); everything else — known or
future — becomes a `tool_use` block with `ToolStart`/`ToolEnd` events and
`CommandTelemetry`. Friendly names come from `_TOOL_ITEM_NAMES`
(`commandExecution → Bash`, `fileChange → Write`, `collabAgentToolCall → Agent`,
`mcpToolCall → Mcp`, …); unknown types fall back to the raw type name so nothing
is dropped.

`commandExecution` / `fileChange` keep their dedicated rich extractors; all other
kinds route through a generic `_extract_generic_telemetry` that reads
status / duration / error generically. The same broadening was applied to the
`_messages_from_items` fallback path.

### Collab sub-agent attribution

`_handle_collab_completion` runs on every completed `collabAgentToolCall`:

- **Spawn** (`tool == 'spawnAgent'`): append one `AgentUsage` sub-agent entry and
  remember `receiver_thread_id → (spawning tool_use_id, AgentUsage)`. `wait`/
  messaging follow-ups reuse the same thread and are **not** new sub-agents.
- **Result**: stash `agents_states[thread].message` in `collab_results` — used
  only as a **fallback** (nest just that text) when the child's rollout can't be
  found. When the rollout *is* found, the child's own final generation already
  carries the returned message, so no separate nesting is needed.

### Sub-agent reconstruction from the child rollout

Codex runs each sub-agent on its own **child thread** whose events never reach
the parent stream, and that child thread persists with **Limited** rollout policy
(`persist_extended_history: false`). Under Limited mode the `commandExecution`
event is *Extended-only* (see `codex-rs/rollout/src/policy.rs`), and the
`thread/read` view builder (`ThreadHistoryBuilder`) only reconstructs command
items from those events — so neither the live stream nor `thread.read(include_turns=True)`
surfaces the sub-agent's shell commands (a `thread.read` probe returns only
`userMessage` + `agentMessage`).

But the child rollout `rollout-*-<thread_id>.jsonl` on disk persists, regardless
of mode, both the raw `function_call` / `local_shell_call` / `custom_tool_call`
(+ `*_output`) **ResponseItems** (`should_persist_response_item`) **and** a
`token_count` event per model generation. `_recover_subagent_tool_calls` runs
post-turn: for each spawned child it locates that rollout by thread id (embedded
in the filename), then `_parse_rollout_generations` splits it into **generations**
(a `token_count` is the generation boundary, same as the parent stream's
`thread/tokenUsage/updated`). For each generation, in order, it:

- emits a `ToolStart`/`ToolEnd` pair per inner tool call (so each lands in
  `TurnRecord.commands` as a `Bash`/`Write`/… row, ids prefixed `sub:<thread>:`);
  and
- nests one `AssistantMessage` (`parent_tool_use_id = the spawn's tool_use_id`)
  carrying that generation's blocks (tool_use / text, in rollout order) **and its
  real per-generation tokens** (cache-miss split, below).

So the sub-agent's `python3 -c "print(sum(range(1,101)))"` call renders as an
expandable tool row with its **actual** ~90 output tokens — not a tokenless
placeholder — followed by the `5050` reply generation with its own ~6, in the
order they happened. Recovery is best-effort and polls briefly for the async
rollout flush; any failure is swallowed so it can't fail the turn.

### Token folding (matching Claude's inclusive total)

Codex bills each sub-agent on a **separate thread**, so the parent's streamed
`thread/tokenUsage/updated` total **excludes** the child — unlike Claude, whose
sub-agent messages bubble into the parent stream and are already in the total.
To match Claude's end state:

- The spawn's `AgentUsage.tokens` is filled from the child's cumulative
  `token_count` (`_subagent_tokens_from_rollout`) — the attributed breakdown the
  evalboard shows on the Agent row.
- `_fold_subagent_tokens` adds those child tokens (and cost) into the turn total
  in `_finalize`, so the run cost reflects the sub-agent. The nested per-generation
  messages are display-only (the total comes from the SDK figure + fold, never by
  summing messages), so there's no double-count; `_resplit_total_cache` /
  `_token_usage_from_messages` deliberately consider **parent-thread messages only**.

### Cache WRITE vs cache MISS (truthful token labels)

The OpenAI/Codex convention buckets a generation's fresh slice
(`input − cached`) as `cache_creation_input_tokens` (a cache write). But after a
spawn/wait pause — or any cache eviction — a generation can re-send a large chunk
of *previously cached* prompt as a cache **miss**: `cached` drops, the fresh slice
spikes, and that spike is **not** a genuine cache write (it's a re-read at input
rate). Lumping it into `cache_creation` made tool-call generations look like they
"wrote" 11k+ tokens.

`_flush_message` now splits the fresh slice using the prompt-size delta: only the
growth since the previous generation (`raw_input − prev_prompt_tokens`) is a real
cache write; the remainder is plain `input_tokens` (a cache miss). `_resplit_total_cache`
applies the same split to the turn total (only when the per-message fresh slices
account for the total, so it's a no-op for runs without per-generation usage).
Cost is unchanged (OpenAI prices cache-write == input rate); only the write/input
**label** moves. The same split is applied to recovered sub-agent generations.

### Evalboard: render blocks in emission order

`MessageBody` (`_sections.tsx`) used to render every message as a fixed
**thinking → tools → text** layout, so a generation that emitted *text then a
tool call* (e.g. "I'm writing 5050 now" followed by the write) showed the tool
above the text. It now walks `MessageEvent.blockTypes` in order — thinking/text
render once at their first occurrence, tool calls consume `toolUses` in sequence —
so text and tools interleave as they actually happened. Agent-agnostic:
`blockTypes` preserves order for both Claude and Codex (both emit
`thinking`/`text`/`tool_use` with monotonic block sequence), with a legacy
fallback to the old grouping when a run recorded no `blockTypes`.

## Tests

`tests/test_codex_agent.py`:

- `TestCodexCollabSubAgent` — spawn records one sub-agent + two `Agent` tool calls;
  the child result nests under the spawn (`parent_tool_use_id`); a bare `wait`
  invents no sub-agent.
- `TestCodexGenericToolCapture` — MCP + web-search items become tool calls; a
  failed MCP call records an error; an unknown tool kind falls back to its raw
  type name.
- `TestCodexSubAgentToolRecovery` — a child rollout's `exec_command` is recovered
  into a `Bash` row nested under the spawn; **generations carry per-generation
  tokens in order** (the python3 generation's real 90 output tokens, then the
  `5050` reply's 6) with the cumulative breakdown folded into the turn total; a
  missing rollout falls back to nesting just the returned text (and the
  `CODEX_HOME`/`sessions` short-circuit keeps the suite fast). A dedicated test
  pins that the folded turn total equals the parent SDK total **plus** the
  recovered child total exactly (no double-count).
- `TestCodexCacheWriteBucketing::test_cache_miss_reread_is_input_not_cache_write`
  — a second generation whose prompt barely grew but mostly missed cache records
  the re-read as `input`, not an inflated cache write, at both the per-message and
  turn-total level.

`tests/test_codex_agent_live.py::test_codex_live_token_usage_populated` — asserts
the cache-write bucket convention (`input_tokens == 0` on a cold first call,
output + cache_creation/cache_read > 0).
