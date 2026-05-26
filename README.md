# coder_eval ("Coding Agents Gym")

A robust, extensible framework for evaluating AI coding agents with comprehensive sandboxing, reproducibility, and data-driven analysis.

## Features

- **Declarative Tasks** — Define evaluations in YAML with pinned dependencies and clear success criteria
- **Continuous Scoring** — Weighted scoring system (0.0–1.0) with configurable thresholds and fractional credit
- **Sandboxed Execution** — Isolated environments with virtual environments and resource limits
- **Agent Abstraction** — Generic agent interface (currently supports Claude Code, extensible to others)
- **Dual Evaluation** — Objective success criteria plus optional qualitative LLM review
- **14 Criterion Types** — From simple file checks to pytest scoring, pylint analysis, code similarity, and LLM-graded rubrics
- **Command Telemetry** — Full traceability of every tool invocation with timing and status
- **Token Usage Tracking** — Input/output token counts for cost analysis
- **Reference Comparison** — Code similarity scoring using AST, token, and complexity analysis
- **Claude Code Plugins** — Configurable plugin support for Claude Code with marketplace directory substitution
- **Experiment Layer** — Compare agent configurations (models, tools, settings) side-by-side with multi-variant experiments
- **Parallel Execution** — Run multiple evaluations concurrently with configurable parallelism
- **Real-Time Streaming** — `--stream` flag for live LLM event output (tool calls, results, text) with full/minimal verbosity modes
- **Rich CLI** — User-friendly command-line interface with validation, execution, and reporting
- **Standalone Proxy** (`coder-eval proxy`) — Run the LLM Gateway proxy standalone to use `claude` CLI without an Anthropic API key

## Quick Start

### Prerequisites

