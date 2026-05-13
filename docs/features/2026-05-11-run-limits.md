# Run Limits — Unified Run-time Caps

Per-task caps that bound an evaluation along four dimensions: agent
turns (`max_turns`), wall-clock (`task_timeout` / `turn_timeout`),
cumulative subject-agent tokens, and USD cost. One conceptual block —
`run_limits:` — covers all of them.

## What it does

When a task declares a `run_limits:` block, the orchestrator enforces
caps in two ways:

- **Structural caps** (`max_turns`, `task_timeout`, `turn_timeout`)
  bound the loop shape and wall-clock. Breaches surface as
  `FinalStatus.MAX_TURNS_EXHAUSTED` / `FinalStatus.TIMEOUT`.
- **Budget caps** (`max_input_tokens`, `max_output_tokens`,
  `max_total_tokens`, `max_usd`) are checked **after each completed
  turn**. A breach aborts the task with one of two new final statuses:
  - `FinalStatus.TOKEN_BUDGET_EXCEEDED` — token cap tripped.
  - `FinalStatus.COST_BUDGET_EXCEEDED` — cost cap tripped.

The budget statuses fall through to `FinalStatus.category == "failed"`,
so they roll up into `tasks_failed` for backward-compatible reporting.
Informational sub-counters (`tasks_token_budget_exceeded`,
`tasks_cost_budget_exceeded`) on `RunSummary` and `VariantAggregate`
let consumers separate budget breaches from criterion failures without
post-hoc parsing.

## How to configure

The block can live at any of four layers and follows the standard
5-layer precedence chain:

```
default experiment defaults → experiment defaults → task → variant → CLI
```

### Migration (2026-05-12)

`max_turns`, `task_timeout`, and `turn_timeout` previously lived at
the top level of `TaskDefinition`, `ExperimentDefaults`, and
`ExperimentVariant`. They now live inside the `run_limits` block.
A deprecation shim accepts the old shapes until **2026-05-20**:

- top-level `max_turns:` / `task_timeout:` / `turn_timeout:` on a task
  → hoisted into `run_limits.<field>` with a `DeprecationWarning`.
- legacy `agent.max_turns:` / `agent.turn_timeout:` → hoisted into
  `run_limits.<field>` with a `DeprecationWarning`.
