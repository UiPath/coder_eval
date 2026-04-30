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
├── reports.py                     # Markdown/JSON report generation (run-level + per-suite rollup via write_suite_rollups)
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
│   ├── enums.py                   # AgentKind, AgentState, SnapshotMode, FinalStatus, ApiBackend
│   ├── criteria.py                # 16 success criterion types + base + union
│   ├── experiment.py              # ExperimentDefinition, ExperimentVariant, ResolvedTask, result models
│   ├── gateway.py                 # DEFAULT_GATEWAY_MODEL constant (cycle-free leaf)
│   ├── mutations.py               # PromptMutation variants (prefix/suffix/replace/template/rephrase)
│   ├── results.py                 # CriterionResult (+ ClassificationCriterionResult), TurnRecord, EvaluationResult, CriterionAggregate, ThresholdCheck, SuiteRollup
│   ├── routing.py                 # ApiRoute (DirectRoute/ProxyRoute/BedrockRoute)
│   ├── sandbox.py                 # SandboxConfig, SnapshotConfig, ResourceLimits
│   ├── tasks.py                   # TaskDefinition, AgentConfig, Dataset (dataset fan-out + sample)
│   ├── telemetry.py               # CommandTelemetry, CommandStatistics, TokenUsage
│   └── templates.py               # RepoSource, TemplateDirSource, StarterFilesSource
│
├── criteria/                      # Criterion checker plugins (one file per type)
│   ├── __init__.py                # CriterionRegistry with auto-discovery
│   ├── base.py                    # BaseCriterion (incl. default aggregate()) + @handle_criterion_errors
│   ├── _classification_aggregate.py  # Shared overlay: accuracy / P/R/F1 / confusion matrix
│   ├── classification_match.py    # File-based label matcher
│   ├── command_executed.py
│   ├── commands_efficiency.py
│   ├── file_check.py
│   ├── file_contains.py
│   ├── file_exists.py
│   ├── file_matches_regex.py
│   ├── import_check.py
│   ├── json_check.py
│   ├── llm_judge.py
│   ├── pylint_score.py
│   ├── pytest_criterion.py
│   ├── reference_comparison.py
│   ├── run_command.py
│   ├── skill_triggered.py         # Binary: did the agent invoke a Skill tool?
│   └── uipath_eval.py
│
├── evaluation/                    # Evaluation orchestration
│   ├── checker.py                 # SuccessChecker (dispatches to criteria/)
│   ├── judge_context.py           # JudgeContextBuilder + shared scrub/truncate/format_details for both judges
│   ├── judge_verdict.py           # parse_judge_verdict + span walker (shared verdict parser)
│   ├── llmgw.py                   # Shared UiPath LLM Gateway client factory
│   ├── sub_agent.py               # SubAgentRunner: sandbox-copy + ClaudeCodeAgent lifecycle for judge-style sub-agents
│   └── summaries.py               # summarize_commands (shared by orchestrator + llm_judge)
│
├── errors/                        # Error handling system
│   ├── agent.py                   # AgentCrashError + format_timeout_reason / truncate_crash_message helpers
│   ├── categories.py              # Error categorization
│   ├── categorization.py          # Error classification logic
│   ├── executor.py                # Execution with error context (+ on_attempt_error hook)
│   ├── retry.py                   # Retry logic with exponential backoff
│   └── timeout.py                 # Timeout handling (TurnTimeoutError carries optional partial TurnRecord)
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
│   ├── proxy_command.py           # `coder-eval proxy`
│   ├── run_helpers.py             # CLI helper functions
│   ├── console.py                 # Rich console instance
│   └── utils.py                   # CLI utilities
│
├── proxy/                         # LLM Gateway proxy (local Anthropic API → LLMGW)
│   ├── auth.py                    # OAuth2 S2S token management
│   ├── config.py                  # ProxyConfig, DEFAULT_MODEL_MAP
│   ├── pricing.py                 # Token cost calculation
│   └── server.py                  # aiohttp proxy server (LLMGatewayProxy)
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
├── simulation/                    # Multi-turn user simulation (dialog-mode evaluation)
│   ├── __init__.py                # Unified exports (UserSimulator, DialogStopReason, evaluate_stop)
│   ├── user_simulator.py          # LLM-driven user simulator (Anthropic + LLMGW backends)
│   └── termination.py             # Dialog-termination predicate + stop-token handling
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
- **Dataset fan-out**: `TaskDefinition.dataset` (inline rows or JSONL path) expands a single task into N row-tasks with `${row.<field>}` substitution in `initial_prompt` and `success_criteria` string fields. Expansion runs in `task_loader.expand_dataset` **before** variant resolution, so variants cannot override the dataset. Row cap: CLI `--sample N` > task-level `dataset.sample`.
- **Per-criterion aggregation**: Each `BaseCriterion` subclass exposes `aggregate(criterion, per_row_results) -> CriterionAggregate | None`. Default emits `count / mean / median / std / min / max` so every criterion is suite-thresholdable for free. Classification-style criteria return `ClassificationCriterionResult` (subclass of `CriterionResult`) and layer accuracy / P/R/F1 / confusion via the shared `overlay_classification_metrics` utility. `BaseSuccessCriterion.suite_thresholds` gates the suite on those metrics; CLI exits non-zero on any gate failure.

