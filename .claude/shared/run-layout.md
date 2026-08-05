<!-- Mirrored verbatim at plugins/coder-eval/reference/run-layout.md — update both together. -->
# Run layout

The on-disk structure of a coder_eval evaluation run — the factual contract every
run-reading command and skill follows. If the run directory structure changes, update it
here and every consumer follows.

```
runs/<run_id>/<variant_id>/<task_id>/<NN>/{task.json, task.log, artifacts/}
```

- `<NN>` — zero-padded replicate index (e.g. `00`, `01`).
- `task.json` — the persisted per-replicate result (the consumer contract; carries the large `turns` array).
- `task.json.malformed` — present only on the docker degrade path: when an existing `task.json` fails to parse (schema skew from a stale `:latest` image, or a truncated/torn write), the docker runner moves the unparseable original aside to this sidecar and writes a synthetic `final_status=ERROR` `task.json` in its place. Diagnostic-only; `rglob("task.json")` consumers do not match it.
- `task.log` — the human-readable task log; `artifacts/` — files the agent produced.

**Scope-marker files** (used to detect what a given path represents):

- `run.json` at the run root → **run scope**. If `experiment.json` (+ `experiment.md`) is also present → multi-variant experiment.
- `variant.json` at a variant directory → **variant scope**.
- `task.json` directly in the path → **task scope** (single replicate); `??/task.json` subdirs without `variant.json` → task scope aggregated over replicates.

**Default target:** when a command takes a run-path argument and none is given,
default to `runs/latest` and tell the user you fell back to the default.
