---
allowed-tools: Read(*), Glob(*), Grep(*), Bash(ls:*), Bash(jq:*), Bash(python3:*), Write(runs/*)
description: Generate per-task review.json (summary + tags) for a completed run
---

## Context

If `$ARGUMENTS` is empty or blank, resolve the run to review per the "No target given" rule in `.claude/shared/run-layout.md` — discover the run root, then take `latest` if that symlink resolves and the newest run directory by name if it does not. Tell the user which run you picked and how you got there before reading anything.

You are producing post-run reviews for a coder_eval evaluation run. The target path is: `$ARGUMENTS`

For each **failed task** in the run you write a structured `review.json` next to that task's `task.json`, plus a small `review_index.json` digest at the run root for fast aggregation. Output is JSON only — no narrative.

## Step 1: Load the suggested vocabulary

Read `src/coder_eval/resources/tags.yaml` (relative to the repo root). It has a top-level `tags:` list of `{name, definition, examples}` entries. **Prefer these names** when classifying — but the vocabulary is a suggestion, not a strict allowlist. If nothing fits, emit a kebab-case slug that does. Drift is caught by a lint, not blocked at write time.

If the vocabulary file is missing or unparseable, continue with an empty suggestion list (don't abort) — drift lint will surface every tag you emit.

## Step 2: Discover failed tasks

The run layout (`runs/<run_id>/<variant_id>/<task_id>/<NN>/…`, `<NN>` a zero-padded replicate index) is defined in `.claude/shared/run-layout.md`.

1. Read `<run_path>/run.json` if present (for context — `run_id`, `start_time`).
2. Glob `<run_path>/*/*/*/task.json` and read each one.
3. Read `<run_path>/analysis.md` if present — it already diagnoses many failures; lean on its findings rather than re-deriving them.
4. Skip any task whose `final_status` is `"NOT_GRADED"` — `coder-eval execute` produced it, no criterion ran, and `weighted_score` is `null`. It is neither a pass nor a failure, and comparing `null < 0.9` would book every ungraded row as a failure to review.
5. Of the rest, a task counts as **failed** if `final_status != "SUCCESS"` **or** `weighted_score < 0.9`. Skip passing tasks for now (we may extend to passing tasks later — the schema supports it).

If no `task.json` files exist, write an empty `review_index.json` (`{"reviews": []}`) and exit.

## Step 3: Write per-task review.json

For each failed task, write `<run_path>/<variant_id>/<task_id>/<NN>/review.json` with this exact shape:

```json
{
  "task_id": "skill-flow-ipe-ceql-where",
  "summary": "Validation failed with 'expected string, received undefined' on CeqlWhereTest.flow; 1200s turn_timeout was hit waiting for the connector to activate.",
  "tags": ["validation-error", "infra"],
  "created_at": "2026-05-08T12:00:00Z"
}
```

Field rules:

- **`task_id`** — exact `task_id` from the corresponding `task.json`. **Must equal the parent directory name** — the review validator rejects mismatches because they would silently misattribute reviews. (variant_id and replicate are encoded by the file path; no need to repeat them.)
- **`summary`** — 1-3 sentences of plain prose describing what happened. Cite specific evidence: error string, criterion name, agent behavior, or a quote from `analysis.md`. ≤ 480 chars. This is the human-readable explanation; tags are the machine-readable filter.
- **`tags`** — array of kebab-case strings. ≤ 8 per task. Prefer names from `tags.yaml`; new ones are fine when nothing fits. Each tag must match `^[a-z0-9]+(-[a-z0-9]+)*$`.
- **`created_at`** — ISO 8601 UTC timestamp matching `YYYY-MM-DDTHH:MM:SS(.fff)?(Z|±HH:MM)` (e.g. `2026-05-08T12:00:00Z`). Bare strings like `"yesterday"` are rejected.

Examples of good reviews:

- `task.json` shows `weighted_score=0.0`, criterion `uipath_eval` failed with `"Schema validation failed: expected string, received undefined"`:
  ```json
  {"task_id":"skill-flow-ipe-ceql-where","summary":"uipath_eval criterion failed: 'Schema validation failed: expected string, received undefined' on CeqlWhereTest.flow.","tags":["validation-error"],"created_at":"..."}
  ```
- Agent ran the same `uip run flow.flow` 7 times with identical error:
  ```json
  {"task_id":"...","summary":"Agent retried 'uip run flow.flow' 7x without adapting after CLI exit code 1.","tags":["agent-loop"],"created_at":"..."}
  ```
- Files look correct but `command_executed` expected `uip publish` while agent used `uipath publish`:
  ```json
  {"task_id":"...","summary":"Agent produced correct outputs; command_executed pattern was too narrow (expected 'uip publish', agent used 'uipath publish').","tags":["criteria-bug"],"created_at":"..."}
  ```

## Step 4: Write the run-level index

After all per-task reviews are written, write `<run_path>/review_index.json`:

```json
{
  "generated_at": "2026-05-08T12:00:00Z",
  "reviews": [
    {
      "task_id": "skill-flow-ipe-ceql-where",
      "variant_id": "default",
      "replicate": "00",
      "tags": ["validation-error", "infra"],
      "summary_excerpt": "uipath_eval criterion failed: 'Schema validation failed: expected string, received undefined'..."
    }
  ]
}
```

- One entry per `(task_id, variant_id, replicate)` triple corresponding to a `review.json` you wrote — the review validator rejects index/file set mismatches.
- `summary_excerpt` (required): first 160 chars of the per-task `summary`, ellipsized if truncated. ≤ 200 chars hard cap.
- The index is a *digest*: it lets the run-grid UI and the cross-run hotspots view aggregate without fetching N per-task files. The per-task `review.json` remains the source of truth.

## Step 5: Report

Print one line:

```
Wrote 7 review.json files + review_index.json to runs/2026-05-08_04-46-07: 4 distinct tag(s)
```

## Principles

- **Per-task review is the source of truth**: the run-level index is a derived digest, not a separate dataset.
- **Vocabulary first, free-form last**: prefer names from `tags.yaml`. Make up new ones only when nothing fits — drift lint will surface them.
- **Evidence in `summary`**: every summary must cite specific failure data — error string, criterion description, observed behavior, quote from `analysis.md`.
- **Skip passing tasks** (for now): keeps the review set focused. The schema can extend to positive reviews later (e.g. `tags: ["optimal-path"]`).
- **Don't validate against the vocabulary**: emit whatever fits. The review tooling and the lint script handle drift.
