---
description: >-
  Field-level reference for Coder Eval's JSON outputs — run.json, task.json,
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
(`judge-N.yaml`, or `post-failure-judge-N.yaml` for diagnostic records) referenced
by a `transcript_path`.

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
| `row_selection` | `{split, max_rows, sample_per_stratum} \| null` | Which dataset rows this run selected (`--split` / `--sample` / `--sample-per-stratum`). **Tri-state:** `null` means *not recorded* (a run predating the field), which is NOT the same as an object whose `split` is `null` (no `--split` was passed). Records what was **requested on the command line** — a task's own `dataset.sample_per_stratum` is not reflected here. Unknown keys inside the object are ignored rather than rejected, so a newer writer's fourth selector cannot break an older reader. |
| `task_results` | `list[dict]` | Flat per-replicate rows — see below. |
| `framework_version` | `str` | Coder Eval version chip. |
| `environment_info` | `dict` | Version/dependency info (may nest, e.g. `tool_plugins`). |

These are **computed**, not stored — derived from the counts and rows above on every
serialization, so they cannot drift from what they summarize. Read them rather than
re-deriving your own; independent re-derivations are how two consumers end up
publishing different numbers for the same run.

| Key | Type | Meaning |
| --- | --- | --- |
| `pass_rate` | `float \| None` | `tasks_succeeded / tasks_run` — errors are in the denominator, counted as misses. `None` on an empty run (0/0 is unknown, not 0%). |
| `error_share` | `float \| None` | `tasks_error / tasks_run`. Diagnostic only; never adjusts the rate. |
| `total_cost_usd` | `float \| None` | **The bill**: agent + judge + simulator, summed over the rows. `None` when nothing could be priced. |
| `agent_cost_usd` | `float \| None` | Subject-agent spend alone. The harness-vs-harness comparison figure — judge spend is a property of the suite's criteria and identical across harnesses, so leaving it in would make two harnesses look closer than they are. |
| `eval_overhead_cost_usd` | `float \| None` | Judge + simulator spend. The other half of `total_cost_usd`. |
| `tasks_cost_incomplete` | `int` | Rows whose recorded spend is missing money (unpriced model, or a hard kill that lost an in-flight turn). |
| `cost_complete` | `bool` | `tasks_cost_incomplete == 0`. When false, every cost figure above is a **floor**, not the bill. A run is never failed for this — see [Missing cost is never fatal](#missing-cost-is-never-fatal). |

### `task_results[]` — the flat per-task row

Each entry is an **untyped dict** (a denormalization, not a Pydantic model) with keys
including: `task_id`, `replicate_index`, `variant_id`, `status`
([`FinalStatus`](#finalstatus)), `weighted_score`, `duration`, `iteration_count`,
`tags`, `task_path`, `model_used`, `reference_similarity`, the token buckets
(`input_tokens` = uncached input, `output_tokens`, `cache_creation_input_tokens`,
`cache_read_input_tokens`, `total_tokens`), the cost fields
(`total_cost_usd` = agent + judge + simulator, plus the `agent_cost_usd` /
`judge_cost_usd` / `simulator_cost_usd` slices and the `cost_complete` flag),
`expected_commands`,
`actual_commands`, `commands_efficiency`, `agent_config`, `sdk_options`,
`installed_tools`, turn accounting (`total_turns`, `visible_turns`, `expected_turns`,
`max_turns_exhausted`, `has_final_reply`), and early-stop fields (`stopped_early`,
`early_stop_reason`, `turns_remaining_at_stop`). `iterations` here is a **reduced**
turn digest (`{iteration, duration_seconds, command_count, assistant_turn_count,
crashed, crash_reason}`) — the full transcript is in `task.json`.

### Missing cost is never fatal

Pricing degrades; the evaluation does not. A model absent from the rate card, a turn
the backend never priced, a hard-killed task that lost its in-flight spend: each one
lowers a total and sets `cost_complete: false`. None of them raises, none of them
books a zero, and none of them changes a run's exit code.

The reasoning is that the two failure modes are not symmetric. A missing cost is
recoverable after the fact — the token counts are on the record, so a corrected rate
card reprices the run from its artifacts. A failed run is not: the tokens are already
spent and the only way back is to run it again. So the framework warns loudly and
keeps going.

The warning fires up front. `check_pricing_coverage` walks every model the run pins
(subject agents and judge criteria) before the first task dispatches, and logs the
ones the card cannot price — early enough to fix the card and restart while it is
still cheap. After that the run is on its own: totals become floors, and
`tasks_cost_incomplete` says how many rows are behind that floor.

Consumers should treat any cost field as a lower bound whenever `cost_complete` is
false, and must not read `None` as `0.0` — "nothing could be priced" and "it was
free" are different facts.

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
| `post_failure_criteria_results` | `list[CriterionResult]` | Diagnostic artifact evidence collected after a terminal agent failure. It does not affect `final_status`, `weighted_score`, gating, or suite aggregation. |

**Transcript:** `iterations: list[TurnRecord]` (accepts legacy alias `turns`) — see
[TurnRecord](#turnrecord).

**Errors** (populated on failure): `error_message`, `error_details`,
`error_log_tail` (carries the Docker build-log tail for `BUILD_FAILED`).

**Config/environment:** `environment_info`, `agent_config`, `sdk_options` (raw
`ClaudeAgentOptions` dump), `sandbox_path`, `task_config`
(`{resolved, source_yaml, source_file, lineage}` — `lineage` maps each field to
`{value, source, source_detail}` so you can trace which config layer set it).
`environment_info.system_prompt_semantics` (`"append"` / `"replace"` /
`"unknown"`) records the system-prompt regime the agent ran with. Every agent
emits it — the base `Agent` supplies `"unknown"` for an agent that has not
declared its regime (including out-of-tree plugin agents), so an absent key
means one thing only: a run predating the marker. Those runs used
replace-on-set / empty-on-unset semantics on Claude Code and are not
score-comparable, so consumers should segment on it (absent ⇒ pre-append
regime; `"unknown"` ⇒ current run, undeclared agent). Codex runs before the
marker dropped `system_prompt` entirely and Antigravity always appended, so for
those two the boundary is a reporting change, not a behavioral one.
`sdk_options.system_prompt` is a `SystemPromptPreset` dict
(`{type: "preset", preset: "claude_code", exclude_dynamic_sections: true, append?: str}`)
on append-mode Claude Code runs and a plain string only in replace mode — it is
no longer `str | null`, so consumers must not string-handle it unconditionally.

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
`evaluation_status` (`evaluated` by default; `not_evaluated` means the check did
not run and is distinct from an evaluated 0.0),
`pass_threshold` (default 0.9), `gating` (default `true`; `false` = informational /
weight-0, excluded from the score and the pass/fail gate). The base allows extra
fields so subclass keys round-trip.

- **`classification`** adds `observed_label`, `expected_label` (sentinels like
  `(none)` / `(other)` allowed). Emitted by `classification_match`, `skill_triggered`.
- **`judge`** adds `findings`, `token_usage` (kept distinct from the agent total), and
  `transcript_path` (a sibling `judge-N.yaml`, or `post-failure-judge-N.yaml` for
  diagnostic records). The full `transcript` is **stripped
  from `task.json`** — read it from the referenced file. Emitted by `llm_judge`,
  `agent_judge`.

### Post-failure criterion evidence

When an agent crashes or its turn times out, coder-eval runs only deterministic,
read-only artifact criteria while the sandbox is still live: `file_exists`,
`file_contains`, `file_matches_regex`, `file_check`, `json_check`,
`reference_comparison`, and `classification_match`. Judges, trajectory checks,
`run_command`, and `uipath_eval` are recorded with
`evaluation_status="not_evaluated"`; they are not invoked on this recovery path.
The diagnostic list is additive evidence. An `ERROR` run remains `ERROR`, and its
canonical score remains 0.0.

### TurnRecord

`iteration`, `user_input`, `agent_output`, `commands` (`list[CommandTelemetry]`),
`timestamp`, `duration_seconds`, `token_usage`, `model_used`, `assistant_turn_count`,
`messages` (`list[TranscriptMessage]`, discriminated on `role`:
`user`/`assistant`/`reconciliation`), `provider_call_costs`
(`list[ProviderCallCost]` — one row per real upstream call with its ACTUAL cost +
cache buckets, captured proxy-side on the LiteLLM open-weight backend and rendered
by the evalboard as a per-call table; empty on every other
backend), `num_turns`, `max_turns_exhausted`,
`result_summary` (`{is_error, subtype, stop_reason, result}`), `crashed`,
`crash_reason`.

> **Token invariant.** Summing the four token buckets across `messages`
> (assistant + the synthetic `reconciliation` entry) equals `token_usage` exactly.
> The `reconciliation` message carries the residual the per-message stream
> under-reports; it has no cost and is excluded from turn/generation counts. The
> LiteLLM actual-cost join writes cost at the TURN level only (`token_usage.total_cost_usd`
> = the real bill) plus the per-call `provider_call_costs` audit record — it does
> NOT touch the message token buckets, so this invariant holds on every backend.
> See the [Claude Code guide](agents/CLAUDE_CODE.md#telemetry).

### EarlyStopInfo

Present (non-`null`) iff the run stopped early — there is no separate boolean.
Fields: `reason` (`criterion_passed` / `criterion_failed` /
`decision_budget_exceeded` — the last marks a fail-stop whose deciding
criterion timed out undecided past its `stop_early.decide_within`; it gates through
the same weighted armed gate as a native fail),
`deciding_criterion_type`, `deciding_criterion_description`, `armed_criteria`,
`sdk_turn_index`, `tool_call_index` (1-based, includes the in-flight call),
`elapsed_seconds`, `turns_remaining_at_stop`, `gate_threshold` (the
`run_limits.stop_early_gate_threshold` in effect for this stop; default `1.0`).

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

## Estimator changes

A rendered statistic can step for **identical data** when an estimator or a resample count
changes. Nothing in a run artifact distinguishes that from a real change in the thing being
measured — the interval simply reads differently — so this table is where such a step is
attributable. It qualifies the callout above: that one tells you to recompute CIs from
`per_replicate_scores`; this one tells you when the recomputation's answer moved.

The last column is a **PR number**, not a `framework_version`: `python-semantic-release` assigns
the version at merge, so an author cannot fill a version column truthfully while the change is in
flight. A PR number is knowable at authoring time and survives — `main` is squash-merged, so every
subject line carries its `(#NNN)` while the branch SHAs that produced it do not.
[`CHANGELOG.md`](https://github.com/UiPath/coder_eval/blob/main/CHANGELOG.md) maps the squashed
commit to the release that carried it. A change made on a long-lived branch before its own PR
merges may cite the branch commit, marked as such; the seed row below is one.

| Date | Change | Constant / fixture | Observed step | PR / commit |
| --- | --- | --- | --- | --- |
| 2026-08-13 | One resample count for every bootstrap, including `bootstrap_mean_ci`'s default | `reports_stats.BOOTSTRAP_RESAMPLES` 1000 → 2000 | `experiment_replicates.md`, both variants' CI upper bounds: `[0.850, 0.933]` → `[0.850, 0.950]` and `[0.600, 0.683]` → `[0.600, 0.700]` | `b306a99` (branch commit, pre-squash) |
| 2026-08-16 | `ExecutionGateVerdict` records whether Holm rejected its null, so the rendered `BLOCKED BY A GUARDRAIL` headline can require it. No estimator, resample count or arithmetic changed. | `optimize_verdicts/execution_gate.json`, `optimize_verdicts/execution_gate_refused.json` | **No statistic moved** — regeneration added the key `holm_rejected` and changed no other value (verified field-by-field against the prior pins). Both pins are pre-Holm `execution_gate` output, so it lands as `null`. | branch commit, pre-squash (PR #109) |

**A PR that changes a watched constant, or modifies a pinned rendered-number fixture, must add a
row here** — a CI job (`estimator-protocol` in `.github/workflows/pr-checks.yml`) fails otherwise.
It is diff-based, so it cannot run in `make verify`: a working tree has no base ref. If a PR moves
a fixture with no estimator change behind it, add a row saying the step was zero and why.

Where the check is sharp and where it is not, so a green job is not read as more than it is. It
watches **constant assignments** — `reports_stats`'s `BOOTSTRAP_RESAMPLES` / `DEFAULT_ALPHA` and
`optimize_gate`'s `MATERIALITY_FLOOR` / `GATE_P_PRECISION` / `GATE_MAX_FAMILY` / `GATE_RESAMPLES` /
`FLOOR_RESOLUTION` / `NEAR_FLOOR_MULTIPLE` — and not estimator **forms**. Changing the expression inside `reports_stats.bootstrap_p_floor`
(which has already happened once: `1/m` → `2/(m+1)`) steps every rendered p floor and is not
matched directly; it is caught only because that floor is rendered into a pinned fixture under
`tests/_fixtures/optimize_renders/`, which the fixture half watches. A form change that reaches no
pinned fixture is genuinely invisible. And a row records that a step happened; only the diff
records why.

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

`TOKEN_BUDGET_EXCEEDED` and `COST_BUDGET_EXCEEDED` are produced by the cumulative budget caps under
`run_limits:` (`max_input_tokens` / `max_output_tokens` / `max_total_tokens`, and `max_usd`
respectively), checked after each completed agent turn — see
[Task Definition Guide → Run Limits](TASK_DEFINITION_GUIDE.md#run-limits).

---

## Consumer gotchas at a glance

- `run.json.task_results` is a flat, untyped denormalization — the typed source of
  truth is each `task.json`.
- Experiment CIs / significance tests are **not** in `experiment.json` (render-time
  only); recompute from `per_replicate_scores`. When your recomputation disagrees with an older
  report over the same data, check [Estimator changes](#estimator-changes) before assuming the
  measurement moved.
- Judge `transcript` is stripped from `task.json`; follow `transcript_path`.
- `TokenUsage.total_tokens` is not serialized; sum the buckets (or use the computed
  `input_tokens` + `output_tokens` + cache buckets).
- `EarlyStopInfo` presence is itself the "stopped early" signal.
- `total_cost_usd` is the whole bill (agent + judge + simulator) at both row and run
  level; `agent_cost_usd` is the agent-only slice. `TokenUsage.total_cost_usd` is a
  different thing: the cost of those tokens, so always agent-only. `run_limits.max_usd`
  gates on that one, since judge and simulator spend is not known mid-run.
- A cost of `None` means unpriced, not free, and any total is a floor while
  `cost_complete` is false — see [Missing cost is never fatal](#missing-cost-is-never-fatal).

## See also

- [User Guide → Output Structure](USER_GUIDE.md#output-structure) and the
  [`aggregate`](USER_GUIDE.md#cli-commands) command
- [A/B Experiments → Reading the Report](AB_EXPERIMENTS.md#reading-the-report)
- [Task Definition Guide](TASK_DEFINITION_GUIDE.md)
