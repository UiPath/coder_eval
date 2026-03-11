# Implementation Plan: Task & Turn Timeouts

**Related PR:** #27

## Problem Statement

The codebase has **three critical hang points** with no timeout protection:

1. **SDK query stream** (`claude_code_agent.py`) — `async for message in query(...)` can block forever if the CLI stalls or streams indefinitely.
2. **Evaluation loop** (`orchestrator.py:306-402`) — No per-turn timeout wrapping `agent.communicate()`, so a single iteration can hang the entire task.
3. **Batch execution** (`batch.py:154`) — `asyncio.gather()` waits forever on hung tasks, blocking semaphore slots and starving other tasks.

Existing timeout coverage is limited to subprocess-level (`sandbox.run_command`) and per-criterion timeouts — these don't protect the agent communication or overall task lifecycle.

All async code already propagates `asyncio.CancelledError` correctly, so wrapping with `asyncio.wait_for()` will trigger clean cancellation through the existing error handling.

---

## Design Principles

- **Layered timeouts**: Turn timeout < Task timeout. Inner timeout fires first with a specific error; outer timeout is a safety net.
- **Configurable at every level**: Task YAML, CLI flags, and `.env` all supported, following existing precedence (CLI > .env > task YAML).
- **Graceful degradation**: Timeouts produce structured errors with context (which layer fired, elapsed time, iteration number), not raw `CancelledError`.
- **Opt-in**: All timeouts default to `None` (disabled). Enable per-task in YAML or globally via CLI flags.

---

## Architecture

```
CLI (--task-timeout, --turn-timeout)
  |
  v
BatchRunConfig --- override --> TaskDefinition.task_timeout
                                AgentConfig.turn_timeout
  |
  v
orchestrator.run():
  asyncio.wait_for(_evaluation_loop(), task_timeout)   <-- Layer 2 (task)
    |
    v
  _evaluation_loop():
    asyncio.wait_for(agent.communicate(), turn_timeout) <-- Layer 1 (turn)
      |
      v
    claude_code_agent.py: async for message in query(...)  <-- Cancelled by Layer 1
```

Both layers live inside `orchestrator.py`. Task timeout wraps only the evaluation loop (not setup/cleanup), so the orchestrator can set `final_status` and run cleanup in its `finally` block regardless.

---

## Step 1: Custom Timeout Exceptions

**File**: `coder_eval/errors/timeout.py` (new)

```python
"""Timeout exceptions for evaluation lifecycle."""


class EvaluationTimeoutError(Exception):
    """Base timeout error for evaluation lifecycle.

    Wraps asyncio.TimeoutError with structured context about
    what timed out and where.
    """

    def __init__(
        self,
        message: str,
        *,
        timeout_seconds: float,
        layer: str,  # "turn" | "task"
        task_id: str | None = None,
        iteration: int | None = None,
        elapsed_seconds: float | None = None,
    ):
        super().__init__(message)
        self.timeout_seconds = timeout_seconds
        self.layer = layer
        self.task_id = task_id
        self.iteration = iteration
        self.elapsed_seconds = elapsed_seconds


class TurnTimeoutError(EvaluationTimeoutError):
    """Agent turn (communicate) exceeded its time limit."""

    def __init__(self, timeout_seconds: float, *, task_id: str | None = None, iteration: int | None = None):
        super().__init__(
            f"Agent turn timed out after {timeout_seconds}s (iteration {iteration})",
            timeout_seconds=timeout_seconds,
            layer="turn",
            task_id=task_id,
            iteration=iteration,
        )


class TaskTimeoutError(EvaluationTimeoutError):
    """Overall task evaluation loop exceeded its time limit."""

    def __init__(self, timeout_seconds: float, *, task_id: str | None = None, elapsed_seconds: float | None = None):
        super().__init__(
            f"Task timed out after {timeout_seconds}s",
            timeout_seconds=timeout_seconds,
            layer="task",
            task_id=task_id,
            elapsed_seconds=elapsed_seconds,
        )
```

**Why custom exceptions?**
- `asyncio.TimeoutError` is generic — can't distinguish turn vs. task timeout.
- Structured fields enable better error categorization, reporting, and retry decisions.
- Inherits from `Exception` (not `TimeoutError`) so it won't collide with the existing `isinstance(error, TimeoutError)` check in `categorize_error`.
- The orchestrator's existing `except Exception` block catches these and populates `error_details`.