## Success Criteria (17 types)

| Type | Scoring | Description |
|------|---------|-------------|
| `file_exists` | Binary | File must exist |
| `file_contains` | Fractional | String presence/absence |
| `file_check` | Fractional | Unified file existence + content + regex check |
| `json_check` | Fractional | JSON validation + JSON Schema + JMESPath assertions |
| `run_command` | Binary / Continuous | Command exit code + optional stdout matching or float scoring |
| `pytest` | Fractional | tests_passed / total |
| `file_matches_regex` | Binary | Regex match on file |
| `pylint_score` | Continuous | pylint score / 10.0 |
| `reference_comparison` | Continuous | AST/token/complexity similarity |
| `command_executed` | Fractional | Agent tool usage verification |
| `commands_efficiency` | Continuous | Agent tool-call efficiency relative to expected budget |
| `import_check` | Fractional | AST-based import extraction + importlib validation |
| `uipath_eval` | Fractional | UiPath agent evaluation results |
| `classification_match` | Binary | File-based label match (observed vs expected) with `(none)`/`(other)` sentinels; emits `ClassificationCriterionResult` for suite-level P/R/F1 |
| `skill_triggered` | Binary | Did the agent invoke a `Skill` tool during the run? Emits `ClassificationCriterionResult` for suite-level P/R/F1 |
| `llm_judge` | Continuous | LLM grades artifacts + optional trajectory + optional reference via UiPath LLM Gateway |
| `agent_judge` | Continuous | Spawns a Claude Code SDK agent in an isolated sandbox copy; judge uses tools (Bash/Read/Grep/…) to investigate and returns a JSON verdict. Expensive; runs with evaluator credentials — see SECURITY note in the criterion docstring. Does not support the PROXY backend (follow-up). |

All criteria support `weight` (default 1.0) and `pass_threshold` (default 0.9). On dataset-backed tasks, criteria may also set `suite_thresholds: {metric: min_value}` — the suite gate passes iff every listed metric (from the criterion's `aggregate()` output) meets its minimum.

## Evaluation Flow

```
CLI → ExperimentRunner (resolve task × variant) → run_batch → Orchestrator → Sandbox + Agent + SuccessChecker

ExperimentRunner resolves configs via 5-layer merge:
  1. experiments/default.yaml  (baseline defaults)
  2. experiment defaults       (experiment-wide defaults)
  3. tasks/<task>.yaml         (task-specific config, wins over defaults)
  4. experiment variant        (variant-specific overrides)
  5. CLI flags                 (always wins)

Per-task (single iteration; simulation mode runs a multi-turn dialog):
  1. Orchestrator._communicate_with_retry(prompt, iteration) → TurnRecord
       (shared by single-shot + simulation paths; wraps
        agent.communicate with execute_with_retry, per-attempt
        turn_timeout, and on_attempt_error → preserves crashed=True
        partial TurnRecords on AgentCrashError / TurnTimeoutError)
  2. Create snapshot (if enabled)
  3. SuccessChecker.check_all() → List[CriterionResult]

Cleanup: Stop agent, save EvaluationResult, generate reports
```

## Development Commands

```bash
# MANDATORY: Run after every implementation phase
make format      # ruff format
make check       # ruff check (lint)
make typecheck   # pyright
make test        # pytest
make lint        # custom architectural lint rules (CE001–CE005)
make verify      # All of the above + coverage check (CI equivalent)
```

When fixing a bug, ask: *could a custom lint rule have prevented this?* If the root cause is a mechanically detectable pattern (e.g., "always import from `coder_eval.models`", "never call blocking IO in async"), add a rule to `tests/lint/rules/` following the CE001–CE005 pattern and wire it up in `tests/lint/runner.py`. This turns a one-time fix into permanent enforcement. See `tests/test_custom_lint.py` for how rules are tested.

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
4. Before raising on any mid-turn failure, set `self.pending_turn` to a
   `crashed=True` `TurnRecord` built from captured telemetry, then raise
   `AgentCrashError` or `TurnTimeoutError` (bare — no payload on the exception).
   The orchestrator's `_on_attempt_failure` callback drains the slot into
   `result.turns` and calls `discard_pending_turn()` to clear it.
5. Override `discard_pending_turn()` to clear `self.pending_turn` and roll back
   any per-turn bookkeeping (e.g. iteration counter). Must be idempotent.
6. Clear `self.pending_turn = None` at the top of `communicate()` (defensive
   reset) and in `stop()` (cleanup).

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
