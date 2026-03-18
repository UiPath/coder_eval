# CLAUDE.md - AI Assistant Guide

Project reference for AI assistants working on the `coder_eval` codebase.

## Project Overview

**coder_eval** is a framework for evaluating AI coding agents with sandboxing, reproducibility, and data-driven analysis.

- **Version**: 0.1.0
- **Python**: >=3.13
- **License**: MIT
- **Entry point**: `coder_eval.cli:app` (command: `coder-eval`)

## Directory Structure

```
coder_eval/
├── agent.py                       # Agent ABC (start, communicate, stop, get_state)
├── config.py                      # Settings via pydantic-settings (.env loading)
├── sandbox.py                     # Sandbox manager (tempdir, venv, templates, snapshots)
├── orchestrator.py                # Main evaluation loop
├── reports.py                     # Markdown/JSON report generation (run-level)
├── reports_experiment.py          # Experiment/cross-variant report generation
├── analysis.py                    # Command statistics aggregation
├── logging_config.py              # Structured logging setup
├── path_utils.py                  # Run ID generation, path utilities
├── utils.py                       # Version info helpers
│
├── agents/
│   └── claude_code_agent.py       # Claude Code SDK agent implementation
│
├── models/                        # Pydantic data models (subpackage)
│   ├── __init__.py                # Unified exports for all models
│   ├── enums.py                   # AgentKind, AgentState, SnapshotMode
│   ├── criteria.py                # 11 success criterion types + base + union
│   ├── experiment.py              # ExperimentDefinition, ExperimentVariant, result models
│   ├── results.py                 # CriterionResult, TurnRecord, EvaluationResult, etc.
│   ├── sandbox.py                 # SandboxConfig, SnapshotConfig, ResourceLimits
│   ├── tasks.py                   # TaskDefinition, AgentConfig (agent optional)
│   ├── telemetry.py               # CommandTelemetry, CommandStatistics, TokenUsage
│   └── templates.py               # RepoSource, TemplateDirSource, StarterFilesSource
│
├── criteria/                      # Criterion checker plugins (one file per type)
│   ├── __init__.py                # CriterionRegistry with auto-discovery
│   ├── base.py                    # BaseCriterion class + @handle_criterion_errors
│   ├── file_exists.py
│   ├── file_contains.py
│   ├── file_check.py
│   ├── json_check.py
│   ├── run_command.py
│   ├── pytest_criterion.py
│   ├── file_matches_regex.py
│   ├── pylint_score.py
│   ├── reference_comparison.py
│   └── command_executed.py
│
├── evaluation/                    # Evaluation orchestration
│   ├── checker.py                 # SuccessChecker (dispatches to criteria/)
│   └── reviewer.py                # LLM reviewer via UiPath LLM Gateway
│
├── errors/                        # Error handling system
│   ├── categories.py              # Error categorization
│   ├── categorization.py          # Error classification logic
│   ├── executor.py                # Execution with error context
│   ├── retry.py                   # Retry logic with exponential backoff
│   └── timeout.py                 # Timeout handling
│
├── orchestration/                 # Batch execution utilities
│   ├── batch.py                   # Parallel task execution (run_batch + run_batch_resolved)
│   ├── config.py                  # Batch run configuration
│   ├── evaluation.py              # Evaluation helpers
│   ├── experiment.py              # ExperimentRunner, resolve_task_for_variant, load_experiment
│   └── task_loader.py             # YAML task loading
│
├── cli/                           # CLI commands (Typer + Rich)
│   ├── __init__.py                # Typer app setup (core + tools sub-app)
│   ├── run_command.py             # `coder-eval run`
│   ├── plan_command.py            # `coder-eval plan`
│   ├── report_command.py          # `coder-eval report`
│   ├── run_helpers.py             # CLI helper functions
│   ├── console.py                 # Rich console instance
│   └── utils.py                   # CLI utilities
│
├── tools/                         # Optional authoring utilities (not part of eval loop)
│   └── autogen/                   # Task generation from Claude Code plugin skill definitions
│       ├── config.py              # AutogenConfig (Pydantic model)
│       ├── generator.py           # LLM-based task + experiment generation
│       ├── validator.py           # Pydantic validation gate for generated tasks
│       └── cli.py                 # `coder-eval tools autogen` command
│
├── scoring/                       # Code similarity scoring
│   ├── ast_similarity.py          # AST-based comparison
│   ├── token_similarity.py        # Token-based comparison
│   ├── signature_similarity.py    # Function signature comparison
│   ├── complexity.py              # Cyclomatic complexity comparison
│   ├── quality.py                 # Quality metrics (annotations, docstrings)
│   └── similarity.py              # Unified similarity interface
│
├── streaming/                     # Real-time LLM event streaming
│   ├── __init__.py                # Unified exports
│   ├── callbacks.py               # StreamCallback protocol, TaskScopedCallback, safe_emit
│   ├── events.py                  # StreamEvent types (TurnStart, ToolCall, ToolResult, etc.)
│   └── renderers.py               # RichStreamRenderer (full/minimal verbosity, batch mode)
│
└── resources/                     # Package resources

experiments/                        # Experiment definition YAML files
tasks/                             # Task definition YAML files
tests/                             # Test suite
docs/                              # Documentation
templates/                         # Sandbox template directories
```

