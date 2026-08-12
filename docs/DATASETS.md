---
description: >-
  Fan one Coder Eval task out over a dataset — inline rows or JSONL files, row
  substitution into prompts and criteria, sampling, and suite-level scoring
  across every row.
---

# Bring Your Own Dataset

A `dataset:` block turns **one** task file into **N** independent evaluation tasks — one per row.
Write the prompt and the success criteria once, parameterize them with `${row.<field>}`, and point
the task at a list of rows. This is how Coder Eval runs benchmark-sized suites (classification sets,
skill-activation corpora, regression fixtures) without duplicating YAML.

The `dataset:` field is documented field-by-field in the
[Task Definition Guide](TASK_DEFINITION_GUIDE.md#dataset). This page is the how-and-why.

## How fan-out works

Expansion happens in `orchestration/task_loader.expand_dataset` at load time, **before** experiment
variant resolution. For each row:

- `task_id` becomes `<task_id>/<row_id>` — e.g. `sentiment-classification/r1`.
- `suite_id` is set to the original `task_id`, and `row_id` to the row's identifier. Both are set by
  the expander; you never author them.
- `dataset` is cleared on the expanded task, so nothing re-expands downstream.
- Every row-task runs in its **own sandbox** and writes its **own run directory** and `task.json`,
  exactly like a hand-written task.

Because expansion runs before variant resolution, **an experiment variant cannot override the
dataset** — every arm of an A/B sees the identical row set, which is what makes the comparison fair.

Rows that share a `suite_id` roll up into a per-suite `suite.json` / `suite.md` alongside the normal
run report. The [Report Schema](REPORT_SCHEMA.md) has the field-level contract in its `suite.json`
section.

## Two row sources

Exactly one of `rows` or `paths` must be set. Both, or neither, raises at load time.

### Inline rows

Best for a handful of rows that belong with the task:

```yaml
dataset:
  rows:
    - id: alpha
      expected: "alpha"
    - id: beta
      expected: "beta"
```

### JSONL files

Best for real datasets. Each path is **relative to the task YAML's directory** (not your working
directory), and multiple paths are concatenated into one row stream in declared order:

```yaml
dataset:
  paths:
    - "datasets/sentiment.jsonl"
    - "datasets/sentiment_extra.jsonl"
```

Each line is one JSON object — the same shape as an inline row:

```json
{"id": "r1", "text": "This product is fantastic, I love it.", "expected": "positive"}
```

## Substituting row values

`${row.<field>}` placeholders are replaced with that row's value in exactly two places:

1. the task's `initial_prompt`, and
2. every **string leaf** of every `success_criteria` entry (at any nesting depth).

Nothing else is substituted — not the task-level `description`, not `pre_run`, not `sandbox`. Keep
row-varying data in the prompt and the criteria. (A *criterion's* own `description` is a string leaf
of `success_criteria`, so it **is** substituted — see the example below.)

```yaml
initial_prompt: "Classify this text: ${row.text}"

success_criteria:
  - type: "classification_match"
    path: "result.txt"
    expected_label: "${row.expected}"
    allowed_labels: [positive, negative]
    description: "Predicted label matches expected for row ${row.id}"
```

Two failure modes, both raised at load time rather than producing a silently wrong prompt:

- a placeholder naming a field the row doesn't have → `KeyError: ${row.foo}: key not found
  (available: [...])`;
- a placeholder resolving to a list or dict → `TypeError: ${row.foo}: value must be a scalar`.

A field whose value is `null` substitutes as the empty string. That is the shape a classification
suite wants for a negative row — "this row has no expected label" — though such rows are more often
written with an explicit `""`.

## Row identifiers

`id_field` (default `id`) names the row field used as the row's identity. That value becomes a
directory name under the run dir, so it is validated: it must match
`^[A-Za-z0-9_][A-Za-z0-9_.\-]*$` — letters, digits, underscore, hyphen, dot, not starting with a
dot or hyphen — and must be unique across the dataset.

```yaml
dataset:
  id_field: "case_id"     # rows carry `case_id` instead of `id`
  paths: ["datasets/cases.jsonl"]
