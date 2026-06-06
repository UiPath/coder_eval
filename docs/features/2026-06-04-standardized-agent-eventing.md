# Standardized agent eventing

**Status:** implemented · **Date:** 2026-06-04

One event protocol, emitted only by the agent, that is faithful to each
backend's real granularity, carries tokens/cost as data, and closes every open
bracket on every exit path. Verified against `claude_hello_world` +
`codex_hello_world` (both SUCCESS, `task.json` fully populated).

## What landed

- **Event protocol** (`streaming/events.py`) — Pydantic `Agent/Turn/Tool
  Start+End` events + `ToolEndStatus`/`TurnEndStatus`/`AgentEndStatus`. The agent
  is the sole emitter; the orchestrator is a pure consumer (it no longer emits
  `TurnStartEvent`).
- **`EventCollector`** (`streaming/collector.py`) — the single agent-agnostic
  reducer from events → `TurnRecord`. `commands` is derived from the `ToolEnd`
  stream; the per-message/token payload rides on `AgentEndEvent` and is read back
  verbatim (the SDK-specific token machinery stays inside each agent, so token
  correctness is untouched). Both agents return `collector.build_turn_record()` —
  no per-agent record assembly.
- **Logging** — `LoggingStreamRenderer` (task.log) + `RichStreamRenderer`, both
  event-driven and agent-agnostic.
- **`SubAgentUsage → AgentUsage`** — composes `TokenUsage`; adds `tool_uses` and
  `per_model` (the per-model cost breakdown the cost simulator needs).
- **Crash/timeout** — a single `_finalize` closure per agent emits orphan
  `ToolEnd(unresolved)` + `TurnEnd` + `AgentEnd` on every exit path (from
  `finally`) and sets `pending_turn` from the collector.

## Core invariants

1. **Agent-only emission.** Every event originates inside `Agent.communicate()`.
   The agent is the single source of truth for its own lifecycle.
2. **Self-describing events.** Each event carries the IDs to place it in the tree
   without consumer-side state: `thread_id` (the agent context — main agent's SDK
   session id, or a sub-agent's spawning tool-use id), `parent_thread_id` (`None`
   for the main agent), `turn_id` (one inner turn within a thread), and `tool_id`
   on tool events.
3. **Strict Start/End bracketing on every exit path.** Every `…StartEvent` has
   exactly one matching `…EndEvent`, emitted even on crash or timeout (End events
   fire from `finally`). A consumer can always balance the tree.
4. **Status codes on every End event.** Success, tool error, permission denial,
   crash, timeout, and orphaned-tool-call are unified into one status mechanism
   rather than a `ToolErrorEvent` subclass + string scans.
5. **Tokens/cost on `AgentEndEvent` are authoritative.** Per-`TurnEndEvent`
   figures are best-effort and may not sum exactly to it (see *Token accuracy*).

## Event hierarchy

Events nest as a tree mirroring execution. A sub-agent is **not** a separate
event type — it is a nested `AgentStart`/`AgentEnd` pair linked to its spawning
tool call via `parent_thread_id == parent.thread_id`.

```
AgentStartEvent(thread_id=A, parent_thread_id=None, prompt, iteration)
  TurnStartEvent(thread_id=A, turn_id=t1)
    TextChunkEvent(turn_id=t1, text)
    ToolStartEvent(turn_id=t1, tool_id=x, tool_name, parameters)
    ToolEndEvent(turn_id=t1, tool_id=x, status=ok, result_preview)
    # the Agent tool spawns a sub-agent — its stream nests here, in order
    AgentStartEvent(thread_id=B, parent_thread_id=A, ...)
      ...
    AgentEndEvent(thread_id=B, parent_thread_id=A, status=completed, usage)
  TurnEndEvent(thread_id=A, turn_id=t1, status=completed, tokens)
AgentEndEvent(thread_id=A, parent_thread_id=None, status=completed, usage)
```

`AgentEndEvent` does not re-deliver child events — it is a boundary marker plus
the cumulative roll-up; consumers reconstruct the tree from stream order + IDs.
`TextChunkEvent`, `ToolStartEvent`, and `ToolEndEvent` carry no tokens/cost — a
sub-agent's cost rides on its own nested `AgentEndEvent.usage`, with
`parent_thread_id` supplying attribution.

## Status codes

```
ToolEndStatus:        ok | error | permission_denied | unresolved
TurnEndStatus /       completed | crashed | timeout | max_turns_exhausted
AgentEndStatus
```

`unresolved` is the orphaned-tool case: on crash/timeout the agent emits, in
`finally`, a `ToolEndEvent(status=unresolved)` for every `tool_id` that had a
`ToolStartEvent` but no result, then `TurnEnd(crashed|timeout)`, then
`AgentEnd` — so nothing leaks.

## Per-agent emission map

**ClaudeCodeAgent** has per-API-call fidelity: the SDK streams one
`AssistantMessage` per API call (each with its own `message_id`), so Claude
emits **N** `TurnStart`/`TurnEnd` pairs per `communicate()`. Sub-agent results
carry tokens from `tool_use_result.usage`; cumulative tokens/cost come from
`ResultMessage.model_usage` (authoritative) on `AgentEndEvent`.

**CodexAgent** has one turn per `communicate()`: one `thread.turn()` = one
`turn_id` (`turn/started` → `turn/completed`), with all `item/*` and
`thread/tokenUsage/updated` notifications scoped to it. Codex has no per-API-call
boundary, so it emits **one** `TurnStart`/`TurnEnd` pair per `communicate()`.

## Notes

- **Token accuracy.** Claude's authoritative per-model cost (`model_usage`)
  arrives only at end-of-stream, so it lands on `AgentEndEvent`. Per-`TurnEnd`
  tokens come from the approximate per-message path and may not sum exactly to
  it; the cost simulator reads `AgentEndEvent` as the source of truth.
- **Permissioning / skills.** Permission-denied is a `ToolEndStatus`, not a new
  event. Skills are observed as ordinary tool calls (Claude's `Skill` tool; Codex
  skill engagement shows up as the command/file-read that invokes it).
- **`CODER_EVAL_RAW_SDK_LOG`** stays agent-specific: it is a *pre-event* raw
  transport dump (inspecting SDK objects before they become our events), not part
  of the standardized event-driven logging.

## Deferred

Cosmetic `CommandTelemetry → ToolTelemetry` rename (zero behavior change); nested
sub-agent `AgentStart/End` *events* (sub-agent usage is still captured on
`AgentEndEvent.sub_agent_usage`, attributed by array index until the nested
events land — see `evalboard/lib/runs.ts::aggregateSubAgentUsage`); Codex
`reasoning_output_tokens` capture + per-API-call granularity.
