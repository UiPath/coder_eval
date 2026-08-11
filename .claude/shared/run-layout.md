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

**Scope-marker files** (used to detect what a given path represents):

- `run.json` at the run root → **run scope**. If `experiment.json` (+ `experiment.md`) is also present → multi-variant experiment.
- `variant.json` at a variant directory → **variant scope**.
- `task.json` directly in the path → **task scope** (single replicate); `??/task.json` subdirs without `variant.json` → task scope aggregated over replicates.

**No target given:** do not assume a path. Resolve the repository's run root by discovery
— a directory holding run directories, each with a `run.json` — then within it use a
`latest` symlink if one exists and resolves, and otherwise the newest run directory by
name (run ids sort chronologically). Say which root you resolved, how you resolved it,
and which run you picked, before reading anything through it.