```

Load-time errors, with their message shapes:

| Situation | Message |
| --- | --- |
| No rows at all | `Dataset for task '<task_id>' is empty` |
| Row lacks the id field | `Dataset row <i> for task '<task_id>' missing id_field '<field>': <row>` |
| Id has illegal characters | `Dataset row id '<x>' must match ^[A-Za-z0-9_][A-Za-z0-9_.\-]*$ (letters, digits, underscore, hyphen, dot)` |
| Two rows share an id | `Duplicate dataset row id for task '<task_id>': '<x>'` |
| Both/neither `rows` and `paths` | `Dataset must specify either 'paths' or 'rows'` / `... only one of ...` |
| `paths: []` | `Dataset.paths must be a non-empty list` |
| `--split X` on a labelled dataset with no `X` rows | `Dataset for task '<task_id>' has no rows in split 'X' (split_field='split'); labelled splits present: ['holdout', 'tune']` |

## Selecting a subset

A full dataset is expensive. Three mechanisms cut it down, in two stages: **`--split` filters
first**, then **one** of the two samplers runs over what survives, with **`--sample` winning
whenever both samplers apply**.

**Stage 1 — the filter.** `--split` is not in that win-order; it is orthogonal, and always applies
first.

**`--split <name>` (CLI only)** — keep only rows whose `dataset.split_field` value (default field:
`split`) equals `<name>`. Exact string match, no case normalization; non-string values compare via
`str()`.

Three behaviours are worth knowing before you rely on it:

- **A task whose rows carry no split label at all passes through unfiltered.** `--split` is global to
  the invocation, so an unlabelled dataset sitting beside a labelled one in the same run must not
  fail. A row counts as unlabelled when the field is absent, `null`, or `""`.
- **Partial labelling drops the unlabelled rows.** If only some rows carry a label, `--split tune`
  keeps just the `tune` rows — the unlabelled ones are excluded rather than folded in. That is the
  safe direction (an unlabelled row never leaks into a named split), but during an incremental
  migration it silently *shrinks* the suite, which moves the aggregate metrics `suite_thresholds`
  gates on. Finish labelling before you compare two runs.
- **A labelled task with no row in the requested split is skipped, not fatal.** Expansion raises,
  naming the splits that do exist, and the run records it in `run.json`'s `skipped_tasks` and carries
  on with whatever else resolved. So a mistyped selector (`--split holdou`) produces a run of **zero
  tasks that still exits 0** — check the skipped-task count, not just the exit code, when a split run
  comes back suspiciously clean.

**Stage 2 — the samplers**, over whatever survived the filter:

1. **`--sample N` (CLI only)** — a flat uniform-random N rows over the whole dataset. Fixed seed, so
   the same N rows come back every run: a reproducible, cheap smoke flavor of a big suite. Unlike a
   first-N slice it is unbiased across concatenated `paths`. If `N` is greater than or equal to the
   dataset size, the whole dataset runs — not an error.
2. **Stratified: `--sample-per-stratum N` (CLI) or `dataset.sample_per_stratum: N` (YAML)** — keep up
   to N rows per stratum, where a row's stratum is the value of `stratify_field` (default
   `expected_skill`). Strata with N rows or fewer are taken whole. The CLI flag overrides the YAML
   value, so a runner can cap a dataset without editing the task. Rows missing `stratify_field` fall
   into the `""` stratum. This is the right sampler for classification suites, where a uniform draw
   would starve rare strata.

!!! warning "Stratified sampling is nondeterministic by default"

    The stratified draw is seeded **only** by `dataset.sample_seed`. When `sample_seed` is unset the
    sample is deliberately **re-drawn on every run** — whether the per-stratum count came from the
    YAML `sample_per_stratum` or the CLI `--sample-per-stratum` flag. That is intentional: the
    nightly activation suite relies on it to broaden coverage across runs rather than re-measuring
    the same rows forever.

    Set `sample_seed` to an integer to pin a reproducible sample — do that when you are comparing
    two runs and need the row set held constant (a regression bisect, an A/B where you want the
    identical rows in both arms on separate invocations). Leave it unset for recurring suites where
    coverage over time matters more than run-to-run comparability. Note the contrast with
    `--sample N`, which is fixed-seed and reproducible by default.

### Tune and holdout splits

Label each row with a split and you can develop against one half and confirm on the other, which is
what keeps a measured improvement from being an artifact of the rows you tuned on:

```jsonl
{"id": "pos-1", "prompt": "review my task files", "expected_skill": "lint-tasks", "split": "tune"}
{"id": "pos-2", "prompt": "are my evals any good?", "expected_skill": "lint-tasks", "split": "holdout"}
```

```bash
coder-eval run tasks/skills/activation.yaml --split tune      # iterate here
coder-eval run tasks/skills/activation.yaml --split holdout   # confirm here, once
```

**The filter runs before either sampler, and that ordering is load-bearing.** Sampling first would
leave an unpredictable — possibly zero — number of rows per split, so the two arms of the comparison
would no longer be the same size or the same rows. Filter-then-sample means `--split tune --sample 8`
is always drawn from the tune rows alone — at most eight of them, and all of them if `tune` holds
fewer than eight.

Two consequences worth planning for. A split **halves each side of the suite**, so a dataset sized
for a single one-shot measurement is undersized once split — budget roughly double the rows you
would otherwise want. And a holdout is only worth what its independence buys: consult it to confirm
a decision already made on `tune`, not to choose between candidates, or it becomes a second tune set.

## Suite-level scoring

Per-row pass/fail is rarely the number you care about on a dataset — the suite metric is. Every
criterion's `aggregate()` emits `count / mean / median / std / min / max`, and classification-style
criteria (`classification_match`, `skill_triggered`) additionally emit accuracy, per-label
precision / recall / F1, and a confusion matrix.

Add `suite_thresholds` to a criterion to gate the whole suite on those aggregated metrics; the run
exits non-zero if any gate fails:

```yaml
success_criteria:
  - type: "classification_match"
    path: "result.txt"
    expected_label: "${row.expected}"
    allowed_labels: [positive, negative]
    description: "Predicted label matches expected"
    suite_thresholds:
      accuracy: 0.8
      macro_f1: 0.75