## Key Architectural Patterns

- **Discriminated Unions**: Criteria types and template sources use Pydantic discriminated unions
- **Plugin Registry**: `criteria/` uses auto-discovery via `pkgutil` + `@register_criterion` decorator
- **Strategy Pattern**: `Agent` ABC with implementations in `agents/`
- **Separation of Concerns**: Data models (`models/`) are pure Pydantic; logic lives in `criteria/`, `evaluation/`, etc.
- **Callback Streaming**: `StreamCallback` protocol with `TaskScopedCallback` wrapper for real-time LLM event output
- **Experiment Layer**: Pre-processing config resolver (`ExperimentRunner`) that resolves task × variant combinations via 5-layer merge (default → experiment defaults → task → variant → CLI) before passing to `run_batch`
- **All core models importable from `coder_eval.models`** regardless of submodule (`AutogenConfig` lives in `coder_eval.tools.autogen.config` — it's not a core model)

## Success Criteria (12 types)

| Type | Scoring | Description |
|------|---------|-------------|
| `file_exists` | Binary | File must exist |
| `file_contains` | Fractional | String presence/absence |
| `file_check` | Fractional | Unified file existence + content + regex check |
| `json_check` | Fractional | JSON validation + JSON Schema + JMESPath assertions |
| `run_command` | Binary | Command exit code + optional stdout matching |
| `pytest` | Fractional | tests_passed / total |
| `file_matches_regex` | Binary | Regex match on file |
| `pylint_score` | Continuous | pylint score / 10.0 |
| `reference_comparison` | Continuous | AST/token/complexity similarity |
| `command_executed` | Fractional | Agent tool usage verification |
| `import_check` | Fractional | AST-based import extraction + importlib validation |
| `uipath_eval` | Fractional | UiPath agent evaluation results |

All criteria support `weight` (default 1.0) and `pass_threshold` (default 0.9).

## Evaluation Flow

```
CLI → ExperimentRunner (resolve task × variant) → run_batch → Orchestrator → Sandbox + Agent + SuccessChecker

ExperimentRunner resolves configs via 5-layer merge:
  1. experiments/default.yaml  (baseline defaults)
  2. experiment defaults       (experiment-wide defaults)
  3. tasks/<task>.yaml         (task-specific config, wins over defaults)
  4. experiment variant        (variant-specific overrides)
  5. CLI flags                 (always wins)

Per-task loop (up to max_iterations):
  1. Agent.communicate(prompt) → TurnRecord
  2. Create snapshot (if enabled)
  3. SuccessChecker.check_all() → List[CriterionResult]
  4. All pass? → SUCCESS. Otherwise → generate feedback → next iteration

Cleanup: Stop agent, save EvaluationResult, generate reports
```

## Development Commands

```bash
# MANDATORY: Run after every implementation phase
make format      # ruff format
make check       # ruff check (lint)
make typecheck   # pyright
make test        # pytest
make verify      # All of the above + coverage check (CI equivalent)
```

## Configuration

- **ruff**: line-length=120, target py313, select E/F/I/N/W/UP/B/SIM/RUF
- **pyright**: standard mode, includes `coder_eval/` only, excludes tests
- **pytest**: asyncio_mode=auto, strict markers, coverage source=coder_eval
- **Coverage threshold**: 80% (enforced in CI)

## Extension Points

### Adding a New Criterion

1. Define model in `models/criteria.py` inheriting `BaseSuccessCriterion`
2. Add to `SuccessCriterion` union type
3. Create checker file in `criteria/` inheriting `BaseCriterion`
4. Use `@register_criterion` decorator — auto-discovered at runtime

### Adding a New Agent

1. Implement `Agent` ABC in `agents/`
2. Add to `AgentKind` enum in `models/enums.py`
3. Register in `Orchestrator._create_agent()`

## Task Definition

Tasks are YAML files. See [docs/TASK_DEFINITION_GUIDE.md](docs/TASK_DEFINITION_GUIDE.md) for the full reference.

## Dependencies

**Runtime**: pydantic, pydantic-settings, pyyaml, typer, rich, python-dotenv, anthropic, claude-agent-sdk, anyio, uipath, uipath-llmgw-client, pylint, radon

**Dev**: pytest, pytest-asyncio, pytest-mock, pytest-cov, ruff, pyright, pip-audit, bandit, pre-commit, mcp

## Design Principles

- **DRY (Don't Repeat Yourself)**: Field descriptions, validation rules, and documentation defined once in Pydantic models
- **Single Source of Truth**: Schema models are the authoritative source for parameter definitions
- **Type Safety**: Full type checking with Pydantic and Pyright
- **YAGNI**: Don't add complexity until actually needed
- **KISS**: Keep it simple, stupid!
- **Clean Code**: No dead code, all imports used, all tests passing
- **Greenfield project**: No worries about backward compatibility

## Notes for AI Assistants

- When doing code review, reach out to gemini-3 and codex through multi mcp server
- Any temporary files should be created in `tmp/` folder, NOT `/tmp` folder
- All models are importable from `coder_eval.models` — don't import from submodules directly
- The `criteria/` package uses auto-discovery; new checkers just need the `@register_criterion` decorator
