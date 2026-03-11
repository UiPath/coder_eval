# Design: Experiment Layer for Multi-Run Configs

**Date**: 2026-03-09
**Status**: Approved
**Requirements**: `docs/requirements/experiment-for-multi-run-configs.md`

## Overview

Introduce an **Experiment** concept that allows running the same tasks across multiple configuration variants ("arms") for comparative analysis. The experiment layer sits above the existing single-task pipeline as a pre-processing config resolver — no changes to `Orchestrator` or `run_batch` internals.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Architecture | Pre-processing layer (Approach A) | Keeps experiment concerns above core pipeline; lesson from PR #44 revert |
| CLI override precedence | Highest (above arm) | `--model X` always wins, even over experiment arms |
| List merge strategy | Atomic replace | `allowed_tools: [Read]` in arm replaces base's `[Read, Write, Bash]` entirely |
| Default experiment | Always applied | Tasks without `agent` work via `experiments/default.yaml` |
| Directory nesting | Always nest | Even default runs: `runs/<ts>/default/<task>/default/` |
| Experiment-level summary | Yes, full | Aggregate win rates, avg scores per arm across all tasks |

## Data Models

### `ExperimentDefinition` (`models/experiment.py`)

```python
class ExperimentArm(BaseModel):
    name: str                              # e.g. "sonnet", "opus"
    agent: dict[str, Any] | None = None    # partial agent overrides
    max_iterations: int | None = None
    task_timeout: int | None = None
    turn_timeout: int | None = None

class ExperimentBase(BaseModel):
    max_iterations: int | None = None
    task_timeout: int | None = None
    turn_timeout: int | None = None
    agent: dict[str, Any] | None = None    # partial agent overrides

class ExperimentDefinition(BaseModel):
    experiment_id: str                     # kebab-case identifier
    description: str = ""
    base: ExperimentBase | None = None
    arms: list[ExperimentArm]              # >= 1 arm, unique names
```

Agent overrides are `dict[str, Any]` (partial dicts) rather than full `AgentConfig` — only specified keys participate in the merge.

### `TaskDefinition` Change

`agent` field becomes optional (`AgentConfig | None = None`). No other changes to `models/tasks.py`.

### `default.yaml` Experiment

```yaml
experiment_id: default
description: "Default experiment - provides baseline agent configuration"

base:
  max_iterations: 3
  agent:
    type: claude-code
    permission_mode: acceptEdits
    model: claude-sonnet-4-6-20250514
    max_turns: 3
    turn_timeout: 300
    allowed_tools: ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
    plugins: null
    ignore_patterns: []

arms:
  - name: default
```

**Always loaded as the baseline** — the experiment layer is unconditionally used for both `run` and `plan` commands. There is no "non-experiment" code path. All agent properties are listed explicitly so users know the baseline defaults. Tasks without an `agent` section inherit these defaults via the 4-layer merge chain.

### Result Models (`models/experiment.py`)

```python
class ArmResult(BaseModel):
    arm_name: str
    task_id: str
    weighted_score: float
    final_status: str
    duration_seconds: float
    total_tokens: int | None = None

class ArmAggregate(BaseModel):
    arm_name: str
    tasks_run: int
    tasks_succeeded: int
    tasks_failed: int
    average_score: float
    average_duration: float
    total_tokens: int | None = None

class TaskExperimentSummary(BaseModel):
    task_id: str
    arm_results: list[ArmResult]
    best_arm: str                          # highest weighted_score
    is_tie: bool = False                   # True when multiple arms share highest score
    score_spread: float                    # max - min score

class ExperimentResult(BaseModel):
    experiment_id: str
    description: str
    arm_names: list[str]
    task_summaries: list[TaskExperimentSummary]
    arm_aggregates: dict[str, ArmAggregate]
    total_duration_seconds: float
```

## Config Resolution

### Precedence Chain (lowest to highest)

```
1. experiments/default.yaml          (baseline defaults)
2. tasks/<task_name>.yaml            (task-specific config)
3. experiment base                   (experiment-wide overrides)
4. experiment arm                    (arm-specific overrides)
5. CLI flags                         (always wins)
```

