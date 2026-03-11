# Plan: Add Model Name/Version to Run Report

**Related PR:** #9

## Context

When running `coder-eval run tasks/hello_date.yaml`, the generated `run-report.md` shows environment info (CLI version, package versions) but **not which model was used**. This is critical for reproducibility — you can't compare evaluation results without knowing what model produced them.

The SDK's `AssistantMessage` already exposes a `model: str` field (e.g., `"claude-sonnet-4-5-20250514"`), so the data is available but not captured.

## Data Flow (new path in bold)

```
SDK AssistantMessage.model
  -> **TurnRecord.model_used** (captured per-turn)
  -> **EvaluationResult.model_used** (resolved from turns + fallback to AgentConfig.model)
  -> **RunSummary.task_results[].model_used** (new dict key)
  -> **Report header + Task Details table** (rendered in markdown)
```

## Implementation Steps

### 1. Add `model_used` field to `TurnRecord` and `EvaluationResult`
**File**: `coder_eval/models/results.py`

- `TurnRecord`: Add `model_used: str | None = Field(default=None, ...)` after `token_usage`
- `EvaluationResult`: Add `model_used: str | None = Field(default=None, ...)` after `agent_type`

### 2. Capture model from SDK `AssistantMessage` in agent
**File**: `coder_eval/agents/claude_code_agent.py`

- Add `sdk_model_used: str | None = None` variable alongside existing `sdk_result_usage`
- In the `_is_assistant_message(message)` block, capture: `sdk_model_used = getattr(message, "model", None) or sdk_model_used`
- Pass `model_used=sdk_model_used` when constructing the returned `TurnRecord`

### 3. Resolve model in Orchestrator's `run()` finally block
**File**: `coder_eval/orchestrator.py`

After token usage aggregation (after line 181), before saving report:
- Extract `model_used` from the last turn that has it
- Fall back to `self.task.agent.model` if no turn provided one

### 4. Propagate `model_used` into `RunSummary.task_results`
**File**: `coder_eval/orchestration/batch.py`

In `_generate_run_summary()`, add `"model_used": r["result"].model_used` to each task result dict.

### 5. Render model info in the report
**File**: `coder_eval/reports.py` — `generate_markdown()`

- **Header**: After Duration line, show `**Model**: \`model-name\`` (or `**Models**: ...` if multiple)
- **Task Details table**: Add conditional Model column (same pattern as Similarity column)

### 6. Update tests
**File**: `tests/test_reports.py`

- Update `_make_task_result()` helper to accept `model_used`
- Add `test_generate_markdown_with_model_info` — model shown in header + table
- Add `test_generate_markdown_no_model_info` — column omitted when no model data (backward compat)
- Add `test_generate_markdown_multiple_models` — header shows multiple models
- Verify existing backward-compat test still passes with missing `model_used` key

### 7. Verify
```bash
make verify  # format + lint + typecheck + test + coverage
```

## Key Design Decisions

- **Per-turn capture**: `TurnRecord.model_used` stores per-turn model (handles potential fallback model changes)
- **Last-turn resolution**: `EvaluationResult.model_used` uses the last turn's model as most representative
- **Two-tier fallback**: SDK actual model > `AgentConfig.model` > `None`
- **Conditional column**: Model column only shown when data exists (backward compatible with old JSON)
- **Duck typing**: Uses `getattr(message, "model", None)` consistent with existing SDK message handling
