# Feature: Multi-Agent Comparison Redesign

**Date**: 2026-03-09
**Status**: Reverting PR #44, pursuing alternative architecture
**PR**: https://github.com/UiPath/coder_eval/pull/44
**Components**: `coder_eval/models/tasks.py`, `coder_eval/orchestration/batch.py`, `coder_eval/orchestration/task_loader.py`

## Context

PR #44 ("feat: support multi-agent comparisons") added the ability to run the same task with
multiple agent configurations side-by-side. It was merged on 2026-03-06 and 7 commits have
landed on top of it since.

We are reverting this approach in favor of a more flexible experimentation architecture.

## What PR #44 Introduced

### Model changes (`models/tasks.py`)

- Added `agents: list[AgentConfig] | None` to `TaskDefinition` alongside the existing `agent` field
- Added `name: str | None` to `AgentConfig` (required when using multi-agent mode)
- Added `validate_agent_config` model validator enforcing mutual exclusivity of `agent` vs `agents`,
  minimum 2 agents, unique names, alphanumeric name pattern, and `__` forbidden in `task_id`

### Task expansion (`orchestration/task_loader.py`)

- Added `expand_task_for_agents()` — flattens a multi-agent `TaskDefinition` into N independent
  single-agent copies sharing the same `task_id` but each with one `AgentConfig`

### Batch pipeline (`orchestration/batch.py`)

- Threaded `agent_name: str | None` through the entire pipeline: task expansion, run directory
  nesting (`run_dir/task_id/agent_name/`), deduplication keyed on `(task_id, agent_name)`,
  error handling, progress callbacks, and `RunSummary` output
- Added warning when CLI agent-level overrides (`--model`, `--permission-mode`, etc.) are silently
  ignored for multi-agent tasks

### Other

- Updated `README.md` with multi-agent YAML syntax and comparison report docs
- Updated `cli/plan_command.py` for multi-agent awareness

## Problems With This Approach

### 1. Model pollution

`TaskDefinition` has two mutually-exclusive fields (`agent` vs `agents`) with a complex validator to
enforce the XOR constraint. Every consumer of `TaskDefinition` must now handle both shapes. The
codebase already uses discriminated unions (criteria, template sources) for variant types, but the
deeper issue is that experimentation concerns are embedded in the task definition itself.

### 2. Tight coupling to batch pipeline

The entire multi-agent logic (expansion, directory nesting, CLI override warnings, deduplication
keyed on `(task_id, agent_name)`) is woven into `batch.py`. Adding new comparison strategies means
modifying this already-complex function (~470 lines).

### 3. Inflexible experimentation

The "expand then run independently" model treats multi-agent as "run N times with different agent
configs." This makes it difficult to:

- Compare agents on different prompts or iteration counts
- Share sandbox state between agents (e.g., one builds, another reviews)
- Define comparison-specific success metrics
- Run asymmetric configurations (agent A gets 5 turns, agent B gets 10)
- Run parameter sweeps (model x temperature x prompt combinations)

### 4. CLI override gap

Agent-level CLI flags (`--model`, `--max-turns`, `--turn-timeout`, etc.) are silently ignored for
multi-agent tasks. This is a UX trap — users set `--model` expecting it to apply globally, but
multi-agent tasks quietly ignore it with only a log warning.

## Proposed Alternative: Experiment Layer

A dedicated experimentation layer that sits *above* the current single-task pipeline.

### Architecture

```
Experiment Definition (YAML)
        │
        ▼
  Experiment Runner        ← new orchestration layer
   ┌────┴────┐
   ▼         ▼
TaskDef A  TaskDef B       ← fully-resolved, single-agent TaskDefinitions
   │         │
   ▼         ▼
Orchestrator Orchestrator  ← existing pipeline, unmodified
   │         │
   ▼         ▼
Result A   Result B
   └────┬────┘
        ▼
 Comparison Reporter       ← side-by-side analysis
```

### Key Design Principles

1. **`TaskDefinition` stays single-agent** — no dual-field ambiguity, no special-case validators.
   One task, one agent, one evaluation. KISS.

2. **Experiment definition is separate** — a new model defines what you're comparing: agent configs,
   prompt variants, parameter sweeps, iteration count variations, etc.

3. **Experiment runner orchestrates** — generates fully-resolved `TaskDefinition` instances from the
   experiment matrix and dispatches them through the existing `Orchestrator` / `run_batch` pipeline.

4. **Comparison reporter is pluggable** — takes N `EvaluationResult`s and generates side-by-side
   analysis. Can evolve independently of the core evaluation pipeline.

5. **CLI overrides apply uniformly** — since experiments produce normal `TaskDefinition`s, CLI flags
   work as expected on every generated task.

### Experiment Definition Sketch

```yaml
experiment_id: compare-models-on-fibonacci
description: Compare Claude Sonnet vs Opus on a simple coding task
base_task: tasks/fibonacci.yaml    # reference task for shared config

matrix:
  agents:
    - name: sonnet-4
      type: claude-code
      model: claude-sonnet-4-20250514
    - name: opus-4
      type: claude-code
      model: claude-opus-4-20250514

  # Optional: vary other dimensions
  # max_iterations: [3, 5]
  # prompts:
  #   - name: minimal
  #     initial_prompt: "Write fibonacci"
  #   - name: detailed
  #     initial_prompt: "Write an efficient fibonacci function with memoization..."

comparison:
  metrics: [weighted_score, duration, token_usage, reference_similarity]
  report_format: markdown
```

### Benefits Over PR #44

| Dimension | PR #44 | Experiment Layer |
|-----------|--------|------------------|
| Task model complexity | Dual fields + XOR validator | Single `agent` field, unchanged |
| Batch pipeline impact | ~100 lines of multi-agent threading | Zero changes to `batch.py` |
| Experimentation flexibility | Agent configs only | Agents, prompts, params, iterations |
| CLI override behavior | Silently ignored for multi-agent | Uniformly applied |
| Comparison reporting | Basic (same summary format) | Dedicated, pluggable |
| Existing test impact | Modified validators and expansion tests | No changes to core tests |

## Revert Plan

Since 7 commits landed on top of PR #44, a clean `git revert` of the merge commit is safest:

1. `git revert -m 1 aa4cac9` (the merge commit for PR #44)
2. Resolve conflicts from subsequent PRs that touched the same files
3. Verify `make verify` passes
4. Open revert PR

### Files affected by revert

- `coder_eval/models/tasks.py` — remove `agents` field, `name` on `AgentConfig`, `validate_agent_config`
- `coder_eval/orchestration/task_loader.py` — remove `expand_task_for_agents()`
- `coder_eval/orchestration/batch.py` — remove `agent_name` threading, simplify tuples back to `(Path, TaskDefinition)`
- `coder_eval/cli/plan_command.py` — revert multi-agent awareness
- `README.md` — remove multi-agent docs
- Tests covering multi-agent paths
