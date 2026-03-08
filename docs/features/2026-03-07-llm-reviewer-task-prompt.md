# Feature: LLM Reviewer Task-Level Custom Prompt

**Date**: 2026-03-07
**Status**: Bug fix (prompt field silently ignored)
**Components**: `coder_eval/models/tasks.py`, `coder_eval/evaluation/reviewer.py`

## Problem

Task YAML files can specify `llm_reviewer.prompt` with custom review criteria, but the field is
silently dropped during parsing. The reviewer always uses a hardcoded generic prompt regardless
of what the task author configures.

The root cause is two-fold:

1. **Model gap**: `LLMReviewerConfig` does not declare a `prompt` field. Pydantic v2 defaults to
   `extra='ignore'`, so the field is silently discarded during model construction.
2. **Wiring gap**: `_build_review_prompt()` in `reviewer.py` constructs the prompt entirely from
   hardcoded text and never references `self.config` for task-specific instructions.

The task definition guide (`docs/TASK_DEFINITION_GUIDE.md`) already documents the `prompt` field
as a valid option (line 391), making this a documentation-reality mismatch.

## Impact

- **8 task YAML files** have custom `prompt` fields being silently dropped
- Domain-specific review criteria (e.g., "Does it use LangGraph StateGraph?") are lost
- The generic hardcoded prompt can produce feedback that is irrelevant or counterproductive
- Task authors believe their custom criteria are being used when they are not

### Affected Task Files

| Task YAML | Custom Prompt |
|---|---|
| `uipath_calculator_agent.yaml` | 6-point UiPath LangGraph checklist |
| `uipath_classification_agent.yaml` | 6-point UiPath LangChain checklist |
| `uipath_translation_agent.yaml` | 6-point translation agent checklist |
| `uipath_validation_agent.yaml` | 5-point validation checklist |
| `uipath_queue_items.yaml` | 6-point queue items checklist |
| `uipath_retrieve_assets.yaml` | 5-point asset retrieval checklist |
| `uipath_bucket_operations.yaml` | 5-point bucket operations checklist |
| `uipath_process_invocation.yaml` | 5-point process invocation checklist |

## Data Flow (Before Fix)

```
YAML: llm_reviewer.prompt: "Evaluate if..."
  |
  v
LLMReviewerConfig(enabled=True, prompt="Evaluate if...")
  |
  v  Pydantic extra='ignore'
LLMReviewerConfig(enabled=True)          <-- prompt DROPPED
  |
  v
LLMReviewer._build_review_prompt()       <-- hardcoded generic prompt only
  |
  v
"Focus on what's wrong or needs improvement. No praise, no fluff."
```

## Data Flow (After Fix)

```
YAML: llm_reviewer.prompt: "Evaluate if..."
  |
  v
LLMReviewerConfig(enabled=True, prompt="Evaluate if...")
  |
  v  prompt field now declared in model
LLMReviewerConfig(enabled=True, prompt="Evaluate if...")
  |
  v
LLMReviewer._build_review_prompt()       <-- injects task-specific criteria
  |
  v
"TASK-SPECIFIC REVIEW CRITERIA:
 Evaluate if the calculator agent is complete and follows UiPath LangGraph patterns:
 1. Does it use LangGraph StateGraph with proper configuration?
 ..."
```

## Fix Details

### 1. Add `prompt` field to `LLMReviewerConfig` (`models/tasks.py`)

```python
class LLMReviewerConfig(BaseModel):
    enabled: bool = Field(default=False, description="Whether to enable LLM review")
    model: str = Field(
        default="anthropic.claude-3-5-sonnet-20240620-v1:0",
        description="Gateway model name",
    )
    temperature: float = Field(default=0.0, ge=0.0, le=2.0, description="Temperature for LLM sampling")
    max_tokens: int = Field(default=1000, gt=0, description="Maximum tokens in response")
    prompt: str | None = Field(
        default=None,
        description="Task-specific review instructions appended to the review prompt",
    )
```

### 2. Wire prompt into `_build_review_prompt()` (`evaluation/reviewer.py`)

Insert the task-level prompt as a `TASK-SPECIFIC REVIEW CRITERIA` section in the review prompt,
positioned between the reference solution section and the agent output section:

```python
task_criteria_section = ""
if self.config.prompt:
    task_criteria_section = f"""
TASK-SPECIFIC REVIEW CRITERIA:
{self.config.prompt}
Evaluate the agent's work against these criteria in addition to general code quality.

"""
```

The full prompt template then includes `{task_criteria_section}` between `{reference_section}` and
the `AGENT OUTPUT` line.

### 3. Consider `extra='forbid'` on config models (optional hardening)

Adding `model_config = ConfigDict(extra="forbid")` to `LLMReviewerConfig` would prevent this class
of silent-failure bugs. Pydantic would raise `ValidationError` on any unknown field, catching
misconfigurations immediately.

## YAML Usage

```yaml
llm_reviewer:
  enabled: true
  prompt: |
    Evaluate if the calculator agent is complete and follows UiPath LangGraph patterns:

    1. Does it use LangGraph StateGraph with proper configuration?
    2. Are input/output models properly defined with Pydantic BaseModel?
    3. Does it implement a calculate node function?
    4. Does the graph flow from START -> calculate -> END?
    5. Can it successfully perform arithmetic operations?
    6. Is the code simple, clean, and production-ready?
```

When `prompt` is `null` (default), the reviewer uses only the generic review instructions.
When provided, the task-specific criteria are included in the prompt alongside the generic
instructions, giving the LLM both domain context and general review guidance.

## Files Modified

- `coder_eval/models/tasks.py` — Add `prompt` field to `LLMReviewerConfig`
- `coder_eval/evaluation/reviewer.py` — Incorporate `self.config.prompt` into `_build_review_prompt()`
- `tests/` — Add test coverage for custom reviewer prompts

## Related

- Related issue: LLM reviewer feedback diverges from reference solution