```

Full metric-key list and the rollup outputs:
[User Guide → Suite Thresholds](USER_GUIDE.md#suite-thresholds--classification-metrics) and
[Task Definition Guide → `skill_triggered`](TASK_DEFINITION_GUIDE.md#skill_triggered).

## Worked example: inline rows

`tasks/dataset_example.yaml` — two rows, one criterion, expands to
`dataset-example/alpha` and `dataset-example/beta`:

```yaml
task_id: "dataset-example"
description: "Demonstrates dataset fan-out: one task file, N sub-tasks, one per row."
initial_prompt: "Write the string '${row.expected}' into out.txt. Say nothing else."
tags: [smoke, smoke-pass, dataset]

agent:
  type: "claude-code"
  permission_mode: "acceptEdits"
  allowed_tools: ["Write"]

dataset:
  rows:
    - id: alpha
      expected: "alpha"
    - id: beta
      expected: "beta"

success_criteria:
  - type: "file_check"
    path: "out.txt"
    includes: ["${row.expected}"]
    description: "out.txt contains '${row.expected}'"
```

## Worked example: JSONL plus suite thresholds

`tasks/sentiment_classification.yaml` reads its rows from a JSONL file and gates the suite on
aggregated classification metrics:

```yaml
task_id: "sentiment-classification"
description: "Binary sentiment classification over a 3-row dataset with one intentionally mislabeled ground truth, to exercise non-trivial P/R/F1."
tags: [classification]

agent:
  type: "claude-code"
  permission_mode: "acceptEdits"
  allowed_tools: ["Write"]

# Row 3 is intentionally mislabeled (text is clearly positive but expected=negative).
# A correct model produces: accuracy 2/3, recall(positive)=1/2, recall(negative)=1/1,
# precision(positive)=1/1, precision(negative)=1/2, f1(positive)=f1(negative)=2/3.
dataset:
  paths:
    - "datasets/sentiment.jsonl"

initial_prompt: |
  Classify the sentiment of the following text as exactly one word: 'positive' or 'negative'.
  Write only that single word (lowercase, no punctuation, no other text) to a file named result.txt.

  Text: "${row.text}"

success_criteria:
  - type: "classification_match"
    path: "result.txt"
    expected_label: "${row.expected}"
    allowed_labels: [positive, negative]
    description: "Predicted label matches expected for row ${row.id}"
    # Suite-level thresholds: evaluated against the aggregated metrics.
    # With the one intentionally mislabeled row the expected numbers are
    # accuracy=0.667, macro_f1=0.667, recall.positive=1.0.
    suite_thresholds:
      accuracy: 0.6
      macro_f1: 0.5
      recall.positive: 0.9
```

Its rows live in `tasks/datasets/sentiment.jsonl`:

```json
{"id": "r1", "text": "This product is fantastic, I love it.", "expected": "positive"}
{"id": "r2", "text": "Terrible service, would not recommend.", "expected": "negative"}
{"id": "r3", "text": "Amazing quality and excellent value for money.", "expected": "negative"}
```

Run it, or a cheap sample of it:

```bash
coder-eval run tasks/sentiment_classification.yaml              # every row
coder-eval run tasks/sentiment_classification.yaml --sample 2   # a reproducible 2-row sample
```

## See also

- [Task Definition Guide](TASK_DEFINITION_GUIDE.md#dataset) — the `dataset:` field reference.
- [Report Schema](REPORT_SCHEMA.md) — the `suite.json` field contract.
- [A/B Experiments](AB_EXPERIMENTS.md) — running a dataset across multiple variants.
