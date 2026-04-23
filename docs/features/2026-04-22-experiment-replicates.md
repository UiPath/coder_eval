# Experiment Replicates

## What it does

Run each (task, variant) combination N times to quantify agent variance. Per-replicate results are preserved separately on disk; reports aggregate them with bootstrap confidence intervals and, for 2-variant experiments, a paired mean-difference test with Cohen's d effect size.

## How to configure it

Three places (precedence: CLI > variant > experiment defaults > `experiments/default.yaml`):

1. **CLI**: `coder-eval run --repeats 5 tasks/*.yaml`
2. **Variant-level** in experiment YAML:
   ```yaml
   variants:
     - variant_id: opus
       repeats: 5
   ```
3. **Experiment-level** (applies to all variants):
   ```yaml
   defaults:
     repeats: 5
   ```

Task YAML does **not** accept `repeats:` — replicates are a run-shape concern, not a task-correctness concern.

## Output layout

```
runs/<id>/<variant>/<task>/
├── 00/          # replicate 0
│   ├── task.json
│   ├── task.log
│   └── artifacts/
├── 01/          # replicate 1
│   └── ...
└── 02/          # replicate 2 (when repeats=3)
    └── ...
```

The aggregate `variant.md` / `experiment.md` reports roll replicates into a single score per (task, variant) using the mean, preserving the raw scores for statistical analysis.

## Reports

When any variant has `repeats > 1`, a **Replicate Statistics** section is added to `experiment.md`:

- **Per-variant row**: mean score, 95% bootstrap CI on weighted_score, Wilson CI on pass-rate.
- **Cross-variant (2 variants only)**: paired mean-diff + 95% CI + Cohen's d. Skipped when replicate counts differ across variants on any task.
- **Single-variant or `repeats=1`**: no new section; existing reports unchanged.

`variant.md` also gains a one-liner showing the score 95% CI when `repeats > 1`.

## Constraints

- Max 99 replicates (2-digit directory padding: `00`–`99`).
- Replicates share the full resolved config — no per-replicate mutations or seeds.
- Final status on the aggregate = worst status across replicates (error > failed > succeeded).
- `score_spread` in `TaskExperimentSummary` is computed from mean-score `VariantResult`s (narrower than raw replicates — this is intentional).

## Where it sits in the evaluation flow

```
CLI (--repeats N)
  → ExperimentRunner.resolve_all_tasks (fan-out: rows × variants × repeats ResolvedTask objects)
  → run_batch (N× tasks, each with its own run_dir)
  → Orchestrator (unchanged; writes one task.json per replicate)
  → aggregate_results (groups replicates into one VariantResult per (task, variant))
  → reports_experiment (new Replicate Statistics section)
```

## Example

```yaml
# experiments/my-experiment.yaml
experiment_id: model-comparison
defaults:
  repeats: 5
variants:
  - variant_id: sonnet
  - variant_id: opus
```

```bash
coder-eval run experiments/my-experiment.yaml tasks/*.yaml
# Produces: runs/<id>/sonnet/<task>/{00..04}/task.json
#           runs/<id>/opus/<task>/{00..04}/task.json
# experiment.md includes ## Replicate Statistics with bootstrap CIs and
# paired mean-diff between sonnet and opus.
```