---

## Step 2: Model Changes

### 2a. `coder_eval/models/tasks.py` — Add `turn_timeout` to `AgentConfig`

```python
class AgentConfig(BaseModel):
    # ... existing fields ...
    turn_timeout: int | None = Field(
        default=None,
        ge=10,
        description="Maximum seconds per agent turn (communicate call). None = no limit.",
    )
```

**Why on `AgentConfig`?** Turn timeout is an agent-level concern — how long we wait for the agent to respond per iteration.

### 2b. `coder_eval/models/tasks.py` — Add `task_timeout` to `TaskDefinition`

```python
class TaskDefinition(BaseModel):
    # ... existing fields ...
    task_timeout: int | None = Field(
        default=None,
        ge=30,
        description="Maximum seconds for the entire evaluation loop (all iterations). None = no limit.",
    )
```

**Why on `TaskDefinition`?** Task timeout covers the full evaluation loop (all iterations + criterion checks). It's a task-level policy.

### 2c. `coder_eval/orchestration/config.py` — Add overrides to `BatchRunConfig`

```python
class BatchRunConfig(BaseModel):
    # ... existing fields ...

    # Timeout overrides (CLI > task YAML)
    task_timeout: int | None = Field(
        default=None, description="Override task timeout for all tasks"
    )
    turn_timeout: int | None = Field(
        default=None, description="Override turn timeout for all tasks"
    )
```

---

## Step 3: Turn-Level Timeout (Layer 1)

**File**: `coder_eval/orchestrator.py`, method `_evaluation_loop()`

**Current code** (lines ~316-328):
```python
agent = self.agent
turn_record = await execute_with_retry(
    operation=lambda prompt=prompt_with_cwd, a=agent: a.communicate(prompt),
    operation_name=f"Agent communication (iteration {iteration})",
    context={...},
)
```

**New code**:
```python
agent = self.agent
turn_timeout = self.task.agent.turn_timeout

async def _communicate_with_timeout(prompt: str = prompt_with_cwd, a: Agent = agent) -> TurnRecord:
    if turn_timeout is not None:
        try:
            return await asyncio.wait_for(a.communicate(prompt), timeout=turn_timeout)
        except asyncio.TimeoutError:
            raise TurnTimeoutError(
                turn_timeout,
                task_id=self.task.task_id,
                iteration=iteration,
            )
    return await a.communicate(prompt)

turn_record = await execute_with_retry(
    operation=_communicate_with_timeout,
    operation_name=f"Agent communication (iteration {iteration})",
    context={
        "task_id": self.task.task_id,
        "component": "agent",
        "agent_name": self.task.agent.type.value,
    },
)
```

Add import at top of file:
```python
from .errors.timeout import TurnTimeoutError, TaskTimeoutError
```

**Key details**:
- `asyncio.wait_for()` cancels the `communicate()` coroutine on timeout, which cancels the SDK `query()` async generator.
- The `TurnTimeoutError` wraps the raw `TimeoutError` with context.
- `execute_with_retry` will catch `TurnTimeoutError` as an `Exception`. The error categorizer (Step 5) classifies it as `AGENT_TIMEOUT` (non-retryable, `max_retries=0`), so `execute_with_retry` will re-raise immediately — no wasted retries.
- When `turn_timeout is None`, no overhead is added.

---

## Step 4: Task-Level Timeout (Layer 2)

**File**: `coder_eval/orchestrator.py`, method `run()`

Wrap `_evaluation_loop()` inside `run()`. This is better than wrapping in `batch.py` because:
- The orchestrator can set `final_status` = "ERROR" directly.
- The `finally` block always runs (cleanup + report saving).
- No `CancelledError` propagation issues.
- Timeout covers only the evaluation loop, not setup or cleanup.

**Current code** (lines ~116-127):
```python
try:
    await self._setup()
    success = await self._evaluation_loop()
    if success:
        self.result.final_status = "SUCCESS"
    else:
        self.result.final_status = "FAILURE"
```

**New code**:
```python
try:
    await self._setup()

    # Wrap evaluation loop with task-level timeout
    task_timeout = self.task.task_timeout
    if task_timeout is not None:
        try:
            success = await asyncio.wait_for(
                self._evaluation_loop(), timeout=task_timeout
            )
        except asyncio.TimeoutError:
            raise TaskTimeoutError(
                task_timeout,
                task_id=self.task.task_id,
            )
    else:
        success = await self._evaluation_loop()

    if success:
        self.result.final_status = "SUCCESS"
    else:
        self.result.final_status = "FAILURE"
```

