# Design: Experiment Layer for Multi-Run Configs

**Date**: 2026-03-09
**Status**: Implemented
**Requirements**: `docs/requirements/experiment-for-multi-run-configs.md`

> **Terminology note (updated):** this design originally used the word "arm"
> (`ExperimentArm`, `arms`, `name`, `ExperimentBase`/`base`). The shipped code
> renamed these to **variant** (`ExperimentVariant`, `variants`, `variant_id`,
> `ExperimentDefaults`/`defaults`), and the per-run scalar caps
> (`max_iterations`/`task_timeout`/`turn_timeout`) were consolidated into a single
> `run_limits` block. This document has been reconciled to the shipped names. For
> the canonical *user-facing* guide see [docs/AB_EXPERIMENTS.md](../AB_EXPERIMENTS.md).

## Overview

Introduce an **Experiment** concept that allows running the same tasks across multiple configuration variants ("arms") for comparative analysis. The experiment layer sits above the existing single-task pipeline as a pre-processing config resolver — no changes to `Orchestrator` or `run_batch` internals.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Architecture | Pre-processing layer (Approach A) | Keeps experiment concerns above core pipeline; lesson from PR #44 revert |
| CLI override precedence | Highest (above variant) | `--model X` always wins, even over experiment variants |
| List merge strategy | Atomic replace | `allowed_tools: [Read]` in a variant replaces base's `[Read, Write, Bash]` entirely |
| Default experiment | Always applied | Tasks without `agent` work via `experiments/default.yaml` |
| Directory nesting | Always nest | Even default runs: `runs/<ts>/default/<task>/default/` |
| Experiment-level summary | Yes, full | Aggregate win rates, avg scores per variant across all tasks |

## Data Models

### `ExperimentDefinition` (`models/experiment.py`)

```python
class ExperimentVariant(BaseModel):
    variant_id: str                        # e.g. "sonnet", "opus"
    description: str = ""
    agent: dict[str, Any] | None = None    # partial agent overrides
    simulation: dict[str, Any] | None = None
    repeats: int | None = None             # replicate count for this variant
    template_sources: list[TemplateSource] | None = None
    prompt_mutations: list[PromptMutation] | None = None
    initial_prompt: str | None = None      # full prompt replacement
    initial_prompt_file: str | None = None # ... or load it from a file
    run_limits: RunLimits | None = None    # field-merged caps (turns/time/tokens/USD)
    driver: Literal["tempdir", "docker"] | None = None

class ExperimentDefaults(BaseModel):
    repeats: int | None = None
    agent: dict[str, Any] | None = None    # partial agent overrides
    simulation: dict[str, Any] | None = None
    template_sources: list[TemplateSource] | None = None
    prompt_mutations: list[PromptMutation] | None = None
    pre_run: list[PreRunCommand] | None = None
    post_run: list[PostRunCommand] | None = None
    run_limits: RunLimits | None = None
    driver: Literal["tempdir", "docker"] | None = None
    sandbox: SandboxConfig | None = None

class ExperimentDefinition(BaseModel):
    experiment_id: str                     # kebab-case identifier
    description: str = ""
    defaults: ExperimentDefaults | None = None
    variants: list[ExperimentVariant]      # >= 1 variant, unique variant_ids
```

Agent overrides are `dict[str, Any]` (partial dicts) rather than full `AgentConfig` — only specified keys participate in the merge.

### `TaskDefinition` Change

`agent` field becomes optional (`AgentConfig | None = None`). No other changes to `models/tasks.py`.

### `default.yaml` Experiment

```yaml
experiment_id: default
description: "Default experiment - provides baseline agent configuration"

defaults:
  run_limits:
    max_turns: 20
    task_timeout: 600
    turn_timeout: 300
  agent:
    type: claude-code
    permission_mode: acceptEdits
    model: claude-sonnet-4-6
    allowed_tools: ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "Skill"]
    plugins: null
    ignore_patterns: []

variants:
  - variant_id: default
    description: "Default configuration"
```

**Always loaded as the baseline** — the experiment layer is unconditionally used for both `run` and `plan` commands. There is no "non-experiment" code path. All agent properties are listed explicitly so users know the baseline defaults. Tasks without an `agent` section inherit these defaults via the merge chain.

