# coder_eval

A robust, extensible framework for evaluating AI coding agents with comprehensive sandboxing, reproducibility, and data-driven analysis.

## Features

- **Declarative Tasks** — Define evaluations in YAML with pinned dependencies and clear success criteria
- **Continuous Scoring** — Weighted scoring system (0.0–1.0) with configurable thresholds and fractional credit
- **Sandboxed Execution** — Isolated environments with virtual environments and resource limits
- **Sandbox Snapshots** — Capture complete sandbox state after each iteration for debugging and analysis
- **Agent Abstraction** — Generic agent interface (currently supports Claude Code, extensible to others)
- **Dual Evaluation** — Objective success criteria plus optional qualitative LLM review
- **10 Criterion Types** — From simple file checks to pytest scoring, pylint analysis, and code similarity
- **Command Telemetry** — Full traceability of every tool invocation with timing and status
- **Token Usage Tracking** — Input/output token counts for cost analysis
- **Reference Comparison** — Code similarity scoring using AST, token, and complexity analysis
- **Parallel Execution** — Run multiple evaluations concurrently with configurable parallelism
- **Rich CLI** — User-friendly command-line interface with validation, execution, and reporting

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

### Installation

```bash
git clone <repo-url>
cd coder_eval

uv venv .venv
source .venv/bin/activate

# Install with dev dependencies (recommended)
make install

# Or manually
uv pip install -e ".[dev]"
```

### Configuration

```bash
cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY
```

### Run Your First Evaluation

```bash
# 1. Validate a task (dry-run)
coder-eval plan tasks/hello_date.yaml

# 2. Run the evaluation
coder-eval run tasks/hello_date.yaml

# 3. View results
coder-eval report runs/latest
```

## CLI Commands

### `coder-eval run` — Execute Evaluations

```bash
# Single task
coder-eval run tasks/hello_date.yaml

# Multiple tasks (sequential)
coder-eval run tasks/*.yaml

# Parallel execution (up to 3 concurrent)
coder-eval run tasks/*.yaml --max-parallel 3
```

**Options:**

| Flag | Description |
|------|-------------|
| `--max-iter, -i` | Override max iterations for all tasks |
| `--preserve / --no-preserve` | Preserve sandbox after execution (default: preserve) |
| `--run-dir` | Custom run directory (default: timestamped in `runs/`) |
| `--max-parallel, -j` | Concurrent tasks (default: 1) |
| `--snapshot-mode` | Override snapshot mode (`disabled`, `full`, `incremental`, `hybrid`) |
| `--snapshot-checkpoint-freq` | Checkpoint frequency for hybrid mode |
| `--verbose, -v` | DEBUG-level logging |
| `--log-file` | Write logs to file |

### `coder-eval plan` — Validate Tasks

```bash
coder-eval plan tasks/*.yaml
```

Checks task syntax, required CLI tools, API keys, and schema validity without executing.

### `coder-eval report` — View Results

```bash
# View latest run
coder-eval report runs/latest

# Export to file
coder-eval report runs/latest -o summary.md
```

## Task Definition

Tasks are defined in YAML files. Here's a minimal example:

```yaml
task_id: "hello_world"
description: "Create a Python script that prints Hello, World!"
initial_prompt: "Create hello.py that prints 'Hello, World!'"
max_iterations: 2

agent:
  type: "claude-code"
  permission_mode: "acceptEdits"
  allowed_tools: ["Read", "Write", "Bash"]

sandbox:
  driver: "tempdir"
  python_version: "3.13"

success_criteria:
  - type: "file_exists"
    path: "hello.py"
    description: "hello.py must be created"

  - type: "run_command"
    command: "python hello.py"
    timeout: 10
    description: "Script must execute successfully"
```

For the full task definition reference — all 10 criterion types, scoring, templates, snapshots, LLM reviewer, and reference comparison — see **[docs/TASK_DEFINITION_GUIDE.md](docs/TASK_DEFINITION_GUIDE.md)**.

> **Tip:** When creating new tasks with Claude Code, point it at the guide:
> *"Read `docs/TASK_DEFINITION_GUIDE.md` and use it as a reference to create a new task definition for ..."*

## Output Structure

```
runs/
├── 2026-02-26_14-30-00/           # Timestamped run directory
│   ├── run-report.md              # Human-readable markdown report
│   ├── run-summary.json           # Aggregated statistics
│   ├── task_id/
│   │   ├── report.json            # Task evaluation result
│   │   ├── task.log               # Task execution log
│   │   ├── snapshots/             # Iteration snapshots (if enabled)
│   │   └── artifacts/             # Preserved sandbox (if --preserve)
│   └── ...
└── latest -> 2026-02-26_14-30-00/ # Symlink to most recent run
```

## Architecture

```
coder_eval/
├── models/          # Pydantic data models (7 submodules)
├── criteria/        # Criterion checker plugins (10 types, auto-discovered)
├── evaluation/      # SuccessChecker + LLM reviewer
├── errors/          # Error categorization + retry logic
├── orchestration/   # Batch execution + task loading
├── cli/             # Typer CLI commands
├── scoring/         # Code similarity scorers (AST, token, complexity)
├── agents/          # Agent implementations (Claude Code)
├── agent.py         # Agent ABC
├── sandbox.py       # Sandbox manager
├── orchestrator.py  # Main evaluation loop
├── config.py        # Settings (pydantic-settings)
├── reports.py       # Report generation
└── ...
```

### Evaluation Flow

1. **Setup**: Create sandbox, install packages, initialize agent
2. **Loop** (up to `max_iterations`):
   - Send prompt to agent → record actions and file changes
   - Create snapshot (if enabled)
   - Check all success criteria
   - If all pass → SUCCESS; otherwise → generate feedback → next iteration
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

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes (for Claude Code) | Anthropic API key |
| `LLMGW_URL` | For LLM reviewer | UiPath LLM Gateway URL |
| `LLMGW_CLIENT_ID` | For LLM reviewer | Gateway client ID |
| `LLMGW_CLIENT_SECRET` | For LLM reviewer | Gateway client secret |
| `LOG_LEVEL` | No | Logging level (default: INFO) |
| `LOG_TO_FILE` | No | Enable file logging (default: false) |

See `.env.example` for the full list.

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

| Problem | Solution |
|---------|----------|
| `ANTHROPIC_API_KEY is required` | Create `.env` from `.env.example` and add your key |
| `claude command not found` | `brew install claude` |
| `uv command not found` | `brew install uv` or `pip install uv` |
| `uv pip install` 401 error | Ensure UiPath Engineering.Cloud Azure group membership |
| Tests failing | `source .venv/bin/activate && uv pip install -e ".[dev]"` |
| Pre-commit hooks failing | `pre-commit autoupdate && pre-commit run --all-files` |

## Roadmap

- [x] Continuous scoring system
- [x] Snapshot system
- [x] Command telemetry tracking
- [x] Reference comparison
- [x] Parallel execution
- [x] Token usage tracking
- [ ] Docker sandbox driver
- [ ] Support for more agents (Aider, Cursor, etc.)
- [ ] Web UI for results visualization
- [ ] Comparative analysis reports

## License

MIT

## Acknowledgments

Built with [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk), [Pydantic](https://pydantic.dev/), [Typer](https://typer.tiangolo.com/), [Rich](https://rich.readthedocs.io/), and [UiPath LLM Gateway](https://uipath.com).
