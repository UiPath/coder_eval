# LLM Stdout Streaming Design

**Issue**: [#29 — Please allow streaming of LLM to stdout](https://github.com/UiPath/coder_eval/issues/29)
**Related PR:** #35
**Date**: 2026-03-03
**Status**: Approved

## Problem

During evaluation runs, users have no visibility into what the LLM agent is doing in real-time. The only feedback is a tqdm progress bar at the task level. Debug logging exists but is buried in task log files, making interactive debugging and progress monitoring difficult.

## Solution: Callback-Based Event Streaming

Thread an optional `StreamCallback` through the Agent → Orchestrator → Batch → CLI stack. The agent emits structured events during its streaming loop; a Rich terminal renderer in the CLI displays them in real-time.

## Event Model

New `coder_eval/streaming/events.py` — dataclass hierarchy:

| Event | Fields | Emitted By |
|-------|--------|------------|
| `TurnStartEvent` | task_id, iteration, max_iterations, prompt_preview | Orchestrator |
| `ToolCallEvent` | task_id, tool_name, tool_id, parameters (truncated), sequence_number | Agent |
| `ToolResultEvent` | task_id, tool_id, success, result_preview (truncated) | Agent |
| `TextChunkEvent` | task_id, text | Agent |
| `TurnCompleteEvent` | task_id, iteration, duration_s, command_count, token_usage | Orchestrator |
| `CriteriaCheckEvent` | task_id, passed, total, weighted_score, details | Orchestrator |

All events inherit from a base `StreamEvent` with `timestamp` and `task_id`.

## Callback Protocol

```python
# coder_eval/streaming/callbacks.py

class StreamCallback(Protocol):
    def on_event(self, event: StreamEvent) -> None: ...
```

Synchronous to avoid async complexity in the rendering path. Callback exceptions are caught and logged — a failing renderer never crashes the evaluation.

## Integration Points

### Agent Layer

`Agent.communicate()` gains an optional `stream_callback: StreamCallback | None = None` parameter.

In `ClaudeCodeAgent.communicate()`, events are emitted at existing processing points:
- Phase 1 (ToolUseBlock) → `ToolCallEvent`
- Phase 2 (ToolResultBlock) → `ToolResultEvent`
- TextBlock from assistant → `TextChunkEvent`

Zero overhead when callback is None (`if callback:` guards).

### Orchestrator Layer

`Orchestrator.__init__()` accepts `stream_callback`. It:
- Emits `TurnStartEvent` before `agent.communicate()`
- Passes callback to `agent.communicate()`
- Emits `TurnCompleteEvent` after communicate returns
- Emits `CriteriaCheckEvent` after success criteria evaluation

### Batch Layer

`run_batch()` accepts `stream_callback_factory: Callable[[str], StreamCallback] | None`.

Factory takes `task_id` and returns a callback instance. In the current implementation, a single shared `RichStreamRenderer` is returned for all tasks (thread-safe via `threading.Lock`). The `TaskScopedCallback` wrapper stamps the correct `task_id` on agent-emitted events before forwarding to the renderer.

### CLI Layer

New `--stream` option on `coder-eval run`:
- `--stream full` or `--stream=full` or `-s full`: Show all events (debugging mode)
- `--stream minimal` or `--stream=minimal` or `-s minimal`: Show only TurnStart, TurnComplete, CriteriaCheck (monitoring mode)
- Default (no flag): Existing tqdm progress bar behavior

Note: `--stream` always requires an explicit value (`full` or `minimal`). Validated via `click.Choice`.

When `--stream` is active, tqdm is suppressed. Output goes to stderr.

## Batch Mode Thread-Safety

Tasks run as concurrent asyncio coroutines (not threads). Safety measures:
- **Shared renderer with task-ID scoping**: The CLI creates a single `RichStreamRenderer` instance shared across all tasks. The `TaskScopedCallback` wrapper ensures each task's events carry the correct `task_id`, which the renderer uses for prefixing.
- **Task-ID prefixed output**: Every line is prefixed with `[task_id]` in batch mode
- **Lock-protected writes**: `RichStreamRenderer` uses `threading.Lock` for atomic console writes
- **Single-task mode**: No interleaving — clean sequential output

## Rich Terminal Renderer

`coder_eval/streaming/renderers.py` — `RichStreamRenderer`:

| Event | Display |
|-------|---------|
| TurnStart | `--- Iteration 1/3 ---` (bold, with task_id in batch) |
| ToolCall | `>>> TOOL: Bash \| {"command": "python ..."}` (params truncated 120 chars) |
| ToolResult | `<<< OK (42 chars)` or `<<< ERROR: file not found...` (truncated 200 chars) |
| TextChunk | Agent text output (dim styling) |
| TurnComplete | `--- Turn complete: 5 commands, 12.3s, 1.2k tokens ---` |
| CriteriaCheck | `Criteria: 3/4 passed (score: 0.875)` with details |

## File Structure

```
coder_eval/streaming/       # NEW subpackage
├── __init__.py              # Public exports
├── events.py                # StreamEvent dataclass hierarchy
├── callbacks.py             # StreamCallback protocol
└── renderers.py             # RichStreamRenderer

# Modified files:
coder_eval/agent.py                    # Add stream_callback param
coder_eval/agents/claude_code_agent.py # Emit events in stream loop
coder_eval/orchestrator.py             # Accept/propagate callback, emit events
coder_eval/orchestration/batch.py      # Accept callback factory
coder_eval/cli/run_command.py          # --stream flag, renderer wiring
```

## Error Handling

- **Callback exceptions**: Caught and logged, never crash evaluation
- **Large output**: Params truncated to 120 chars, results to 200 chars (configurable)
- **No TTY**: Rich degrades to plain text automatically
- **Testing**: `CollectingCallback` helper collects events into a list for assertions

## Approach Alternatives Considered

1. **Async Event Bus (Queue-based)**: True producer/consumer decoupling via `asyncio.Queue`. More complex — adds queue management, consumer lifecycle, and backpressure concerns. Overkill for a display feature.

2. **Enhanced Logging + Rich Live**: Extend existing `_log_message_debug()` to emit INFO-level structured log records, rendered by Rich Live display. Minimal new code but mixes UI concerns into logging. Batch interleaving is messy with logging handlers.

The callback approach was chosen for its clean separation of concerns, natural fit with existing patterns (batch callbacks already exist), and simplicity compared to the event bus.