### Result Models (`models/experiment.py`)

```python
class VariantResult(BaseModel):
    variant_id: str
    task_id: str
    weighted_score: float
    final_status: FinalStatus
    duration_seconds: float
    total_tokens: int | None = None
    replicate_index: int = 0               # 0 when no replicates
    replicate_count: int = 1

class VariantAggregate(BaseModel):
    variant_id: str
    tasks_run: int
    tasks_succeeded: int
    tasks_failed: int
    tasks_error: int
    average_score: float
    average_duration: float
    total_tokens: int | None = None
    replicate_count: int = 1

class TaskExperimentSummary(BaseModel):
    task_id: str
    variant_results: list[VariantResult]
    best_variant: str                      # highest weighted_score
    is_tie: bool = False                   # True when multiple variants share highest score
    score_spread: float                    # max - min score

class ExperimentResult(BaseModel):
    experiment_id: str
    description: str
    variant_ids: list[str]
    task_summaries: list[TaskExperimentSummary]
    variant_aggregates: dict[str, VariantAggregate]
    total_duration_seconds: float
    per_replicate_scores: dict[str, dict[str, list[float]]]  # variant_id → task_id → [scores]
```

## Config Resolution

### Precedence Chain (lowest to highest)

```
1. experiments/default.yaml          (baseline defaults)
2. experiment defaults               (experiment-wide defaults)
3. tasks/<task_name>.yaml            (task-specific config)
4. experiment variant                (variant-specific overrides)
5. CLI flags                         (always wins)
```

> Note the ordering: experiment **defaults** sit *below* the task (layer 2),
> while the **variant** sits *above* it (layer 4). A variant always wins over a
> task; experiment-wide defaults never do.

### Merge Algorithm

`resolve_task_for_variant(default_experiment, task, experiment, variant) -> (TaskDefinition, lineage, repeats)`

**For `agent` (dict merge, lists replace):**

1. Start with default experiment's `defaults.agent` dict
2. Overlay experiment's `defaults.agent` dict
3. Overlay task's `agent` dict (task-explicit fields only, via `exclude_unset`)
4. Overlay variant's `agent` dict
5. Construct `AgentConfig` from merged dict

Every `agent` key is shallow-merged except `sdk_options`, which is **deep**-merged
so a higher-priority layer adding one SDK key doesn't wipe keys set below it.

**For `run_limits` and `sandbox`:** field-merge (per-key) rather than block
replacement — a variant setting `run_limits.max_turns` leaves the task's
`task_timeout` intact.

**For list fields** (`allowed_tools`, `plugins`, `ignore_patterns`): atomic replacement — if a later layer specifies a list, it fully replaces the earlier value. `template_sources` are the exception: variant entries are *appended* after the task's base templates.

### Example

```
default.yaml      ->  agent: {type: claude-code, permission_mode: acceptEdits}
experiment defaults ->  agent: {permission_mode: bypassPermissions}
task.yaml         ->  agent: {allowed_tools: [Read, Write, Bash]}
variant "opus"    ->  agent: {model: claude-opus-4-6}

Result            ->  agent: {type: claude-code, permission_mode: bypassPermissions,
                              allowed_tools: [Read, Write, Bash],
                              model: claude-opus-4-6}
```

CLI flags (e.g. `--model X`) are applied after resolution by `_apply_cli_overrides()` (layer 5).

## Orchestration Flow

### `ExperimentRunner` (`orchestration/experiment.py`)

```python
class ExperimentRunner:
    async def run(
        self,
        task_files: list[Path],
        experiment_path: Path,
        batch_config: BatchRunConfig,
    ) -> ExperimentResult:
```

**Flow:**

```
CLI: coder-eval run tasks/**/*.yaml --experiment experiments/model-comparison.yaml
        |
        v
  1. Load experiments/default.yaml (always)
  2. Load experiment YAML
  3. Load all task files
        |
        v
  4. For each (task x variant):
       resolve_task_for_variant(default, task, experiment, variant)
       -> fully-resolved TaskDefinition
       -> per-task dir set to: runs/<ts>/<variant_id>/<task_id>/<NN>/
        |
        v
  5. Pass all resolved tasks to run_batch()
     (existing pipeline, no changes)
        |
        v
  6. Collect results, group by (task_id, variant_id)
  7. Generate task-level cross-variant reports
  8. Generate experiment-level summary
        |
        v
  ExperimentResult
```

