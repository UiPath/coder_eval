---
description: >-
  Field-level reference for coder_eval's JSON outputs — run.json, task.json,
  variant.json, experiment.json, and suite.json — plus the token/criterion
  telemetry sub-models and FinalStatus values, for anyone consuming a run.
---

# Report Schema

Coder Eval writes machine-readable JSON alongside every markdown/HTML report. This
page is the field-level reference for consumers (dashboards, CI parsers, evalboard
forks). For the on-disk directory tree see
[User Guide → Output Structure](USER_GUIDE.md#output-structure); for how to
re-generate these files see [`coder-eval report` / `aggregate`](USER_GUIDE.md#cli-commands).

All JSON is Pydantic `model_dump_json` output — keys are the model field names
verbatim (no aliases, except `iterations` also accepts the legacy key `turns` on
read). Times are ISO-8601.

## Who writes what

| File | Model | When |
| --- | --- | --- |
| `run.json` / `run.md` | `RunSummary` | Every run (and rebuildable via `coder-eval aggregate`) |
| `<variant>/<task_id>/<NN>/task.json` | `EvaluationResult` | One per replicate |
| `<variant>/<suite_id>/suite.json` / `.md` | `SuiteRollup` | Dataset-backed suites only |
| `experiment.json` / `.md` | `ExperimentResult` | Every run (experiment layer) |
| `<variant>/variant.json` / `.md` | `VariantAggregate` | Per variant |

`<NN>` is the zero-padded replicate index. Judge transcripts spill to sibling files
(e.g. `judge-0.yaml`) referenced by a `transcript_path`.

---

## `run.json` — `RunSummary`

**`run.json` is flat, not a `run → variant → task → replicate` tree.** It is the
run-level summary; full per-replicate detail lives in each `task.json`.

| Key | Type | Meaning |
| --- | --- | --- |
| `run_id` | `str` | Timestamp id, e.g. `"2025-10-09_15-30-45"`. |
| `start_time` / `end_time` | `datetime` | Run window. |
| `total_duration_seconds` | `float` | Wall-clock. |
| `tasks_run` | `int` | Total replicates executed. |
| `tasks_succeeded` / `tasks_failed` / `tasks_error` | `int` | Category counts. **Invariant:** the three sum to `tasks_run`. |
| `tasks_token_budget_exceeded` / `tasks_cost_budget_exceeded` | `int` | Sub-counters of `tasks_failed` (not part of the invariant). |
| `skipped_tasks` | `list[{path, reason}]` | Load failures / `skip: true` opt-outs. |
| `max_parallel` | `int` | Concurrency used. |
| `task_results` | `list[dict]` | Flat per-replicate rows — see below. |
| `framework_version` | `str` | Coder Eval version chip. |
| `environment_info` | `dict` | Version/dependency info (may nest, e.g. `tool_plugins`). |

### `task_results[]` — the flat per-task row

Each entry is an **untyped dict** (a denormalization, not a Pydantic model) with keys
including: `task_id`, `replicate_index`, `variant_id`, `status`
([`FinalStatus`](#finalstatus)), `weighted_score`, `duration`, `iteration_count`,
`tags`, `task_path`, `model_used`, `reference_similarity`, the token buckets
(`input_tokens` = uncached input, `output_tokens`, `cache_creation_input_tokens`,
`cache_read_input_tokens`, `total_tokens`), `total_cost_usd`, `expected_commands`,
`actual_commands`, `commands_efficiency`, `agent_config`, `sdk_options`,
`installed_tools`, turn accounting (`total_turns`, `visible_turns`, `expected_turns`,
`max_turns_exhausted`, `has_final_reply`), and early-stop fields (`stopped_early`,
`early_stop_reason`, `turns_remaining_at_stop`). `iterations` here is a **reduced**
turn digest (`{iteration, duration_seconds, command_count, assistant_turn_count,
crashed, crash_reason}`) — the full transcript is in `task.json`.

---

## `task.json` — `EvaluationResult`

The authoritative per-replicate record.

**Identity/metadata:** `task_id`, `task_description`, `variant_id` (default
`"default"`), `agent_type`, `model_used`, `started_at`, `completed_at`,
`duration_seconds`.

**Results:**

| Key | Type | Meaning |
| --- | --- | --- |
| `final_status` | [`FinalStatus`](#finalstatus) | Terminal status. |
| `weighted_score` | `float \| null` | Weighted average of criterion scores, 0.0–1.0. |
| `max_turns_exhausted` | `bool` | Ran out of turns. |
| `iteration_count` | `int` | Number of turns. |
| `success_criteria_results` | `list[CriterionResult]` | Per-criterion results — see [below](#criterionresult). |

**Transcript:** `iterations: list[TurnRecord]` (accepts legacy alias `turns`) — see
[TurnRecord](#turnrecord).

**Errors** (populated on failure): `error_message`, `error_details`,
`error_log_tail` (carries the Docker build-log tail for `BUILD_FAILED`).

**Config/environment:** `environment_info`, `agent_config`, `sdk_options` (raw
`ClaudeAgentOptions` dump), `sandbox_path`, `task_config`
(`{resolved, source_yaml, source_file, lineage}` — `lineage` maps each field to
`{value, source, source_detail}` so you can trace which config layer set it).

**Telemetry/totals:** `total_token_usage` ([TokenUsage](#tokenusage)),
`command_stats` (`CommandStatistics`), `total_assistant_turns`, `expected_commands` /
`actual_commands` / `commands_efficiency`, `pre_run_results` / `post_run_results`
(`{command, exit_code, stdout, stderr, duration_seconds, error}`), `simulation`
(dialog-mode telemetry, `null` in single-shot), and `early_stop`
([EarlyStopInfo](#earlystopinfo)).

### CriterionResult

A discriminated union on `result_kind` (`basic` / `judge` / `classification`); legacy
files without `result_kind` are inferred from `criterion_type`. Base fields
(`result_kind="basic"`):

`criterion_type`, `description`, `score` (0.0–1.0), `details`, `error`,
`pass_threshold` (default 0.9), `gating` (default `true`; `false` = informational /
weight-0, excluded from the score and the pass/fail gate). The base allows extra
fields so subclass keys round-trip.

- **`classification`** adds `observed_label`, `expected_label` (sentinels like
  `(none)` / `(other)` allowed). Emitted by `classification_match`, `skill_triggered`.
- **`judge`** adds `findings`, `token_usage` (kept distinct from the agent total), and
  `transcript_path` (a sibling `judge-N.yaml`). The full `transcript` is **stripped
  from `task.json`** — read it from the referenced file. Emitted by `llm_judge`,
  `agent_judge`.

### TurnRecord

`iteration`, `user_input`, `agent_output`, `commands` (`list[CommandTelemetry]`),
`timestamp`, `duration_seconds`, `token_usage`, `model_used`, `assistant_turn_count`,
`messages` (`list[TranscriptMessage]`, discriminated on `role`:
`user`/`assistant`/`reconciliation`), `num_turns`, `max_turns_exhausted`,
`result_summary` (`{is_error, subtype, stop_reason, result}`), `crashed`,
`crash_reason`.

> **Token invariant.** Summing the four token buckets across `messages`
> (assistant + the synthetic `reconciliation` entry) equals `token_usage` exactly.
> The `reconciliation` message carries the residual the per-message stream
> under-reports; it has no cost and is excluded from turn/generation counts. See the
> [Claude Code guide](agents/CLAUDE_CODE.md#telemetry).

### EarlyStopInfo

Present (non-`null`) iff the run stopped early — there is no separate boolean.
Fields: `reason` (`criterion_passed` / `criterion_failed`),
`deciding_criterion_type`, `deciding_criterion_description`, `armed_criteria`,
`sdk_turn_index`, `tool_call_index` (1-based, includes the in-flight call),
`elapsed_seconds`, `turns_remaining_at_stop`.

---

## `variant.json` — `VariantAggregate`

A single aggregate (not wrapped): `variant_id`, `tasks_run`, `tasks_succeeded`,
`tasks_failed`, `tasks_error` (same sum-to-`tasks_run` invariant), `average_score`,
`average_duration`, `total_tokens`, `replicate_count`, `tasks_token_budget_exceeded`,
`tasks_cost_budget_exceeded`.

## `experiment.json` — `ExperimentResult`

The cross-variant summary:

- `experiment_id`, `description`, `variant_ids`.
- `task_summaries: list[TaskExperimentSummary]` — each
  `{task_id, variant_results, best_variant, is_tie, score_spread, replicate_count}`,
  where each `VariantResult` carries `{variant_id, task_id, weighted_score,
  final_status, duration_seconds, total_tokens, iteration_count,
  total_assistant_turns, reference_similarity, replicate_index, replicate_count}`.
- `variant_aggregates: dict[str, VariantAggregate]` — keyed by variant id.
- `total_duration_seconds`.
- `per_replicate_scores: dict[variant_id -> dict[task_id -> list[float]]]`.

> **Statistics are render-time only.** Bootstrap/Wilson confidence intervals and the
> Welch/paired mean-difference tests appear in `experiment.md` / HTML but are **not**
> serialized into `experiment.json`. A consumer that wants CIs must recompute them
> from `per_replicate_scores`.

## `suite.json` — `SuiteRollup`

Written for dataset-backed suites; its `passed` flag drives the CI exit code.

| Key | Type | Meaning |
| --- | --- | --- |
| `suite_id` / `variant_id` | `str` | Identity. |
| `rows_total` / `rows_passed` / `rows_failed` / `rows_error` | `int` | Row counts. |
| `pass_rate` | `float` | `rows_passed / rows_total`. |
| `average_weighted_score` | `float \| null` | Mean row score. |
| `criterion_stats` | `list[{criterion_type, rows_evaluated, average_score, error_count}]` | Per-criterion summary. |
| `failed_samples` | `list[FailedRowSummary]` | Capped at 20 (`{row_id, task_id, final_status, weighted_score, failure_reasons, error_message, task_json_relpath, replicate_index}`). |
| `criterion_aggregates` | `list[CriterionAggregate]` | The thresholdable metrics — see below. |
| `passed` | `bool` | All aggregates met their thresholds. |

### CriterionAggregate & ThresholdCheck

`CriterionAggregate`: `criterion_type`, `description`, `rows_total`, `rows_excluded`,
`metrics` (a **flat** `dict[str, float]`, e.g. `accuracy`, `macro_f1`,
`precision.yes`, `recall.yes`, `f1.yes`), `threshold_checks`, `passed`, `details`
(untyped render extras — for classification: `labels`, `per_label`, `confusion`),
`error`.

`ThresholdCheck`: `metric`, `min_value`, `actual_value` (`null` if the aggregator
didn't emit that metric), `passed` (`actual_value >= min_value`). These correspond to
the `suite_thresholds` you set on a criterion — see
[User Guide → Suite Thresholds](USER_GUIDE.md#suite-thresholds--classification-metrics).

---

## TokenUsage

Serialized fields: `uncached_input_tokens`, `output_tokens`,
`cache_creation_input_tokens`, `cache_read_input_tokens`, `total_cost_usd`, plus a
computed **`input_tokens`** (= sum of the three input buckets). Note: `total_tokens`
is a plain property and is **not** serialized. Cost bills `uncached_input_tokens`,
not `input_tokens`. Legacy files that only have `input_tokens` are adopted as
`uncached_input_tokens` on read.

## FinalStatus

String enum values and their reporting category:

| Value | Category | Icon |
| --- | --- | --- |
| `SUCCESS` | succeeded | `+` |
| `FAILURE` | failed | `-` |
| `TIMEOUT` | failed | `T` |
| `MAX_TURNS_EXHAUSTED` | failed | `M` |
| `TOKEN_BUDGET_EXCEEDED` | failed | `#` |
| `COST_BUDGET_EXCEEDED` | failed | `$` |
| `ERROR` | error | `!` |
| `BUILD_FAILED` | error | `B` |

> **Gotcha:** `BUILD_FAILED` (a failed Docker image build) categorizes as **error**,
> not failed — easy to miscount downstream.

---

## Consumer gotchas at a glance

- `run.json.task_results` is a flat, untyped denormalization — the typed source of
  truth is each `task.json`.
- Experiment CIs / significance tests are **not** in `experiment.json` (render-time
  only); recompute from `per_replicate_scores`.
- Judge `transcript` is stripped from `task.json`; follow `transcript_path`.
- `TokenUsage.total_tokens` is not serialized; sum the buckets (or use the computed
  `input_tokens` + `output_tokens` + cache buckets).
- `EarlyStopInfo` presence is itself the "stopped early" signal.

## See also

- [User Guide → Output Structure](USER_GUIDE.md#output-structure) and the
  [`aggregate`](USER_GUIDE.md#cli-commands) command
- [A/B Experiments → Reading the Report](AB_EXPERIMENTS.md#reading-the-report)
- [Task Definition Guide](TASK_DEFINITION_GUIDE.md)