- **Python 3.13+**
- **Claude CLI** — [install guide](https://docs.anthropic.com/claude/docs/claude-code)
  ```bash
  brew install claude  # macOS
  ```
- **uv 0.8+** — [install guide](https://docs.astral.sh/uv/)
  ```bash
  brew install uv  # macOS, or: pip install uv
  ```
- **UiPath package index credentials** *(optional)* — only needed if you want the `[uipath]` extra (LLMGW judge transport, `rephrase` prompt mutation, in-host `uipath` SDK). Set `UV_INDEX_UIPATH_USERNAME` / `UV_INDEX_UIPATH_PASSWORD` and export them. See the [Agents Gym installation guide](https://github.com/UiPath/agents_gym?tab=readme-ov-file#installation) for setup. Without these, install the framework without `[uipath]`; LLMGW-specific features will fail at dispatch with a clear hint.

### Installation

(currently only tested on Mac)

```bash
git clone https://github.com/UiPath/coder_eval.git
cd coder_eval

uv venv .venv
source .venv/bin/activate

# Install with dev dependencies (recommended)
make install

# Or manually — pick the surface that matches your environment:
uv pip install -e ".[dev]"              # core + dev tools (no UiPath features)
uv pip install -e ".[dev,uipath]"       # + LLMGW judge transport, rephrase, uipath SDK
```

#### Which features need the `[uipath]` extra?

| Feature                                                              | Needs `[uipath]`? |
| -------------------------------------------------------------------- | ----------------- |
| Agent loop, sandbox, all non-LLM criteria                            | no                |
| `llm_judge` via Anthropic SDK (`ANTHROPIC_API_KEY`)                  | no                |
| `llm_judge` via AWS Bedrock                                          | no                |
| `llm_judge` via LLM Gateway transport (`judge_transport="llmgw"`)    | **yes**           |
| LLM Gateway proxy backend (`api_backend=proxy`)                      | no (uses HTTP)    |
| Prompt `rephrase` mutation                                           | **yes**           |
| `uipath_eval` criterion (in-sandbox `uipath` CLI)                    | sandbox-side only |

If you skip `[uipath]`, runs that don't touch LLMGW work unchanged. Runs that do touch it fail at dispatch with a `RuntimeError` pointing back to `pip install 'coder-eval[uipath]'`.

### Configuration

```bash
cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY
```

### Run Your First Evaluation

```bash
# 1. Validate all tasks (dry-run)
coder-eval plan

# 2. Run all evaluations (discovers tasks/ recursively)
coder-eval run

# 3. Run a specific task
coder-eval run tasks/hello_date.yaml

# 4. View results
coder-eval report runs/latest

# 5. Analyze results with Claude Code (diagnose failures, suggest improvements)
claude -p '/coder-eval-run-analysis runs/latest'

# Alternatively: evaluate criteria against a directory without an agent
coder-eval evaluate tasks/hello_date.yaml ./my_solution
```

> **Tip:** Use the `/coder-eval-run-analysis` slash command in Claude Code to get actionable recommendations for improving tasks, config, and prompts based on run results.

## CLI Commands

### `coder-eval run` — Execute Evaluations

```bash
# Run all tasks (discovers tasks/ recursively)
coder-eval run

# Single task
coder-eval run tasks/hello_date.yaml

# Multiple tasks (sequential)
coder-eval run tasks/*.yaml

# Parallel execution (up to 3 concurrent)
coder-eval run tasks/*.yaml --max-parallel 3

# Stream real-time LLM output
coder-eval run tasks/hello_date.yaml --stream full
```

**Options:**

| Flag                         | Description                                                                                   |
| ---------------------------- | --------------------------------------------------------------------------------------------- |
| **Execution**                |                                                                                               |
| `--max-parallel, -j`         | Concurrent tasks (default: 1)                                                                 |
| `--preserve / --no-preserve` | Preserve sandbox after execution (default: preserve)                                          |
| `--run-dir`                  | Custom run directory (default: timestamped in `runs/`)                                        |
| **Agent overrides**          |                                                                                               |
| `--allowed-tools`            | Override allowed tools (comma-separated, e.g., `Read,Write,Bash`)                             |
| `--ignore-patterns`          | Override ignore patterns (comma-separated, e.g., `*.log,__pycache__`)                         |
| `--max-turns`                | Override max agent inner-loop turns per iteration                                             |
| `--model, -m`                | Override agent model for all tasks (e.g., `claude-sonnet-4-20250514`)                         |
| `--permission-mode`          | Override permission mode (`default`, `acceptEdits`, `plan`, `bypassPermissions`)              |
| `--plugins`                  | Override plugins (JSON array, e.g., `'[{"name":"x","path":"/y"}]'`)                           |
| **Timeouts**                 |                                                                                               |
| `--task-timeout`             | Override task timeout in seconds (covers the evaluation loop)                                 |
| `--turn-timeout`             | Override turn timeout in seconds (per agent communicate call)                                 |
| **Filtering**                |                                                                                               |
| `--exclude-tags`             | Skip tasks matching any of these tags (comma-separated)                                       |
| `--tags, -t`                 | Only run tasks matching any of these tags (comma-separated)                                   |
| **Experiments**              |                                                                                               |
| `--experiment, -e`           | Experiment definition YAML for multi-variant comparison (default: `experiments/default.yaml`) |
| **Output & networking**      |                                                                                               |
| `--log-file`                 | Write logs to file                                                                            |
| `--backend, -b`              | API backend: `direct`, `bedrock`, or `proxy` (default: from `API_BACKEND` env var)            |
| `--stream, -s`               | Stream LLM events to terminal: `full` or `minimal` (disables progress bar)                    |
| `--verbose, -v`              | DEBUG-level logging                                                                           |

### `coder-eval plan` — Validate Tasks

```bash
# Validate all tasks (discovers tasks/ recursively)
coder-eval plan

# Validate specific tasks
coder-eval plan tasks/*.yaml
```

Checks task syntax, required CLI tools, API keys, and schema validity without executing.

### `coder-eval evaluate` — Test Criteria Without an Agent

```bash
# Evaluate criteria against a directory
coder-eval evaluate tasks/hello_date.yaml ./my_solution

# Preserve sandbox for debugging
coder-eval evaluate tasks/hello_date.yaml ./my_solution --preserve

# Verbose output
coder-eval evaluate tasks/test.yaml /path/to/code -v
```

Runs all success criteria defined in a task against a directory without requiring an agent. Useful for:

- Testing criterion definitions
- Validating task configurations
- Evaluating code that was already written
- Debugging criteria issues

**Options:**

| Flag                         | Description                                           |
| ---------------------------- | ----------------------------------------------------- |
| `--preserve / --no-preserve` | Preserve sandbox after evaluation (default: preserve) |
| `--verbose, -v`              | DEBUG-level logging                                   |

### `coder-eval report` — View Results

```bash
# View latest run
coder-eval report runs/latest

# Export to file
coder-eval report runs/latest -o summary.md
```

### `coder-eval proxy` — Standalone LLM Gateway Proxy

Starts a local proxy that routes Anthropic API calls through the UiPath LLM Gateway. This lets you use `claude` CLI without an `ANTHROPIC_API_KEY` — the proxy handles OAuth2 authentication transparently.

```bash
# Terminal 1: start proxy
coder-eval proxy --port 8080

# Terminal 2: use claude
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
export ANTHROPIC_API_KEY=llmgw-proxy
claude
```

For scripted / CI usage:

```bash
eval "$(coder-eval proxy --port 8080 -q &)"
sleep 2
claude -p "hello"
```

**Options:**

| Flag | Default | Description |
| -------------- | ----------- | ---------------------------------------------------------- |
| `--port` | `0` (auto) | Port to bind to (`0` = auto-assign a free port) |
| `--env-file` | `.env` | Path to `.env` file with LLM Gateway credentials |
| `--vendor` | `awsbedrock` | Gateway vendor (`awsbedrock`, `anthropic`) |
| `--api-flavor` | `invoke` | Gateway API flavor |
| `--quiet, -q` | `false` | Only print `export` commands to stdout (for `eval` usage) |

Requires `LLMGW_URL`, `LLMGW_CLIENT_ID`, `LLMGW_CLIENT_SECRET`, `LLMGW_SEMANTIC_ORG_ID`, and `LLMGW_SEMANTIC_TENANT_ID` in your `.env` file or environment. See [docs/features/2026-04-03-standalone-proxy-cli.md](docs/features/2026-04-03-standalone-proxy-cli.md) for the full spec.

### Claude Code Slash Commands

The project includes [Claude Code custom slash commands](https://docs.anthropic.com/en/docs/claude-code/slash-commands) in `.claude/commands/` for common workflows:

| Command                           | Description                                                                                                           |
| --------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `/coder-eval-run-analysis <path>` | Analyze evaluation runs and suggest improvements to tasks, config, and prompts. Works at task, variant, or run scope. |
| `/coder-eval-task-create`         | Create evaluation task YAML files from a natural language description.                                                 |

These commands are available when using [Claude Code](https://docs.anthropic.com/en/docs/claude-code) within this repository.

## Task Definition

Tasks are defined in YAML files. Here's a minimal example:

```yaml
task_id: "hello_world"
description: "Create a Python script that prints Hello, World!"
initial_prompt: "Create hello.py that prints 'Hello, World!'"

agent:
  type: "claude-code"
  permission_mode: "acceptEdits"
  allowed_tools: ["Read", "Write", "Bash"]
  plugins:
    - type: "local"
      path: "$UIPATH_PLUGIN_MARKETPLACE_DIR/plugins/my-plugin"

sandbox:
  driver: "tempdir"
  python: {}

success_criteria:
  - type: "file_exists"
    path: "hello.py"
    description: "hello.py must be created"

  - type: "run_command"
    command: "python hello.py"
    timeout: 10
    description: "Script must execute successfully"
```

For the full task definition reference — all 17 criterion types, scoring, templates, and reference comparison — see **[docs/TASK_DEFINITION_GUIDE.md](docs/TASK_DEFINITION_GUIDE.md)**.

> **Tip:** When creating new tasks with Claude Code, point it at the guide:
> _"Read `docs/TASK_DEFINITION_GUIDE.md` and use it as a reference to create a new task definition for ..."_

### Claude Code Agent Configuration

The `agent` section configures the Claude Code behavior. Common options:

```yaml
agent:
  type: "claude-code" # Agent type
  permission_mode: "acceptEdits" # Permission level: default, acceptEdits, plan, bypassPermissions
  allowed_tools: ["Read", "Write", "Bash"] # Tools Claude Code can use
  model: "claude-opus-4-6" # Optional: override model
  max_turns: 5 # Optional: max internal loop turns per iteration
  plugins: # Optional: Claude Code plugins
    - type: "local"
      path: "$UIPATH_PLUGIN_MARKETPLACE_DIR/plugins/mcp" # Plugin path (supports env var substitution)
    - type: "local"
      path: "/absolute/path/to/plugin" # Or absolute path
```

**Plugin Configuration:**

- Set `UIPATH_PLUGIN_MARKETPLACE_DIR` environment variable to enable `$UIPATH_PLUGIN_MARKETPLACE_DIR` substitution in plugin paths
- Each plugin requires `type: "local"` and a `path` to the plugin directory
- Plugin paths support environment variable substitution (e.g., `$UIPATH_PLUGIN_MARKETPLACE_DIR/plugin-name`)

## Experiments (Multi-Variant Comparison)

Every run uses the experiment layer. By default, `experiments/default.yaml` provides baseline agent configuration:

```yaml
# experiments/default.yaml (always loaded)
experiment_id: default
description: "Default experiment - provides baseline agent configuration"

base:
  agent:
    type: claude-code
    permission_mode: acceptEdits
    model: claude-sonnet-4-6-20250514
    max_turns: 3
    turn_timeout: 300
    allowed_tools: ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]

variants:
  - variant_id: default
```

Tasks can omit the `agent` section entirely — defaults are resolved from the experiment layer via a 4-layer merge chain: `default.yaml` → task YAML → experiment base → experiment variant → CLI flags.

To compare configurations, create a custom experiment:

```yaml
# experiments/model-comparison.yaml
experiment_id: model-comparison
description: "Compare Sonnet vs Opus on coding tasks"

base:
  agent:
    type: claude-code
    permission_mode: bypassPermissions

variants:
  - variant_id: sonnet
    agent:
      model: claude-sonnet-4-20250514
  - variant_id: opus
    agent:
      model: claude-opus-4-20250514
```

```bash
# Run all tasks across both variants
coder-eval run -e experiments/model-comparison.yaml -j 4
```

This produces per-task cross-variant comparisons and experiment-level aggregates (win rates, ties, average scores, most divergent tasks). See [docs/features/2026-03-09-experiment-multi-run-configs-design.md](docs/features/2026-03-09-experiment-multi-run-configs-design.md) for the full design.

## API Routing & Benchmarking

`coder-eval` supports three API routing modes, selected via the `--backend` flag or `API_BACKEND` env var:

- **Direct API** (`--backend direct`, default): Calls the Anthropic API directly using your `ANTHROPIC_API_KEY`. This is the standard path with accurate token/cost reporting from the SDK.
- **AWS Bedrock** (`--backend bedrock`): Routes through AWS Bedrock using bearer token authentication. Useful for cross-region model access and organization-managed AWS deployments.
- **LLM Gateway Proxy** (`--backend proxy`): Routes all API traffic through a local proxy that forwards requests to the UiPath LLM Gateway. Useful for testing gateway integration and using organization-managed model access.

```bash
# Direct API (default) — use for official benchmarks
coder-eval run tasks/hello_date.yaml

# Via AWS Bedrock (configure BEDROCK_* vars in .env)
coder-eval run tasks/hello_date.yaml --backend bedrock

# Via LLM Gateway proxy
coder-eval run tasks/hello_date.yaml --backend proxy
```

> **For official benchmarking, always use direct API (`--backend direct`).**
>
> - Proxy adds ~2x latency on simple tasks (S2S auth, extra network hops, per-turn routing)
> - SDK reports zero token/cost usage through the proxy; tokens are estimated by the proxy server instead
> - Task outcomes are not affected, but latency and cost metrics are not comparable across modes
>
> See [docs/features/api-direct-vs-proxy-comparison.md](docs/features/api-direct-vs-proxy-comparison.md) for a detailed analysis.

## Output Structure

```
runs/
├── 2026-02-26_14-30-00/               # Timestamped run directory
│   ├── run.json                      # Run-level summary (tasks, durations, tokens)
│   ├── run.md                        # Run-level markdown report
│   ├── experiment.md                  # Cross-variant comparison report
│   ├── experiment.json                # Full experiment result data
│   ├── experiment.log                 # Aggregated execution log
│   ├── <variant_id>/                  # Per-variant directory
│   │   ├── variant.md                 # Variant aggregate report
│   │   ├── variant.json               # Variant aggregate data
│   │   └── <task_id>/                 # Per-task directory
│   │       └── 00/                    # Replicate index — one dir per replicate (set via `repeats:` or --repeats)
│   │           ├── task.json          # Evaluation result
│   │           ├── task.log           # Execution log
│   │           └── artifacts/         # Preserved sandbox (if --preserve)
│   └── ...
└── latest -> 2026-02-26_14-30-00/     # Symlink to most recent run
```

### Replicates

Run the same (task, variant) N times via `repeats:` in an experiment YAML or `--repeats N` on the CLI. Per-replicate results live in separate `NN/` directories; reports aggregate them with bootstrap confidence intervals and (for 2-variant experiments) a paired mean-difference test. Defaults to 1 (no repetition).

## Architecture

```
coder_eval/
├── models/          # Pydantic data models (7 submodules)
├── criteria/        # Criterion checker plugins (17 types, auto-discovered)
├── evaluation/      # SuccessChecker + llm_judge / agent_judge runners
├── errors/          # Error categorization + retry logic
├── orchestration/   # Batch execution + experiment resolution + task loading
├── cli/             # Typer CLI commands (run, plan, evaluate, report, proxy)
├── scoring/         # Code similarity scorers (AST, token, complexity)
├── streaming/       # Real-time event streaming (callbacks, renderers)
├── agents/          # Agent implementations (Claude Code)
├── proxy/           # LLM Gateway proxy (local Anthropic API → LLMGW)
├── agent.py         # Agent ABC
├── sandbox.py       # Sandbox manager
├── orchestrator.py  # Main evaluation loop
├── config.py        # Settings (pydantic-settings)
├── reports.py       # Report generation
└── ...
```

### Evaluation Flow

1. **Setup**: Create sandbox, install packages, initialize agent
2. **Run**: Send prompt to agent → record actions; check all success criteria
3. **Cleanup**: Stop agent, calculate scores, save results, generate reports

## Development

### Makefile Commands

```bash
make install    # Install package + dev deps + pre-commit hooks
make format     # Auto-format with ruff
make check      # Lint with ruff
make typecheck  # Type check with pyright
make test       # Run test suite
make test-cov   # Tests with coverage report
make verify     # All checks (CI equivalent)
make clean      # Clean build artifacts
make run        # Run all tasks with 8 parallel jobs
```

### Verification

Always run before pushing:

```bash
make verify
```

This runs format check, lint, type check, and tests with 80% coverage threshold — the same checks as CI.

### Running Tests

```bash
pytest tests/                         # All tests
pytest tests/test_sandbox.py          # Specific module
pytest --cov=coder_eval tests/        # With coverage
```

### Pre-commit Hooks

Installed automatically by `make install`. Includes ruff format/lint, trailing whitespace, YAML validation, and large file checks.

## Configuration

### Environment Variables (`.env`)

| Variable                        | Required              | Description                                                                                                  |
| ------------------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------ |
| `ANTHROPIC_API_KEY`             | Yes (for Claude Code) | Anthropic API key                                                                                            |
| `UV_INDEX_UIPATH_USERNAME`      | Yes (for install)     | UiPath package index username                                                                                |
| `UV_INDEX_UIPATH_PASSWORD`      | Yes (for install)     | UiPath package index password (for installing LLMGW client from private index)                               |
| `LLMGW_URL`                     | For llm_judge / proxy | UiPath LLM Gateway URL                                                                                       |
| `LLMGW_CLIENT_ID`               | For llm_judge / proxy | Gateway client ID                                                                                            |
| `LLMGW_CLIENT_SECRET`           | For llm_judge / proxy | Gateway client secret                                                                                        |
| `LLMGW_SEMANTIC_ORG_ID`         | For llm_judge / proxy | Gateway semantic org ID                                                                                      |
| `LLMGW_SEMANTIC_TENANT_ID`      | For llm_judge / proxy | Gateway semantic tenant ID                                                                                   |
| `LLMGW_SEMANTIC_USER_ID`        | For llm_judge / proxy | Gateway semantic user ID                                                                                     |
| `LLMGW_REQUESTING_PRODUCT`      | No                    | Requesting product name (default: `coder-eval`)                                                              |
| `LLMGW_REQUESTING_FEATURE`      | No                    | Requesting feature name (default: `llm-judge`)                                                               |
| `LLMGW_TIMEOUT_SECONDS`         | No                    | Gateway request timeout (default: 290)                                                                       |
| `API_BACKEND`                   | No                    | API backend: `direct`, `bedrock`, or `proxy` (default: `direct`). Overridden by `--backend` CLI flag         |
| `AWS_BEARER_TOKEN_BEDROCK`      | For Bedrock           | AWS Bedrock bearer token for authentication                                                                  |
| `AWS_REGION`                    | For Bedrock           | AWS region for Bedrock endpoint (e.g., `eu-north-1`)                                                         |
| `BEDROCK_MODEL`                 | No                    | Cross-region Bedrock model ID (e.g., `eu.anthropic.claude-sonnet-4-5-20250929-v1:0`)                         |
| `BEDROCK_SMALL_MODEL`           | No                    | Cross-region Bedrock small/fast model ID                                                                     |
| `UIPATH_PLUGIN_MARKETPLACE_DIR` | No                    | Base directory for Claude Code plugins (used to substitute `$UIPATH_PLUGIN_MARKETPLACE_DIR` in plugin paths) |
| `PLUGIN_TOOLS_DIR`              | No                    | Canonical `node_modules/@uipath` to pin UiPath CLI plugin discovery. When unset, the sandbox auto-derives it from the resolved `uip` binary. Operators can override on dedicated eval hosts. Honored by both the agent SDK and criterion subprocesses (MST-9795). |
| `CODER_EVAL_REMEDIATE_HOME_PLUGINS` | No                | **DESTRUCTIVE.** Truthy (`1`/`true`/`yes`, case-insensitive) deletes `$HOME/node_modules/@uipath` at sandbox setup to clear sibling-task pollution on dedicated eval hosts. Off by default; do **not** enable on developer workstations. Refuses to act if `$HOME` resolves to filesystem root or the target escapes `$HOME` (MST-9795). |
| `LOG_LEVEL`                     | No                    | Logging level (default: INFO)                                                                                |
| `LOG_TO_FILE`                   | No                    | Enable file logging (default: false)                                                                         |

See `.env.example` for the full list with default values.

## Extending

### Adding a New Success Criterion

1. Define the data model in `coder_eval/models/criteria.py` (inherit `BaseSuccessCriterion`)
2. Add to the `SuccessCriterion` union type
3. Create a checker in `coder_eval/criteria/` (inherit `BaseCriterion`, use `@register_criterion`)
4. The registry auto-discovers it — no manual wiring needed

### Adding a New Agent

1. Implement the `Agent` ABC in `coder_eval/agents/`
2. Add the agent type to `AgentKind` enum
3. Register in `Orchestrator._create_agent()`

See [CLAUDE.md](CLAUDE.md) for detailed architecture documentation.

## Troubleshooting

| Problem                         | Solution                                                                                                                        |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `ANTHROPIC_API_KEY is required` | Create `.env` from `.env.example` and add your key                                                                              |
| `claude command not found`      | `brew install claude`                                                                                                           |
| `uv command not found`          | `brew install uv` or `pip install uv`                                                                                           |
| `uv pip install` 401 error      | Set `UV_INDEX_UIPATH_USERNAME` and `UV_INDEX_UIPATH_PASSWORD` in `.env`; ensure UiPath Engineering.Cloud Azure group membership |
| Tests failing                   | `source .venv/bin/activate && uv pip install -e ".[dev]"`                                                                       |
| Pre-commit hooks failing        | `pre-commit autoupdate && pre-commit run --all-files`                                                                           |

## Roadmap

- [x] Continuous scoring system
- [x] Command telemetry tracking
- [x] Reference comparison
- [x] Parallel execution
- [x] Token usage tracking
- [x] Real-time LLM streaming output
- [ ] Docker sandbox driver
- [ ] Support for more agents (Aider, Cursor, etc.)
- [ ] Web UI for results visualization
- [x] Comparative analysis reports (experiment layer with multi-variant comparison)

## License

MIT

## Acknowledgments

Built with [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk), [Pydantic](https://pydantic.dev/), [Typer](https://typer.tiangolo.com/), [Rich](https://rich.readthedocs.io/), and [UiPath LLM Gateway](https://uipath.com).
