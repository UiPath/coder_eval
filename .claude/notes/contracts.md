# Contracts: criteria, datasets, judging

> Conventions and authority order: see [README.md](README.md).

## Datasets and aggregation

- **Dataset fan-out**: `TaskDefinition.dataset` (inline rows or JSONL path) expands a single task into N row-tasks with `${row.<field>}` substitution in `initial_prompt` and `success_criteria` string fields. Expansion runs in `task_loader.expand_dataset` **before** variant resolution, so variants cannot override the dataset. Row sampling: CLI `--sample N` (fixed-seed uniform-random N over the whole dataset) overrides `--sample-per-stratum N` / `dataset.sample_per_stratum` (stratified random N-per-stratum, keyed on `stratify_field`, default `expected_skill` — for classification suites like activation). Stratified sampling (whether the N-per-stratum count comes from the **CLI** `--sample-per-stratum` flag or **YAML** `dataset.sample_per_stratum`) is **nondeterministic** by default — it re-draws each run (so the nightly activation suite broadens coverage over time). Set `dataset.sample_seed` to pin a reproducible sample; an explicit seed always wins. (Only `--sample N` uses a fixed seed, since a smoke test wants the same N rows each run.)

- **Per-criterion aggregation**: Each `BaseCriterion` subclass exposes `aggregate(criterion, per_row_results) -> CriterionAggregate | None`. Default emits `count / mean / median / std / min / max` so every criterion is suite-thresholdable for free. Classification-style criteria return `ClassificationCriterionResult` (subclass of `CriterionResult`) and layer accuracy / P/R/F1 / confusion via the shared `overlay_classification_metrics` utility. `BaseSuccessCriterion.suite_thresholds` gates the suite on those metrics; CLI exits non-zero on any gate failure.