- `max_iterations:` / `llm_reviewer:` (removed in PR #191) are silently
  dropped with a `DeprecationWarning` so external tasks still load.

Canonical location is `run_limits.<field>`.

### Field-merge semantics

The merge is **field-wise**: each layer contributes a partial dict;
later layers overwrite specific keys, leaving keys they don't mention
intact. A variant that sets `run_limits: {max_usd: 1.0}` overrides
only `max_usd` — the task's `max_turns` and `task_timeout` survive.
(This is a behavioral change from the original PR #238 design, which
did whole-object replace.)

### CLI overrides

The structural caps additionally accept CLI flags:
`--max-turns` / `--task-timeout` / `--turn-timeout`. They patch into
`run_limits.*` via the same field merge. Budget caps (tokens, USD)
remain YAML-only by design.

### Task-level

```yaml
# tasks/my_task.yaml
task_id: my_task
# ...
run_limits:
  max_turns: 20              # structural: inner-loop turn cap
  task_timeout: 600          # structural: wall-clock cap (seconds)
  turn_timeout: 300          # structural: per-turn cap (seconds)
  max_input_tokens: 50000
  max_output_tokens: 10000
  max_total_tokens: 60000   # cumulative cap (input + output)
  max_usd: 0.20             # requires per-turn cost reporting
  count_cached_input: false # default: cached reads don't count
```

### Experiment defaults

```yaml
# experiments/my_experiment.yaml
defaults:
  run_limits:
    max_turns: 20
    task_timeout: 600
    turn_timeout: 300
    max_usd: 1.0   # ceiling for every task in this experiment
```

### Per-variant override (field merge)

```yaml
# experiments/my_experiment.yaml
variants:
  - variant_id: opus
    run_limits:
      max_usd: 5.0  # opus gets a higher cap; other keys inherit from the task
```

All `run_limits` fields are optional. An empty block (`run_limits: {}`)
is now legal and produces a `RunLimits()` with all-None fields.

## Enforcement boundary

Token / cost caps are checked **between turns**, not mid-turn. The
Claude Code SDK does not expose per-tool-call token deltas, so a
single very long turn can still exceed the budget before the check
fires. For single-shot tasks this means the limit catches overruns
**after** the only turn has finished; pair the budget caps with
`run_limits.turn_timeout` and `run_limits.max_turns` for hard upper
bounds on wall-clock and turn count — both live in the same block.

Mid-turn preemption is out of scope.

## Cost source and the `cost_data_available` flag

Cost is read from `TurnRecord.token_usage.total_cost_usd`. This is
populated when:

- **Proxy mode**: the local LLM Gateway proxy computes cost from token
  counts and per-model pricing.
- **Direct/Bedrock mode**: depends on whether the SDK reports cost in
  its `ResultMessage`. Currently not all routes report cost — when no
  turn produces a `total_cost_usd`, a `max_usd` budget cannot be
  checked.

When this happens, the orchestrator:

1. Logs a single warning per task: `"max_usd budget configured but no
   turn reported cost; skipping cost check"`.
2. Sets `result.environment_info["cost_data_available"] = False`.
3. **Skips** the cost check (does NOT fail the task).
4. Token budgets are independent and still enforced.

When cost data IS available, the flag is set to `True`. The key is
absent entirely when no `max_usd` budget is configured, to avoid noise.

Use the flag to audit whether a budget was actually enforceable:

```bash
jq '.environment_info.cost_data_available' run_dir/task.json
```

## Per-row dataset semantics

Dataset fan-out happens **before** variant resolution, so each
row-task carries its own copy of `run_limits`. A 100-row task with
`max_usd: 0.10` permits up to **\$10** of cumulative experiment
spend. Plan accordingly — there is no experiment-wide cumulative cap.

## Interaction with criteria

Criteria still run and record `success_criteria_results` when a budget
trips, for partial-credit visibility. In single-shot mode the budget
check fires **after** the criteria block. In simulation mode the
dialog loop forces an end-of-dialog criteria check before re-raising
`BudgetExceededError`, even when the breach happens mid-dialog.

## Interaction with `SimulationConfig.max_total_tokens`

These are independent:

- `SimulationConfig.max_total_tokens` is a dialog-internal cap on the
  sum of simulator + subject tokens. When exceeded, the dialog
  terminates gracefully via `DialogStopReason.BUDGET` and the task can
  still succeed.
- `run_limits` is a hard task abort on subject-agent tokens/cost only.
  Triggers `DialogStopReason.RUN_LIMIT_EXCEEDED` and the dedicated
  `TOKEN_BUDGET_EXCEEDED` / `COST_BUDGET_EXCEEDED` `FinalStatus` values.

Both can be set on the same task.

## How to read it back

Per-task: `task.json`

- `final_status`: one of `TOKEN_BUDGET_EXCEEDED` / `COST_BUDGET_EXCEEDED`.
- `error_message`: e.g. `"input_tokens budget exceeded: 2100 > 1000 (iteration 1)"`.
- `error_details`: structured context with `component` set to
  `orchestrator.run_limits.tokens` or `orchestrator.run_limits.cost`.
- `task_config.lineage["run_limits"]`: records which layer (default /
  experiment-defaults / task / variant) supplied the active block.
- `environment_info["cost_data_available"]`: `True` / `False` when a
  `max_usd` budget was configured (absent otherwise).
- `simulation.stop_reason`: `"run_limit_exceeded"` for simulation
  tasks that aborted on a budget breach.

Per-run: `run.json` and `run.md`

- `tasks_token_budget_exceeded` / `tasks_cost_budget_exceeded`:
  informational sub-counters under `tasks_failed`.
- `run.md` renders a parenthetical `(incl. N token budget, M cost
  budget exceeded)` next to the `Failed` line when non-zero.

Per-variant (experiment runs): `experiment.json`, `experiment.md`,
`variant.html`

- `VariantAggregate` carries the same two sub-counters.
- `experiment.md` adds `- Token budget` / `- Cost budget` sub-rows
  under `Failed` in the cross-variant aggregate table when non-zero.
- `variant.html` adds `Token Budget` / `Cost Budget` stat tiles to the
  summary grid when non-zero.

## Smoke task

`tasks/smoke_budget_exceeded.yaml` (tags: `smoke-fail`) exercises the
feature end-to-end with an unsatisfiable `max_input_tokens: 1`. CI
asserts the task lands in `tasks_failed`, guarding against regressions
that silently disable the budget gate.
