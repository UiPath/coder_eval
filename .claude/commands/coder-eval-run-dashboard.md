---
allowed-tools: Bash(*), Read(*), Glob(*), Grep(*), Agent, Monitor, TaskCreate, TaskUpdate, TaskStop
description: Run the dashboard pipeline (tests + blob upload + ADX ingest) and monitor progress
---

## Context

Run the coder_eval dashboard pipeline: execute a test suite, upload results to Azure Blob Storage, and ingest into ADX. The dashboard lives at `dashboard/` and is invoked via `uv run dashboard run` from the `dashboard/` directory.

## Arguments

`$ARGUMENTS` is parsed as space-separated flags. Supported options:

| Argument | Default | Description |
|----------|---------|-------------|
| `suite=<name>` | *(all suites)* | Suite to run: `skills`, `smoke`, `flow-init`, `flow` |
| `model=<model>` | `claude-sonnet-4-6` | Model to evaluate |
| `tags=<tags>` | *(suite default)* | Tag filter for tasks |
| `backend=<name>` | *(default)* | API backend: `direct`, `bedrock`, `proxy` |
| `skip-build` | false | Skip UiPath CLI build step |
| `skip-pull` | false | Skip git pull steps |
| `skip-analysis` | false | Skip AI analysis generation |
| `verbose` | false | Enable verbose logging |

Examples:
- `/run-dashboard suite=skills` — run skills suite with defaults
- `/run-dashboard suite=smoke skip-build skip-pull` — quick smoke run
- `/run-dashboard suite=flow model=claude-opus-4-6 backend=proxy` — flow suite with Opus via proxy
- `/run-dashboard` — run all suites

If `$ARGUMENTS` is empty, run all suites and inform the user: "No suite specified — running all suites."

## Step 1: Parse Arguments and Build Command

Parse `$ARGUMENTS` into dashboard CLI flags. Build the command:

```
cd dashboard && uv run dashboard run [flags]
```

Map arguments to flags:
- `suite=X` → `--suite X`
- `model=X` → `--model X`
- `tags=X` → `--tags X`
- `backend=X` → `--backend X`
- `skip-build` → `--skip-build`
- `skip-pull` → `--skip-pull`
- `skip-analysis` → `--skip-analysis`
- `verbose` → `--verbose`

## Step 2: Run the Dashboard

1. Pick a deterministic log path: `tmp/dashboard-run-<timestamp>.log` (use `date +%Y%m%d-%H%M%S`). Ensure `tmp/` exists at the repo root via `mkdir -p tmp`.
2. Show the user the exact command you're about to run, including the log path.
3. Execute the command using `Bash` with `run_in_background: true`. Redirect output to the log file via `tee` so it's captured for monitoring:

    ```bash
    mkdir -p tmp && cd dashboard && uv run dashboard run [flags] 2>&1 | tee ../tmp/dashboard-run-<timestamp>.log
    ```

    Do **not** set a wall-clock `timeout` — full pipeline runs (especially `flow` or all-suites) routinely exceed 10 minutes. Rely on the pipeline's own completion rather than killing it from the outside.
4. Immediately set up a `Monitor` (persistent) on the log file path — `tmp/dashboard-run-<timestamp>.log`. Monitor takes a file path, not a shell pipeline; do **not** wrap it in `tail`/`grep`. As lines stream in, filter mentally (or via the reader's own pattern matching) for these high-signal markers from the dashboard CLI:

    - `Suite:`, `Run started`, `Run completed:`
    - `PASS`, `FAIL`, `Task timed out`, `Sandbox preserved`
    - `Starting iteration`, `Criterion … score:`, `All success criteria`
    - `UiPath CLI build succeeded`, `UiPath CLI login succeeded`
    - `Blob upload complete`, `ADX ingest`, `tasks found`
    - `Traceback`, `ERROR`, `WARNING`

5. Create a task with `TaskCreate` to track the overall run status, and update it via `TaskUpdate` as milestones are hit.
6. Determine completion by watching the background Bash task: when its `TaskGet` status transitions to completed/failed, the run is done. Do **not** rely on a timeout to end the monitoring loop.

## Step 3: Monitor and Report

As monitor events arrive, maintain a running summary for the user:

- When a task completes (passes or fails), update the running tally
- When a task times out, note it and check which task it was
- When upload/ingest events appear, report progress
- **When the background Bash task completes** (detected via `TaskGet` status transitioning to completed/failed, or a task-completion notification): immediately stop the monitor using `TaskStop` with the monitor's task ID, then provide a final summary. Do not use a wall-clock timeout to decide completion.

### Final Summary Format

When the run completes, report:

```
## Dashboard Run Complete

**Run ID**: <run_id>
**Suite**: <suite_name>
**Duration**: <total_time>

### Results
| # | Task | Duration | Score | Result |
|---|------|----------|-------|--------|
| 1 | ... | ... | ... | PASS/FAIL/TIMEOUT |

### Pipeline Status
| Step | Status |
|------|--------|
| Test execution | X/Y passed |
| Analysis | OK / Failed / Skipped |
| Blob upload | Complete → <url> |
| ADX ingest | Complete → X tasks, Y criteria |
```

## Principles

- **Always monitor**: Never fire-and-forget. Always set up a monitor so the user sees live progress.
- **Concise updates**: Don't dump raw logs. Summarize events into a running tally table.
- **Report failures immediately**: If a task times out or fails, tell the user right away with the task name.
- **Final accounting**: When done, always provide the full results table and pipeline status.
- **No silent failures**: If the background command exits with a non-zero code, investigate and report what went wrong.
- **Clean up monitors**: When the background Bash command completes, **always** stop the persistent monitor via `TaskStop` before delivering the final summary. Never leave monitors running after the run is done.