The ExperimentRunner is **always in the path** — no branching between experiment and non-experiment mode. The default experiment with a single `default` variant produces equivalent behavior to today.

### CLI Changes (`cli/run_command.py`)

New option:

```
--experiment, -e PATH    Experiment definition YAML (default: experiments/default.yaml)
```

The run command always delegates to `ExperimentRunner` — the legacy `run_batch` code path has been removed. (Subsequent work added `--sample`, `--repeats`, and `--driver`; see [docs/AB_EXPERIMENTS.md](../AB_EXPERIMENTS.md#cli-reference).)

Both `run` and `plan` commands accept optional task file arguments. When no task files are provided, all `.yaml` files under `tasks/` are discovered recursively:

```bash
coder-eval run                              # runs all tasks under tasks/
coder-eval run tasks/hello_date.yaml        # runs specific task
coder-eval plan                             # validates all tasks under tasks/
```

## Output Structure

> **Note:** This section describes the as-shipped layout and filenames. Per-task
> artifacts are nested **variant-first** with a zero-padded replicate sub-dir,
> and there is no `<experiment_id>` path segment — experiment-level reports live
> at the run root.

```
runs/<timestamp>/
  run.md                         # Flat batch execution log (all task x variant)
  run.json                       # Flat batch summary
  experiment.md                  # Experiment-level aggregate across all tasks
  experiment.json                # Experiment-level aggregate data
  experiment.html                # Browsable experiment report
  <variant_id>/
    variant.md                   # Per-variant rollup
    variant.json                 # Per-variant aggregate (VariantAggregate)
    variant.html                 # Browsable per-variant report
    <task_id>/
      <NN>/                       # Replicate index (00, 01, …)
        artifacts/
        task.json                # EvaluationResult for this task x variant x replicate
        task.html                # Browsable per-task trace/report
  latest -> <timestamp>/         # Symlink
```

### Report Content

**Experiment-level `experiment.md` / `experiment.json`:**
- Per-task side-by-side comparison: variant_id, score, status, duration, tokens
- Best/worst variant callout and score spread per task
- Per-variant win/loss counts across all tasks (ties counted separately)
- Summary table: variant_id, tasks succeeded, avg score, avg duration, total tokens
- When `repeats > 1`, `per_replicate_scores` for variance
- Tasks where variants diverged most (highest score spread)

**Per-variant `variant.md` / `variant.json`:**
- Single-variant rollup across all its tasks (aggregate score, tokens, duration)

## Files to Create/Modify

### New Files
- `coder_eval/models/experiment.py` — ExperimentDefinition, ExperimentVariant, ExperimentDefaults, result models (incl. `is_tie`)
- `coder_eval/orchestration/experiment.py` — ExperimentRunner, resolve_task_for_variant(), tie detection
- `coder_eval/reports_experiment.py` — ExperimentReportGenerator (task/experiment reports, win rates with ties)
- `experiments/default.yaml` — default experiment definition with explicit agent defaults

### Modified Files
- `coder_eval/models/__init__.py` — export new experiment models
- `coder_eval/models/tasks.py` — make `agent` optional
- `coder_eval/cli/run_command.py` — add `--experiment` flag, always delegate to ExperimentRunner (removed legacy `_run_legacy` path)
- `coder_eval/cli/run_helpers.py` — add `discover_default_tasks()` for zero-argument CLI invocation
- `coder_eval/cli/plan_command.py` — experiment-aware validation (always loads experiment, no silent fallback)
- `coder_eval/orchestration/batch.py` — add `run_batch_resolved()` for pre-resolved task execution, extract `_apply_cli_overrides()`

### Unchanged
- `coder_eval/orchestrator.py` — no changes
- `coder_eval/evaluation/` — no changes
- `coder_eval/criteria/` — no changes
