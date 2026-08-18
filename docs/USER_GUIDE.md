---
description: >-
  Complete Coder Eval reference — every CLI command and flag, configuration
  layers and -D overrides, environment variables, run outputs, and reports for
  evaluating AI coding agents.
---

# Coder Eval User Guide

The full command, configuration, and output reference. For a gentle introduction
start with the [tutorials](tutorials/README.md); for the task-file schema see the
[Task Definition Guide](TASK_DEFINITION_GUIDE.md).

## Table of Contents

- [CLI Commands](#cli-commands)
- [API Routing & Benchmarking](#api-routing--benchmarking)
- [Output Structure](#output-structure)
- [Suite Thresholds & Classification Metrics](#suite-thresholds--classification-metrics)
- [Environment Variables](#environment-variables)
- [Troubleshooting](#troubleshooting)

## CLI Commands

### `coder-eval run` — execute evaluations

```bash
coder-eval run                              # all tasks (discovers tasks/ recursively)
coder-eval run tasks/hello_date.yaml        # a single task
coder-eval run tasks/*.yaml --max-parallel 3  # multiple, 3 concurrent
coder-eval run tasks/hello_date.yaml --stream full  # live LLM output
```

| Flag | Description |
| --- | --- |
| `--max-parallel, -j` | Concurrent tasks (default: 1) |
| `--preservation-mode` | Sandbox persistence: `NONE` / `MOVE_ON_WRITE` / `DIRECT_WRITE`. Default is driver-derived (docker → `DIRECT_WRITE`, else `MOVE_ON_WRITE`); explicit value always wins. |
| `--run-dir` | Custom run directory (default: timestamped in `runs/`) |
| `-D path=value` / `--set` | Override any resolved task-config field (`agent`/`run_limits`/`sandbox` roots), e.g. `-D run_limits.max_turns=30 -D agent.permission_mode=plan -D agent.sdk_options.effort=high`. Repeatable; schema-validated. This is the way to set permission mode, turn/timeout limits, token/USD budget caps, tools, plugins, and SDK options. |
| `--model, -m` | Shorthand alias for `-D agent.model=…` (e.g., `claude-sonnet-5`) |
| `--driver` | Shorthand alias for `-D sandbox.driver=…` (`tempdir` or `docker`) |
| `--type, -T` | Override agent type for all tasks (`claude-code`, `codex`, `antigravity`, or a plugin kind). |
| `--repeats` | Run each `(task, variant)` N times (≥1); overrides experiment/variant `repeats:`. See [Replicates](#replicates). |
| `--resume` | Resume an interrupted run: skip tasks already finalized in `--run-dir` and run the rest, folding prior results into `run.json`. Requires `--run-dir`. A task with *any* final status (incl. FAILED/ERROR) counts as finalized, so resume does **not** retry failures — delete a task's `task.json` to force a re-run. A config mismatch is warned, not refused. |
| `--sample N` | For dataset-backed tasks, run a fixed-seed random N-row sample (reproducible; cheap smoke test). See [Bring Your Own Dataset](DATASETS.md). |
| `--sample-per-stratum N` | For dataset-backed tasks, keep up to N rows per stratum (`stratify_field`). Overridden by `--sample`. Nondeterministic unless `dataset.sample_seed` is set — see [Bring Your Own Dataset](DATASETS.md). |
| `--include-skipped` | Also run tasks marked `skip: true` in their YAML (off by default so CI keeps excluding them). |
| `--exclude-tags` | Skip tasks matching any of these tags (comma-separated) |
| `--tags, -t` | Only run tasks matching any of these tags (comma-separated) |
| `--experiment, -e` | Experiment definition YAML for multi-variant comparison (default: `experiments/default.yaml`) |
| `--log-file` | Write logs to file |
| `--backend, -b` | API backend: `direct` or `bedrock` (default: from `API_BACKEND` env var) |
| `--stream, -s` | Stream LLM events to terminal: `full` or `minimal` (disables progress bar) |
| `--verbose, -v` | DEBUG-level logging |

**Run-time caps** — turns, wall-clock timeouts, and cumulative token / USD budgets — are not CLI
flags of their own. They live under `run_limits:` in the task YAML, or on the command line as
`-D run_limits.<field>=<value>` (e.g. `-D run_limits.max_usd=2.50`). The complete field reference is
in the [Task Definition Guide](TASK_DEFINITION_GUIDE.md#run-limits).

### `coder-eval plan` — validate tasks

```bash
coder-eval plan                # validate all tasks
coder-eval plan tasks/*.yaml   # validate specific tasks
```

Checks task syntax, required CLI tools, API keys, and schema validity without executing.

| Flag | Description |
| --- | --- |
| `--experiment, -e` | Experiment definition YAML to resolve variants against (default: `experiments/default.yaml`). |

### `coder-eval evaluate` — test criteria without an agent

```bash
coder-eval evaluate tasks/hello_date.yaml ./my_solution            # evaluate a directory
coder-eval evaluate tasks/hello_date.yaml ./my_solution --preserve # keep the sandbox
```

Runs a task's success criteria against a directory without an agent — useful for
testing criterion definitions, validating task configs, or scoring code that was
already written.

| Flag | Description |
| --- | --- |
| `--preserve / --no-preserve` | Preserve sandbox after evaluation (default: preserve) |
| `--run-dir` | Custom run directory (default: auto-generated timestamped dir in `runs/`). |
| `--verbose, -v` | DEBUG-level logging |

### `coder-eval report` — view results

```bash
coder-eval report runs/latest                 # view latest run (markdown to stdout)
coder-eval report runs/latest -o summary.md    # export markdown to a file
coder-eval report runs/latest --format html    # (re)render every task.json as task.html
```

The `run` command already writes reports during execution; `report` re-displays or
re-exports them later. For the on-disk layout and field-level schema of the JSON it
reads, see [Output Structure](#output-structure) and the
[Report Schema](REPORT_SCHEMA.md).

| Flag | Description |
| --- | --- |
| `--output, -o` | Write to a file instead of stdout (markdown). |
| `--format, -f` | `md` (default) or `html`. `html` re-renders each `task.json` under the run dir to a `task.html` beside it (or to `-o` when exactly one task is found). |

### `coder-eval aggregate` — rebuild `run.json` from task results

```bash
coder-eval aggregate runs/2026-06-22_14-32-27           # rebuild the summary in place
coder-eval aggregate runs/combined -o runs/combined     # aggregate a merged dir
```

Re-derives the run-level `run.json` + `run.md` from the finalized `task.json` files
already on disk, using the same builder a live run uses. Use it when a run dir's
top-level summary is missing or stale — e.g. after recovering an interrupted run or
combining several run directories. It rebuilds the **run-level summary only**;
per-suite rollups (`suite.json`/`suite.md`) and experiment reports
(`experiment.json`/`experiment.md`) are *not* rebuilt, because the per-row
suite/variant grouping they need is not recoverable from `task.json` alone.

| Flag | Description |
| --- | --- |
| `--output, -o` | Write `run.json`/`run.md` into this directory instead of the run dir (e.g. a merged output dir). |

### Claude Code slash commands

Authoring and analysis commands ship in the [Claude Code plugin](PLUGIN.md), so they work
in **any** repository rather than only this one:

```
/plugin marketplace add UiPath/coder_eval
```

| Command | Description |
| --- | --- |
| `/coder-eval:task` | Create evaluation task YAML files from a natural language description. |
| `/coder-eval:analyze <path>` | Analyze evaluation runs and suggest improvements to tasks, config, and prompts. Works at task, variant, or run scope. |

See [Claude Code Plugin](PLUGIN.md) for the full set of six skills. The
`.claude/commands/` directory in this repository holds contributor-only tooling
(code review, planning) that is deliberately not shipped.

<a id="api-routing--benchmarking"></a>

## API Routing & Benchmarking

`coder-eval` supports two API routing modes, selected via `--backend` or the
`API_BACKEND` env var:

- **Direct API** (`--backend direct`, default) — calls the Anthropic API directly
  using your `ANTHROPIC_API_KEY`. Accurate token/cost reporting from the SDK.
- **AWS Bedrock** (`--backend bedrock`) — routes through AWS Bedrock with bearer
  token auth. Useful for cross-region model access and org-managed AWS deployments.

```bash
coder-eval run tasks/hello_date.yaml                    # direct (default)
coder-eval run tasks/hello_date.yaml --backend bedrock  # via Bedrock (set BEDROCK_* in .env)
```

> **For official benchmarking, use the direct API (`--backend direct`)** for accurate token/cost reporting.

## Output Structure

```
runs/
├── 2026-02-26_14-30-00/               # Timestamped run directory
│   ├── run.json                       # Run-level summary (tasks, durations, tokens)
│   ├── run.md                         # Run-level markdown report
│   ├── experiment.md / .json / .log   # Cross-variant comparison + aggregated log
│   ├── <variant_id>/                  # Per-variant directory
│   │   ├── variant.md / variant.json  # Variant aggregate report + data
│   │   └── <task_id>/
│   │       └── 00/                    # Replicate index — one dir per replicate
│   │           ├── task.json          # Evaluation result
│   │           ├── task.log           # Execution log
│   │           └── artifacts/         # Preserved sandbox (unless --preservation-mode NONE)
│   └── ...
└── latest -> 2026-02-26_14-30-00/     # Symlink to most recent run
```

### Replicates

Run the same (task, variant) N times via `repeats:` in an experiment YAML or
`--repeats N` on the CLI. Per-replicate results live in separate `NN/` directories;
reports aggregate them with bootstrap confidence intervals and (for 2-variant
experiments) a paired mean-difference test. Defaults to 1 (no repetition).

For a field-level reference to `run.json`, `variant.json`, `task.json`, and the
suite/experiment rollups, see the [Report Schema](REPORT_SCHEMA.md).

<a id="suite-thresholds--classification-metrics"></a>

## Suite Thresholds & Classification Metrics

Any criterion on a **dataset-backed** task (see [Bring Your Own Dataset](DATASETS.md))
can gate the whole suite, not just individual rows. Each criterion's `aggregate()`
emits `count / mean / median / std / min / max`, and classification-style criteria
(`classification_match`, `skill_triggered`) additionally emit accuracy, per-label
precision/recall/F1, and a confusion matrix. Add `suite_thresholds` to require a
minimum for any of those metrics — the run exits non-zero if any gate fails:

```yaml
success_criteria:
  - type: skill_triggered
    skill_name: uipath-agents
    expected_skill: "${row.expected_skill}"   # "" for rows where it shouldn't fire
    suite_thresholds:
      recall.yes: 0.70      # fired on ≥70% of rows that needed it
      precision.yes: 0.80   # ≤20% false activations
```

Available metric keys: `accuracy`, `macro_f1`, `weighted_f1`, `micro_f1`, and
per-label `precision.<label>` / `recall.<label>` / `f1.<label>`. This makes
Coder Eval usable as a SkillsBench-style activation harness. Full details and the
suite-rollup outputs live in [A/B Experiments](AB_EXPERIMENTS.md#measuring-the-difference)
and the [Task Definition Guide](TASK_DEFINITION_GUIDE.md).

## Environment Variables

Set these in `.env` (copy from `.env.example`).

| Variable | Required | Description |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | Yes (for Claude Code) | Anthropic API key |
| `API_BACKEND` | No | API backend: `direct` or `bedrock` (default: `direct`). Overridden by `--backend`. |
| `AWS_BEARER_TOKEN_BEDROCK` | For Bedrock | AWS Bedrock bearer token for authentication |
| `AWS_REGION` | For Bedrock | AWS region for Bedrock endpoint (e.g., `eu-north-1`) |
| `BEDROCK_MODEL` | No | Cross-region Bedrock model ID (e.g., `eu.anthropic.claude-sonnet-5`) |
| `BEDROCK_SMALL_MODEL` | No | Cross-region Bedrock small/fast model ID |
| `CODEX_API_KEY` / `CODEX_BASE_URL` / `CODEX_MODEL` / `CODEX_API_VERSION` | For Codex | Codex agent auth & endpoint routing — see [Codex Agent Guide](agents/CODEX.md#endpoint-routing). |
| `GEMINI_API_KEY` / `ANTIGRAVITY_MODEL` | For Antigravity | Antigravity (Gemini) agent auth & model — see [Antigravity Agent Guide](agents/ANTIGRAVITY.md#setup). |
| `UIPATH_PLUGIN_MARKETPLACE_DIR` | No | Conventional base directory for local plugins. Not special-cased by the framework: **any** `$VAR` / `${VAR}` referenced in a plugin `path` is expanded from the environment, so a plugin path like `$UIPATH_PLUGIN_MARKETPLACE_DIR/my-plugin` resolves against this variable. (An undefined variable in a plugin path logs a warning.) |
| `PLUGIN_TOOLS_DIR` | No | A *separate* mechanism from the above: the canonical `node_modules/@uipath` directory used to pin UiPath CLI plugin discovery (not path substitution). When unset, the sandbox auto-derives it from the resolved `uip` binary. |
| `CODER_EVAL_REMEDIATE_HOME_PLUGINS` | No | **DESTRUCTIVE.** Truthy deletes `$HOME/node_modules/@uipath` at sandbox setup to clear sibling-task pollution on dedicated eval hosts. Off by default; do **not** enable on developer workstations. |
| `LOG_LEVEL` | No | Logging level (default: INFO) |
| `LOG_TO_FILE` | No | Enable file logging (default: false) |
| `TELEMETRY_ENABLED` | No | Anonymous usage telemetry, **on by default**. Set `false` to disable entirely — see [Usage Telemetry](#usage-telemetry). |
| `TELEMETRY_CONNECTION_STRING` | No | Route telemetry to your own App Insights resource instead of the shared default (aliases: `APPLICATIONINSIGHTS_CONNECTION_STRING`, `UIPATH_AI_CONNECTION_STRING`) |
| `TELEMETRY_SOURCE` | No | Origin stamp emitted as the `Source` dimension (default: `coder-eval`) |

### Usage Telemetry

`coder-eval` collects **anonymous usage telemetry** (command names, outcomes, counts,
durations, an anonymous per-install id, and platform info) to help improve the tool.
It **never** captures prompts, file contents, or repo paths. Telemetry is **on by
default** and the first run prints a one-time notice to stderr disclosing this.

- **Disable it entirely:** set `TELEMETRY_ENABLED=false` (in `.env` or the environment).
- **Send it to your own resource:** set `TELEMETRY_CONNECTION_STRING` to your Azure
  Application Insights connection string.

## Troubleshooting

| Problem | Solution |
| --- | --- |
| `ANTHROPIC_API_KEY is required` | Create `.env` from `.env.example` and add your key |
| `claude command not found` | `brew install claude` |
| `uv command not found` | `brew install uv` or `pip install uv` |
| Tests failing | `source .venv/bin/activate && uv pip install -e ".[dev]"` |
| Pre-commit hooks failing | `pre-commit autoupdate && pre-commit run --all-files` |
