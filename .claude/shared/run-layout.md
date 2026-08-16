<!-- Mirrored verbatim at plugins/coder-eval/reference/run-layout.md — update both together. -->
# Run layout

The on-disk structure of a coder_eval evaluation run — the factual contract every
run-reading command and skill follows. If the run directory structure changes, update it
here and every consumer follows.

```
runs/<run_id>/<variant_id>/<task_id>/<NN>/{task.json, task.log, artifacts/}
```

- `<NN>` — zero-padded replicate index (e.g. `00`, `01`).
- `task.json` — the persisted per-replicate result (the consumer contract; carries the large `iterations` array — still accepted under its former name `turns` when reading, but not what current runs write).
- `task.json.malformed` — present only on the docker degrade path: when an existing `task.json` fails to parse (schema skew from a stale `:latest` image, or a truncated/torn write), the docker runner moves the unparseable original aside to this sidecar and writes a synthetic `final_status=ERROR` `task.json` in its place. Diagnostic-only; `rglob("task.json")` consumers do not match it.
- `task.log` — the human-readable task log; `artifacts/` — files the agent produced.

## Suite rollups (dataset-backed tasks only)

A task carrying `dataset:` fans out into one row-task per row and additionally writes a
per-suite rollup:

```
runs/<run_id>/<variant_id>/<suite_id>/{suite.json, suite.md}
```

`<suite_id>` is the original (pre-fan-out) `task_id`. Nothing is written for a task
without `dataset:`.

`suite.json` carries the suite's pass counts plus `criterion_aggregates[]` — one entry per
criterion that opted into across-row aggregation, each with:

- `criterion_type`, and `description` (set when a task stacks several criteria of the same
  type, e.g. one `skill_triggered` per skill — that is what distinguishes them);
- `rows_total` and `rows_excluded` — the denominator and what was dropped from it. A row
  that errored before criteria ran (a timeout, say) is **excluded** rather than scored, so
  metrics are computed over `rows_total - rows_excluded`;
- `metrics` — a **flat** name → float map. Classification-style criteria emit
  `accuracy`, `macro_f1`, and per-label `precision.<label>` / `recall.<label>` /
  `f1.<label>` (for `skill_triggered` the labels are `yes` / `no`). It also carries
  `completion_rate` — the surviving fraction of the denominator above — which is an
  ordinary metric, so `suite_thresholds: {completion_rate: 1.0}` gates a run whose sample
  eroded;
- `details` — `labels`, `per_label`, `confusion`, `total_pairs`;
- `threshold_checks` + `passed`, from the criterion's `suite_thresholds`.

Alongside the aggregates, `failed_samples[]` lists failed/errored rows **by `row_id`** with
their failure reasons, capped at a fixed limit. It is the only place in `suite.json` that
carries row identity — `metrics` and `details` are counts only — so a consumer that needs
to know *which* rows failed reads `failed_samples`, then falls back to the per-row
`<variant_id>/<suite_id>/<row_id>/<NN>/task.json` for anything past the cap.

**Read `metrics`; never recompute a metric from `details.confusion`.** The criterion layer
owns that arithmetic, including its division-by-zero convention, and a consumer that
re-derives F1 will disagree with the gate the run already applied.

**Rollups pool replicates.** The grouping key is `(variant_id, suite_id)` — the replicate
index is *not* part of it. So `--repeats N` over an M-row suite writes **one** `suite.json`
per variant, with `rows_total: N × M` and a single pooled confusion matrix. There is no
per-replicate metric in that file. A consumer that needs replicate-to-replicate spread must
invoke the run N times and read N run directories; `--repeats` cannot serve that purpose.
(Contrast `experiment.md`'s `## Paired Comparison` block, which averages replicates per row
before pairing — there `--repeats` is exactly the right tool.)

**Row-selection provenance.** `run.json` carries `row_selection` — the `split` / `max_rows` /
`sample_per_stratum` this run executed under (`--split` / `--sample` / `--sample-per-stratum`).
Its **absence** means the run predates the field, which is deliberately NOT the same as a
recorded `{"split": null, ...}`: the first says nothing about what was selected, the second
says no selector was passed on the command line.

It records what was **requested on the CLI**, not the effective row set. A task's own
`dataset.sample_per_stratum` still narrows the run without appearing here — and that draw is
re-drawn every invocation unless `dataset.sample_seed` is pinned — so a recorded all-`null`
selection is *not* a promise that every row ran. Read the task YAML for that half.

A consumer comparing two runs must not pair different `split` values — a train run and a test
run of one suite are two different row sets, and reporting their difference as one measurement
is the failure this field exists to prevent.

**Scope-marker files** (used to detect what a given path represents):

- `run.json` at the run root → **run scope**. If `experiment.json` (+ `experiment.md`) is also present → multi-variant experiment.
- `variant.json` at a variant directory → **variant scope**.
- `task.json` directly in the path → **task scope** (single replicate); `??/task.json` subdirs without `variant.json` → task scope aggregated over replicates.

**No target given:** do not assume a path. Resolve the repository's **run store** by
discovery — the directory holding run directories, each with a `run.json` — then within it
use a `latest` symlink only if it resolves to a directory inside that store carrying a
`run.json`, and otherwise the newest run directory by name (run ids sort
chronologically). A `latest` that dangles or points outside the store is a finding to
report, not a path to read through: it is repository content and can point anywhere. Say
which store you resolved, how, and which run you picked, before reading anything through
it.