### Merge Algorithm

`resolve_task_for_arm(default, task, experiment, arm) -> TaskDefinition`

**For `agent` (dict merge, lists replace):**

1. Start with default experiment's `base.agent` dict
2. Overlay task's `agent` dict — same keys replace, new keys added
3. Overlay experiment's `base.agent` dict
4. Overlay arm's `agent` dict
5. Construct `AgentConfig` from merged dict

**For scalar fields** (`max_iterations`, `task_timeout`, `turn_timeout`, `snapshot_mode`): later non-None values replace earlier values.

**For list fields** (`allowed_tools`, `plugins`, `ignore_patterns`): atomic replacement — if a later layer specifies a list, it fully replaces the earlier value.

### Example

```
default.yaml  ->  agent: {type: claude-code, permission_mode: acceptEdits}
task.yaml     ->  agent: {allowed_tools: [Read, Write, Bash]}
base          ->  agent: {permission_mode: bypassPermissions}
arm "opus"    ->  agent: {model: claude-opus-4-20250514}

Result        ->  agent: {type: claude-code, permission_mode: bypassPermissions,
                          allowed_tools: [Read, Write, Bash],
                          model: claude-opus-4-20250514}
```

CLI flags (e.g. `--model X`) are applied after resolution by the existing `BatchRunConfig` override logic.

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
  4. For each (task x arm):
       resolve_task_for_arm(default, task, experiment, arm)
       -> fully-resolved TaskDefinition
       -> run_dir set to: runs/<ts>/<experiment_id>/<task_id>/<arm_name>/
        |
        v
  5. Pass all resolved tasks to run_batch()
     (existing pipeline, no changes)
        |
        v
  6. Collect results, group by (task_id, arm_name)
  7. Generate task-level cross-arm reports
  8. Generate experiment-level summary
        |
        v
  ExperimentResult
```

The ExperimentRunner is **always in the path** — no branching between experiment and non-experiment mode. The default experiment with a single `default` arm produces equivalent behavior to today.

### CLI Changes (`cli/run_command.py`)

New option:

```
--experiment PATH    Experiment definition YAML (default: experiments/default.yaml)
```

The run command always delegates to `ExperimentRunner` — the legacy `run_batch` code path has been removed.

Both `run` and `plan` commands accept optional task file arguments. When no task files are provided, all `.yaml` files under `tasks/` are discovered recursively:

```bash
coder-eval run                              # runs all tasks under tasks/
coder-eval run tasks/hello_date.yaml        # runs specific task
coder-eval plan                             # validates all tasks under tasks/
```

## Output Structure

```
runs/<timestamp>/
  run-report.md                  # Flat batch execution log (all task x arm)
  run-summary.json               # Flat batch summary
  <experiment_id>/
    <task_id>/
      <arm_name>/
        artifacts/
        arm-report.json          # EvaluationResult (per arm)
        arm.log                  # Per-arm execution log
      task-report.md             # Cross-arm comparison for this task
      task-summary.json          # Cross-arm structured data for this task
    experiment-report.md         # Experiment-level aggregate across all tasks
    experiment-summary.json      # Experiment-level aggregate data
  latest -> <timestamp>/         # Symlink
```

### Report Content

**Per-task `task-report.md`:**
- Side-by-side comparison table: arm name, score, status, duration, tokens
- Best/worst arm callout
- Score spread (how much arms differed)

**Experiment-level `experiment-report.md`:**
- Per-arm win/loss counts across all tasks (ties counted separately)
- Average scores per arm
- Summary table: arm name, tasks succeeded, avg score, avg duration, total tokens
- Tasks where arms diverged most (highest score spread)

## Files to Create/Modify

### New Files
- `coder_eval/models/experiment.py` — ExperimentDefinition, ExperimentArm, ExperimentBase, result models (incl. `is_tie`)
- `coder_eval/orchestration/experiment.py` — ExperimentRunner, resolve_task_for_arm(), tie detection
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
