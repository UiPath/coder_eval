# Threaded Timeout Watchdog

**Related PR:** (follow-up to #160 "hard-kill CLI subprocess when turn/task timeout fires")

## What this feature does

`coder_eval` enforces `turn_timeout` (per-iteration) and `task_timeout` (whole task) via an **OS-thread timer** that SIGKILLs the Claude CLI subprocess at the deadline. The timer runs on a daemon `threading.Timer`, so it fires on wall-clock time regardless of what the asyncio event loop is doing — immune to event-loop starvation and to the `anyio` cancel-scope suppression that made previous `asyncio.wait_for` / `asyncio.sleep`-based watchdogs unreliable.

## How to configure

Both timeouts are resolved through the existing 5-layer merge chain (`default.yaml` → `experiment defaults` → `task YAML` → `variant` → CLI flags). No new fields, no new CLI flags.

| Setting | Location | Default | Description |
|---|---|---|---|
| `turn_timeout` | `agent.turn_timeout` in task YAML or experiment defaults | `300` (e2e) / `None` (otherwise) | Max seconds for one `agent.communicate()` call. |
| `task_timeout` | Top-level `task_timeout` in task YAML or experiment defaults | `600` / `1200` (varies) | Max seconds for the entire evaluation loop. |
| `--turn-timeout` | CLI override | — | Applies to all tasks in the run. |
| `--task-timeout` | CLI override | — | Applies to all tasks in the run. |

`None` disables that layer (no watchdog is started). `<= 0` is treated as disabled too.

## Where it fits in the evaluation flow

```
orchestrator.run():
  with ThreadedWatchdog(task_timeout, on_timeout=agent.kill_sync, ...) as wd:
    await _evaluation_loop()           # <-- Layer 2 (task, OS-thread timer)
  if wd.fired: raise TaskTimeoutError  # <-- belt-and-suspenders post-loop check

_evaluation_loop():
  await agent.communicate(..., timeout=turn_timeout)
                                       # <-- Layer 1 enforced inside the agent

claude_code_agent.communicate():
  with ThreadedWatchdog(turn_timeout, on_timeout=_kill_transport, ...):
    async for message in query(...):   # <-- SDK subprocess, may block on I/O
      ...
```

Both watchdogs:
1. Start a `threading.Timer(timeout, _fire)`.
2. On fire, SIGKILL the captured subprocess via `os.kill(pid, SIGKILL)`.
3. Also deliver `asyncio.Task.cancel()` via `loop.call_soon_threadsafe` so the running coroutine unwinds (useful for mocks with no real subprocess).

## Expected behaviour on timeout

**Per-turn timeout** (`agent.turn_timeout` exceeded):
- Claude CLI subprocess receives SIGKILL (`pid=<N>` logged).
- SDK raises `ProcessError(exit_code=-9)` which the agent classifies as a timeout.
- `TurnTimeoutError` propagates up; categorised as `AGENT_TIMEOUT` (non-retryable).
- Orchestrator final status: `ERROR` with the turn-timeout error message.

**Per-task timeout** (`task_timeout` exceeded):
- Agent subprocess SIGKILLed (if an agent is in flight).
- Running orchestrator task receives `asyncio.CancelledError`.
- `TaskTimeoutError` raised with `elapsed_seconds` populated.
- Orchestrator final status: `TIMEOUT`.

## Troubleshooting

Look in `task.log` for lines from `coder_eval.agents.watchdog`:

```
[WARNING] coder_eval.agents.watchdog: Turn timeout (1200s) fired after 1200s — hard-killing subprocess
[WARNING] coder_eval.agents.claude_code_agent: Hard-killing Claude CLI subprocess (pid=12345)
```

If a task exceeded its deadline but no watchdog line appears, the subprocess likely exited on its own first (the watchdog's `__exit__` cancels the timer on clean exit). Check `duration_seconds` in `task.json` — genuine timeouts land within a few seconds of the configured deadline.

## Why threads instead of asyncio

The Claude Agent SDK uses `anyio.create_task_group` internally. In Python 3.11+, `asyncio.wait_for` cancels the inner task and waits for it to unwind — but anyio cancel scopes can absorb `CancelledError`, so `wait_for` never raises `TimeoutError` and the watchdog never fires. An `asyncio.sleep(timeout)` watchdog is subject to the same event-loop starvation during long rate-limited API calls (observed 15+ minute gaps in a real 3-hour flow-task run where the 1200s watchdog never logged).

A `threading.Timer` runs on its own OS thread: the timer fires on wall-clock time, independent of the asyncio loop. SIGKILL on the CLI subprocess PID forces the SDK's I/O to unblock, which lets the async generator unwind cleanly.

## Implementation pointers

- `src/coder_eval/agents/watchdog.py` — the `ThreadedWatchdog` context manager (self-contained, ~100 lines, no new deps).
- `src/coder_eval/agents/claude_code_agent.py` — per-turn watchdog wraps the `async for message in query(...)` loop inside `communicate()`.
- `src/coder_eval/orchestrator.py` — per-task watchdog wraps `await self._evaluation_loop()` inside `Orchestrator.run()`.
- `src/coder_eval/agent.py` — `Agent.kill_sync()` default no-op; concrete agents override to SIGKILL their subprocess by PID.