**How it works**:
- `asyncio.wait_for()` on timeout cancels `_evaluation_loop()` and raises `asyncio.TimeoutError` to the caller.
- We catch it and re-raise as `TaskTimeoutError` with structured context.
- `TaskTimeoutError` inherits from `Exception`, so the existing `except Exception as e:` block catches it, sets `final_status = "ERROR"`, and populates `error_details`.
- The `finally` block runs cleanup and saves the report with whatever partial results were accumulated.

**Turn timeout inside task timeout**: If a turn timeout fires (Layer 1), it raises `TurnTimeoutError` which is caught by `execute_with_retry` and re-raised (non-retryable). This bubbles up through `_evaluation_loop()` to the `except Exception` handler. The task timeout (Layer 2) is a safety net for when the overall evaluation runs too long even without any single turn timing out.

---

## Step 5: Error Categorization

**File**: `coder_eval/errors/categorization.py`

Add `EvaluationTimeoutError` check before the generic `TimeoutError` check (since `EvaluationTimeoutError` does NOT inherit from `TimeoutError`):

```python
from .timeout import EvaluationTimeoutError

def categorize_error(...):
    # ... existing hint check ...

    # Check our custom timeout exceptions first (before generic TimeoutError)
    if isinstance(error, EvaluationTimeoutError):
        return ErrorCategory.AGENT_TIMEOUT

    # ... rest of existing checks unchanged ...
```

This maps both `TurnTimeoutError` and `TaskTimeoutError` to `AGENT_TIMEOUT`, which has `max_retries=0` in `RETRY_CONFIG` — exactly right, since timeout errors are not transient and retrying would just hit the same timeout.

Add error tip in `coder_eval/errors/categories.py`:

The existing `AGENT_TIMEOUT` entry in `ERROR_TIPS` doesn't exist yet. Add:

```python
ERROR_TIPS = {
    ErrorCategory.AGENT_TIMEOUT: (
        "Agent or task timed out. Consider increasing --turn-timeout or --task-timeout, "
        "or simplifying the task to require fewer iterations."
    ),
    # ... existing entries ...
}
```

---

## Step 6: CLI Flags & Batch Wiring

### 6a. `coder_eval/cli/run_command.py` — Add CLI options

Add to `run_command()` parameters:

```python
task_timeout: int | None = typer.Option(
    None,
    "--task-timeout",
    help="Override task timeout (seconds) for all tasks. Covers the evaluation loop.",
    min=30,
),
turn_timeout: int | None = typer.Option(
    None,
    "--turn-timeout",
    help="Override turn timeout (seconds) for all tasks. Per agent.communicate() call.",
    min=10,
),
```

Pass through to `_run_all_tasks()` and into `BatchRunConfig`:

```python
# In _run_all_tasks() signature, add:
task_timeout: int | None = None,
turn_timeout: int | None = None,

# In BatchRunConfig construction, add:
config = BatchRunConfig(
    # ... existing fields ...
    task_timeout=task_timeout,
    turn_timeout=turn_timeout,
)
```

### 6b. `coder_eval/orchestration/batch.py` — Apply timeout overrides

In `run_batch()`, where CLI overrides are applied to each task (after the existing agent overrides block ~lines 103-116), add:

```python
# Apply timeout overrides (CLI > task YAML)
if config.task_timeout is not None:
    task.task_timeout = config.task_timeout

if config.turn_timeout is not None:
    task.agent.turn_timeout = config.turn_timeout
```

---

## Step 7: Tests

### 7a. `tests/test_timeout_exceptions.py` (new)

Test the custom exceptions:

- `TurnTimeoutError` sets correct `layer="turn"`, `timeout_seconds`, `iteration`, `task_id`
- `TaskTimeoutError` sets correct `layer="task"`, `timeout_seconds`, `task_id`
- Both inherit from `EvaluationTimeoutError`
- Neither inherits from `TimeoutError` (important for categorization logic)
- `str()` produces the expected message

### 7b. `tests/test_timeout_models.py` (new)

Test model field validation:

- `AgentConfig(type="claude-code", turn_timeout=60)` — accepted
- `AgentConfig(type="claude-code", turn_timeout=5)` — rejected (ge=10)
- `AgentConfig(type="claude-code", turn_timeout=None)` — accepted (default)
- `TaskDefinition(..., task_timeout=120)` — accepted
- `TaskDefinition(..., task_timeout=10)` — rejected (ge=30)
- `TaskDefinition(..., task_timeout=None)` — accepted (default)
- `BatchRunConfig(..., task_timeout=300, turn_timeout=60)` — accepted

### 7c. `tests/test_timeout_orchestrator.py` (new)

Test timeout behavior in the orchestrator:

- **Turn timeout fires**: Mock `agent.communicate()` to sleep longer than turn timeout. Verify `TurnTimeoutError` is raised, `final_status="ERROR"`, `error_message` contains "turn timed out".
- **Task timeout fires**: Mock `agent.communicate()` to be slow (but within turn timeout). Set `max_iterations=10`, `task_timeout=1`. Verify `final_status="ERROR"`, `error_message` contains "Task timed out".
- **No timeout (None)**: Verify normal execution when both timeouts are `None`.
- **Turn timeout < task timeout**: Verify turn timeout fires first when a single turn is slow.

### 7d. `tests/test_timeout_categorization.py` (new)

Test error categorization:

- `categorize_error(TurnTimeoutError(...), {"component": "agent"})` → `AGENT_TIMEOUT`
- `categorize_error(TaskTimeoutError(...), {"component": "agent"})` → `AGENT_TIMEOUT`
- `categorize_error(TurnTimeoutError(...), {})` → `AGENT_TIMEOUT` (regardless of component)
- Existing `TimeoutError` behavior unchanged

### 7e. `tests/test_timeout_batch.py` (new)

Test batch override wiring:

- Verify `BatchRunConfig.task_timeout` overrides `task.task_timeout` in `run_batch()`
- Verify `BatchRunConfig.turn_timeout` overrides `task.agent.turn_timeout` in `run_batch()`
- Verify `None` overrides don't clobber task YAML values

---

## Step 8: Verification

```bash
make verify   # format + lint + typecheck + test + coverage
```

Check that:
- [x] All existing tests still pass
- [x] New tests pass
- [x] Coverage >= 80%
- [x] No pyright errors
- [x] No ruff errors

---

## Files Changed Summary

| File | Change |
|------|--------|
| `coder_eval/errors/timeout.py` | **New** — `EvaluationTimeoutError`, `TurnTimeoutError`, `TaskTimeoutError` |
| `coder_eval/models/tasks.py` | Add `turn_timeout` to `AgentConfig`, `task_timeout` to `TaskDefinition` |
| `coder_eval/orchestration/config.py` | Add `task_timeout`, `turn_timeout` to `BatchRunConfig` |
| `coder_eval/orchestrator.py` | Wrap `agent.communicate()` with turn timeout, wrap `_evaluation_loop()` with task timeout |
| `coder_eval/errors/categorization.py` | Add `EvaluationTimeoutError` → `AGENT_TIMEOUT` check |
| `coder_eval/errors/categories.py` | Add `AGENT_TIMEOUT` error tip |
| `coder_eval/cli/run_command.py` | Add `--task-timeout`, `--turn-timeout` CLI flags |
| `coder_eval/orchestration/batch.py` | Apply timeout overrides from `BatchRunConfig` to tasks |
| `tests/test_timeout_exceptions.py` | **New** — Exception unit tests |
| `tests/test_timeout_models.py` | **New** — Model validation tests |
| `tests/test_timeout_orchestrator.py` | **New** — Orchestrator timeout behavior tests |
| `tests/test_timeout_categorization.py` | **New** — Error categorization tests |
| `tests/test_timeout_batch.py` | **New** — Batch override wiring tests |

---

## Task YAML Example

```yaml
task_id: slow-task
description: "Task with timeout protection"
initial_prompt: "Build a web server..."
max_iterations: 5
task_timeout: 600   # 10 minutes for all iterations
agent:
  type: claude-code
  turn_timeout: 120  # 2 minutes per turn
sandbox:
  driver: tempdir
success_criteria:
  - type: file_exists
    file_path: server.py
```

## CLI Usage

```bash
# Override timeouts for all tasks
coder-eval run tasks/*.yaml --task-timeout 600 --turn-timeout 120

# Only turn timeout (no overall limit)
coder-eval run tasks/hard_task.yaml --turn-timeout 300
```
