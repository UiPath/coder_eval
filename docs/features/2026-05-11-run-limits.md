# Run Limits — Token & Cost Budget Enforcement

Per-task budget caps that abort an evaluation when the subject agent's
cumulative token usage or USD cost exceeds a declared limit. Distinct
from `task_timeout` / `turn_timeout` (time-based) and from criteria
(post-hoc scoring) — this is mid-run resource governance.

## What it does

When a task declares a `run_limits:` block, the orchestrator checks
cumulative subject-agent token and cost usage **after each completed
turn**. If any configured cap is exceeded, the task is aborted with one
of two new final statuses:

- `FinalStatus.TOKEN_BUDGET_EXCEEDED` — token cap tripped
  (`max_input_tokens`, `max_output_tokens`, or `max_total_tokens`).
- `FinalStatus.COST_BUDGET_EXCEEDED` — cost cap tripped (`max_usd`).

Both statuses fall through to `FinalStatus.category == "failed"`, so
they roll up into `tasks_failed` for backward-compatible reporting.
Informational sub-counters (`tasks_token_budget_exceeded`,
`tasks_cost_budget_exceeded`) on `RunSummary` and `VariantAggregate`
let consumers separate budget breaches from criterion failures without
post-hoc parsing.

## How to configure

Budgets are **YAML-only** — there are no CLI flag overrides (a
deliberate design decision to avoid CLI bloat). The block can live at
any of three layers and follows the same precedence chain as
`task_timeout`:

```
default experiment defaults → experiment defaults → task → variant
```

The merge is **whole-object replace**, not field-wise — a variant's
`run_limits:` block replaces the task's block in full. Set it once at
the appropriate layer.

### Task-level

```yaml
# tasks/my_task.yaml
task_id: my_task
# ...
run_limits:
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
    max_usd: 1.0   # ceiling for every task in this experiment
```

### Per-variant override

```yaml
# experiments/my_experiment.yaml
variants:
  - variant_id: opus
    run_limits:
      max_usd: 5.0  # opus gets a higher cap (whole-object replace)
```

At least one of the four budget fields (`max_input_tokens`,
`max_output_tokens`, `max_total_tokens`, `max_usd`) is required when
the block is present — an empty `run_limits: {}` is a load-time error.

## Enforcement boundary

Budgets are checked **between turns**, not mid-turn. The Claude Code
SDK does not expose per-tool-call token deltas, so a single very long
turn can still exceed the budget before the check fires. For single-shot
tasks this means the limit catches overruns **after** the only turn has
finished; pair `run_limits` with `turn_timeout` and `max_turns` for
hard upper bounds on wall-clock and turn count.

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
