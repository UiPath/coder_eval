# `expected_turns` — soft target on cumulative SDK turns

## What it does

`expected_turns` is a soft sibling of `max_turns`. It sets a target for the
cumulative number of inner-loop turns the SDK reports across **all iterations
of a single task** (including simulation/dialog turns). When the running total
exceeds the configured value, the orchestrator:

- Logs a single warning to `task.log` (one-shot per task — additional turns
  do not produce additional warnings).
- Surfaces a badge / marker in the HTML and Markdown reports.

The run is **not** aborted. `max_turns` remains the hard cap (enforced inside
the SDK). `expected_turns` is purely a logging + reporting signal and is not
used to fail criteria, change `final_status`, or affect `weighted_score`.

## How to configure

Set `run_limits.expected_turns` to a positive integer in any of the layered
configuration sources. The same field-merge logic that applies to `max_turns`
applies here:

1. `experiments/default.yaml` (commented out by default).
2. Experiment defaults block.
3. Task YAML.
4. Variant block.

Example (task YAML):

```yaml
run_limits:
  max_turns: 20         # hard cap — SDK enforces
  expected_turns: 15    # soft target — orchestrator logs + report badges
```

There is **no CLI flag**. Override per-variant or per-task only.

## Relationship to other knobs

| Knob | Axis | Behaviour | Affects score? |
|------|------|-----------|----------------|
| `run_limits.max_turns` | SDK inner-loop turns, per iteration | Hard cap — SDK aborts the agent loop | No (but `max_turns_exhausted` flag is surfaced) |
| `run_limits.expected_turns` | SDK inner-loop turns, cumulative across iterations | Soft warning + report badge | No |
| `commands_efficiency.expected_commands` | Tool-call count, per task | Continuous criterion score | Yes (criterion score) |

`expected_turns` is **not** a criterion — it does not appear in
`success_criteria_results` and does not influence pass/fail.

## Where the signal appears

- **Logs**: search `task.log` for `expected_turns`. A single warning per task
  is emitted when the cumulative sum first exceeds the target.
- **HTML report** (`task.html`): a `expected_turns exceeded (N/M)` badge at
  the run/header level. No per-turn badge — the signal is cumulative across
  iterations.
- **Markdown report** (`run.md`): a `## Run-time Notes` section listing each
  task that exceeded its target, alongside `max_turns exhausted` notes for
  parity with the HTML report.
- **Evalboard run-detail table**: a single sortable `Turns` column
  rendering the per-task tool-call count (`actual_commands`) with
  green/yellow/red tint per `actual_commands / expected_turns` ratio. A
  hover tooltip names the configured target. Thresholds are
  env-configurable via `EVALBOARD_TURNS_YELLOW_RATIO` (default `1.25`
  — yellow at +25%) and `EVALBOARD_TURNS_RED_RATIO` (default `1.5` —
  red at +50%). Rows without a target render the bare count untinted.
- **Evalboard task-detail page**: a `Turns` stat (tinted, same
  tooltip) and an adjacent `Expected turns` stat showing the target.
- **Evalboard trends view**: the per-task history sub-table renders
  the same tinted `Turns` column. The per-task summary row carries an
  `Avg turns` aggregate (mean tool-call count over successful runs).
- **`total_turns` field**: persisted into `run.json` for future
  analytics. Not currently surfaced in the evalboard UI —
  `actual_commands` is the easier-to-interpret signal and the one we
  display.
