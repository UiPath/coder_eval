# Run layout (shared)

The on-disk structure of a coder_eval evaluation run — the factual contract that
`coder-eval-run-analysis` and `coder-eval-review` both read. If the run directory
structure changes, update it here and every consumer follows.

```
runs/<run_id>/<variant_id>/<task_id>/<NN>/{task.json, task.log, artifacts/}
```

- `<NN>` — zero-padded replicate index (e.g. `00`, `01`).
- `task.json` — the persisted per-replicate result (the consumer contract; carries the large `turns` array).
- `task.log` — the human-readable task log; `artifacts/` — files the agent produced.

**Scope-marker files** (used to detect what a given path represents):

- `run.json` at the run root → **run scope**. If `experiment.json` (+ `experiment.md`) is also present → multi-variant experiment.
- `variant.json` at a variant directory → **variant scope**.
- `task.json` directly in the path → **task scope** (single replicate); `??/task.json` subdirs without `variant.json` → task scope aggregated over replicates.

**Default target:** when a command takes a run-path argument and none is given,
default to `runs/latest` and tell the user you fell back to the default.
