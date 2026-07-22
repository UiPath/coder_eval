---
description: >-
  Complete coder_eval reference — every CLI command and flag, configuration
  layers and -D overrides, environment variables, run outputs, and reports for
  evaluating AI coding agents.
---

# coder_eval User Guide

The full command, configuration, and output reference. For a gentle introduction
start with the [tutorials](tutorials/README.md); for the task-file schema see the
[Task Definition Guide](TASK_DEFINITION_GUIDE.md).

## Table of Contents

- [CLI Commands](#cli-commands)
- [API Routing & Benchmarking](#api-routing--benchmarking)
- [Output Structure](#output-structure)
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
| `-D path=value` / `--set` | Override any resolved task-config field (`agent`/`run_limits`/`retry`/`sandbox` roots), e.g. `-D run_limits.max_turns=30 -D agent.permission_mode=plan -D agent.sdk_options.effort=high`. Repeatable; schema-validated. This is the way to set permission mode, turn/timeout limits, tools, plugins, and SDK options. |
| `--model, -m` | Shorthand alias for `-D agent.model=…` (e.g., `claude-sonnet-4-20250514`) |
| `--driver` | Shorthand alias for `-D sandbox.driver=…` (`tempdir` or `docker`) |
| `--exclude-tags` | Skip tasks matching any of these tags (comma-separated) |
| `--tags, -t` | Only run tasks matching any of these tags (comma-separated) |
| `--experiment, -e` | Experiment definition YAML for multi-variant comparison (default: `experiments/default.yaml`) |
| `--log-file` | Write logs to file |
| `--backend, -b` | API backend: `direct` or `bedrock` (default: from `API_BACKEND` env var) |
| `--stream, -s` | Stream LLM events to terminal: `full` or `minimal` (disables progress bar) |
| `--verbose, -v` | DEBUG-level logging |

### `coder-eval plan` — validate tasks

```bash
coder-eval plan                # validate all tasks
coder-eval plan tasks/*.yaml   # validate specific tasks
```

Checks task syntax, required CLI tools, API keys, and schema validity without executing.

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
| `--verbose, -v` | DEBUG-level logging |

### `coder-eval report` — view results

```bash
coder-eval report runs/latest              # view latest run
coder-eval report runs/latest -o summary.md  # export to file
```

### Claude Code slash commands

The project ships [Claude Code custom slash commands](https://docs.anthropic.com/en/docs/claude-code/slash-commands)
in `.claude/commands/`, available when using Claude Code in this repository:

| Command | Description |
| --- | --- |
| `/coder-eval-run-analysis <path>` | Analyze evaluation runs and suggest improvements to tasks, config, and prompts. Works at task, variant, or run scope. |
| `/coder-eval-task-create` | Create evaluation task YAML files from a natural language description. |

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

## Environment Variables

Set these in `.env` (copy from `.env.example`).

| Variable | Required | Description |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | Yes (for Claude Code) | Anthropic API key |
| `API_BACKEND` | No | API backend: `direct` or `bedrock` (default: `direct`). Overridden by `--backend`. |
| `AWS_BEARER_TOKEN_BEDROCK` | For Bedrock | AWS Bedrock bearer token for authentication |
| `AWS_REGION` | For Bedrock | AWS region for Bedrock endpoint (e.g., `eu-north-1`) |
| `BEDROCK_MODEL` | No | Cross-region Bedrock model ID (e.g., `eu.anthropic.claude-sonnet-4-5-20250929-v1:0`) |
| `BEDROCK_SMALL_MODEL` | No | Cross-region Bedrock small/fast model ID |
| `UIPATH_PLUGIN_MARKETPLACE_DIR` | No | Base directory for Claude Code plugins (substitutes `$UIPATH_PLUGIN_MARKETPLACE_DIR` in plugin paths) |
| `PLUGIN_TOOLS_DIR` | No | Canonical `node_modules/@uipath` to pin UiPath CLI plugin discovery. When unset, the sandbox auto-derives it from the resolved `uip` binary. |
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
