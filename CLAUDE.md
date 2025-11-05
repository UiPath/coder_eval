# CLAUDE.md - Project Structure Documentation

This document provides a comprehensive overview of the `coder_eval` project structure for AI assistants and developers.

## Project Overview

**Name**: coder_eval
**Version**: 0.1.0
**Purpose**: A robust, extensible framework for evaluating AI coding agents with sandboxing, reproducibility, and data-driven analysis.
**Python Version**: >=3.13
**License**: MIT

### Key Capabilities

1. **Sandbox Management**: Isolated temporary environments with virtual environments
2. **Template System**: Multi-source templates (git repos, local directories, inline files)
3. **Agent Interface**: Abstract agent protocol with Claude Code SDK implementation
4. **Success Criteria**: 9 criterion types with continuous scoring (0.0-1.0)
5. **Snapshot System**: Full/incremental/hybrid sandbox state capture after each iteration
6. **Telemetry**: Detailed command tracking with duration, status, and statistics
7. **LLM Review**: Optional qualitative feedback via UiPath LLM Gateway
8. **Reference Comparison**: Code similarity scoring (AST, token, complexity)
9. **Parallel Execution**: Concurrent task evaluation with asyncio

## Directory Structure

```
coder_eval/
├── pyproject.toml                 # Project config, dependencies, scripts
├── .python-version                # "3.13"
├── .gitignore                     # Python + project artifacts
├── .env.example                   # Environment variable template
├── README.md                      # User documentation
├── CLAUDE.md                      # This file
├── Makefile                       # Development commands
│
├── .github/                       # CI/CD configuration
│   ├── workflows/
│   │   ├── pr-checks.yml          # Quality gate (format, lint, type, test, security)
│   │   └── codeql.yml             # Weekly security analysis
│   ├── pull_request_template.md   # PR template
│   └── dependabot.yml             # Dependency updates
│
├── .pre-commit-config.yaml        # Pre-commit hooks
├── .vscode/                       # VSCode settings
│
├── docs/                          # Documentation
│
├── coder_eval/                    # Main package
│   ├── models.py                  # Pydantic data models (765 lines)
│   ├── config.py                  # Configuration management
│   ├── sandbox.py                 # Sandbox manager (625 lines)
│   ├── agent.py                   # Agent interface (ABC)
│   ├── evaluator.py               # Success checker + LLM reviewer (824 lines)
│   ├── orchestrator.py            # Main evaluation loop (500+ lines)
│   ├── cli.py                     # Command-line interface (400+ lines)
│   ├── utils.py                   # Utility functions
│   ├── logging_config.py          # Logging setup
│   ├── path_utils.py              # Path utilities
│   ├── reports.py                 # Report generation
│   ├── analysis.py                # Command statistics
│   ├── scorers.py                 # Code similarity scorers
│   ├── resources/                 # Package resources
│   └── agents/
│       └── claude_code_agent.py   # Claude Code agent (374 lines)
│
├── tasks/                         # Task definition YAML files
├── tests/                         # Test suite (252 tests, 86% coverage)
├── artifacts/                     # Preserved sandboxes (on-demand)
├── reports/                       # Evaluation results JSON (on-demand)
└── runs/                          # Run directories (on-demand)
```

## Core Modules

### 1. models.py - Data Models (765 lines)

Defines all Pydantic models for type safety and validation.

**Key Model Groups**:

1. **Enums**: `AgentKind`, `AgentState`, `SnapshotMode`
2. **Template Sources**: Discriminated union of `RepoSource`, `TemplateDirSource`, `StarterFilesSource`
3. **Success Criteria**: 9 types (discriminated union):
   - `FileExistsCriterion`, `FileContainsCriterion`, `RunCommandCriterion`
   - `ProgramStdoutEqualsCriterion`, `PytestCriterion`, `FileMatchesRegexCriterion`
   - `CodeLintsCriterion`, `PylintScoreCriterion`, `ReferenceComparisonCriterion`
4. **Sandbox Models**: `SandboxConfig`, `SnapshotConfig`, `SnapshotManifest`
5. **Telemetry Models**: `CommandTelemetry`, `CommandStatistics`, `SlowestCommandInfo`
6. **Results Models**: `CriterionResult`, `LLMDecision`, `FileChange`, `TurnRecord`, `EvaluationResult`
7. **Task Definition**: `TaskDefinition`, `AgentConfig`, `LLMReviewerConfig`, `ReferenceSource`

**Continuous Scoring**:
- All criteria have `weight` (default: 1.0) and `pass_threshold` (default: 0.9)
- Results include `score: float` (0.0-1.0) instead of binary pass/fail
- `EvaluationResult.weighted_score` = Σ(score × weight) / Σ(weight)
- Task succeeds when ALL criteria meet their pass thresholds

**Snapshot System**:
- `SnapshotMode`: DISABLED, FULL, INCREMENTAL, HYBRID
- `SnapshotConfig`: mode, checkpoint_frequency, ignore_patterns
- `SnapshotManifest`: Metadata for each snapshot (iteration, size, file_count, changed_files)

---

### 2. sandbox.py - Sandbox Manager (625 lines)

Manages isolated execution environments with templates and snapshots.

**Key Methods**:
- `setup() -> Path` - Creates sandbox, applies templates, sets up venv
- `cleanup(preserve: bool)` - Removes or preserves sandbox
- `run_command(command, timeout) -> (exit_code, stdout, stderr)`
- `get_file_content()`, `file_exists()`, `list_files()`

**Template System**:
- Sequential application of multiple template sources
- `_apply_repo_source()` - Git clone with optional commit
- `_apply_template_dir_source()` - Recursive copy with ignore patterns
- `_apply_starter_files_source()` - Create inline files from YAML
- Auto-ignores: `.venv`, `.git`, `__pycache__`, `node_modules`, etc.

**Snapshot System** (async):
- `async create_snapshot() -> SnapshotManifest`
- `async _snapshot_full()` - Full copy with `shutil.copytree`
- `async _snapshot_incremental()` - Changed files only
- Uses `asyncio.to_thread()` for non-blocking file I/O

**Virtual Environment**:
- Uses `uv venv` (fallback to stdlib venv)
- Uses `uv pip install` (fallback to pip)

---

### 3. agent.py - Agent Interface (63 lines)

Abstract base class defining the agent contract.

```python
class Agent(ABC):
    async def start(working_directory: str) -> None
    async def communicate(user_input: str) -> TurnRecord
    async def stop() -> None
    def get_state() -> AgentState
```

---

### 4. agents/claude_code_agent.py - Claude Implementation (374 lines)

Implements Agent interface using Claude Agent SDK.

**Two-Phase Command Telemetry**:
1. **Phase 1**: Capture `ToolUseBlock` from `AssistantMessage` → create pending command with start time
2. **Phase 2**: Receive `ResultMessage` → update status (success/error), duration, error message
3. **Phase 3**: Finalize commands → handle missing results as "unknown" status

**File Change Detection**:
- Captures file tree (path → mtime) before/after turn
- Detects created, modified, deleted files
- Ignores `.venv`, `__pycache__`, `.git` directories

---

### 5. evaluator.py - Evaluation System (824 lines)

**SuccessChecker**:
- `check_all(criteria, reference_code) -> list[CriterionResult]`
- Dispatch table maps criterion types to checker methods
- All checkers decorated with `@handle_criterion_errors` for consistent error handling
- Implements 9 criterion types with continuous scoring (0.0-1.0)

**Key Checkers**:
- `_check_file_exists()` - Binary: 1.0 if exists, 0.0 if not
- `_check_file_contains()` - Fractional: average of (includes matched / total) and (excludes absent / total)
- `_check_pytest()` - Fractional: tests_passed / tests_total
- `_check_pylint_score()` - Continuous: pylint_score / 10.0 (normalized to 0.0-1.0)
- `_check_reference_comparison()` - Uses similarity scorers (AST, token, complexity)

**LLMReviewer**:
- Uses UiPath LLM Gateway with LangChain (`LLMGatewayNormalizedChatModel`)
- `review() -> LLMDecision | None`
- Returns: issues, score, next_steps, should_continue

---

### 6. orchestrator.py - Main Controller (500+ lines)

Coordinates the complete evaluation lifecycle.

**Execution Flow**:

1. **Setup**: Create sandbox, install packages, initialize agent, setup snapshot directory
2. **Loop**: For each iteration (up to max_iterations):
   - Send prompt to agent
   - Record turn with file changes
   - **Create snapshot** (if enabled, after each iteration)
   - Check success criteria (score >= pass_threshold for each)
   - If all pass: SUCCESS, exit loop
   - If failed: Generate feedback (LLM or deterministic)
3. **Cleanup**: Stop agent, preserve/cleanup sandbox, save results

**Snapshot Integration**:
- Creates `snapshot_base_dir` during setup if mode != DISABLED
- `_create_iteration_snapshot()` called after each agent turn
- Hybrid mode: full snapshots at checkpoints (iteration % checkpoint_frequency == 0), incremental otherwise

**Feedback Generation**:
- **LLM Mode**: Uses LLM reviewer for qualitative feedback with suggestions
- **Deterministic Mode**: Lists criteria that failed thresholds with scores, errors, and remediation steps

**Batch Execution**:
- `run_batch()` runs multiple tasks in parallel with `asyncio.gather()`
- Configurable concurrency limit via `max_parallel`

---

### 7. cli.py - Command-Line Interface (400+ lines)

Typer-based CLI with Rich terminal output.

**Commands**:
- `run` - Execute evaluation tasks (supports parallel execution)
- `plan` - Validate task files without executing (dry-run)
- `report` - Display or export pre-generated run reports

**Features**:
- Rich terminal output with colors and tables
- Progress spinners during execution
- Markdown report generation
- Logging configuration (verbose mode, log files)

---

### 8. Supporting Modules

**config.py**: Centralized configuration using pydantic-settings
- API keys, paths, defaults
- Environment variable loading from `.env`
- Global `settings` singleton

**utils.py**: Version info utilities
- `get_version_info()` - Captures versions of key dependencies

**logging_config.py**: Structured logging setup
- Task-specific log handlers
- Color-coded output
- Per-task log files

**path_utils.py**: Path and run ID utilities
- `generate_run_id()`, `get_task_run_dir()`, `create_latest_symlink()`

**reports.py**: Report generation
- Markdown report generation
- Load reports from run directories

**analysis.py**: Command statistics calculation
- `calculate_command_statistics()` - Aggregates telemetry from all turns

**scorers.py**: Code similarity scorers
- `SimilarityScorer` - AST and token-based comparison
- `ComplexityScorer` - Cyclomatic complexity comparison
- `QualityScorer` - Type annotations, docstrings, error handling metrics

---

## Data Flow

### Typical Evaluation Flow

```
CLI → Orchestrator → Sandbox + Agent + SuccessChecker + LLMReviewer

Evaluation Loop:
  ├─> Agent.communicate() → TurnRecord (with CommandTelemetry)
  ├─> Create snapshot (if enabled)
  ├─> SuccessChecker.check_all() → List[CriterionResult]
  ├─> LLMReviewer.review() → LLMDecision (if enabled and failed)
  └─> Generate next prompt or exit if success

Cleanup:
  ├─> Calculate weighted score
  ├─> Calculate command statistics
  └─> Save EvaluationResult to reports/
```

---

## Dependencies

### Core Runtime Dependencies

```toml
pydantic>=2.7              # Data validation and models
pydantic-settings>=2.2     # Configuration management
pyyaml>=6.0                # YAML parsing
typer[all]>=0.12           # CLI framework
rich>=13.7                 # Terminal formatting
python-dotenv>=1.0         # Environment variables
anthropic>=0.25            # Anthropic API
claude-agent-sdk>=0.1.1    # Claude Code SDK
anyio>=4.11.0              # Async I/O
uipath>=2.1.78             # UiPath SDK
uipath-llmgw-client>=0.1.33 # LLM Gateway
pylint>=3.3.9              # Code quality (for criteria)
radon>=6.0                 # Complexity metrics
```

### Development Dependencies

```toml
pytest>=8.0                # Testing framework
pytest-asyncio>=1.2.0      # Async test support
pytest-mock>=3.12          # Mocking
pytest-cov>=4.1            # Coverage
mcp>=1.16.0                # MCP server
ruff>=0.14.0               # Linting + formatting
pyright>=1.1.406           # Type checking
pip-audit>=2.6             # Security scanning
bandit[toml]>=1.7          # Security analysis
pre-commit>=3.0            # Pre-commit hooks
```

---

## Configuration Files

### pyproject.toml

**Key sections**:
- `[project]` - Metadata, dependencies, scripts
- `[project.optional-dependencies]` - Dev dependencies (Dependabot compatible)
- `[tool.ruff]` - Line length 120, Python 3.13 target
- `[tool.pyright]` - Type checking (standard mode, selective warnings)
- `[[tool.uv.index]]` - UiPath package index

### .env (user-created from .env.example)

```bash
ANTHROPIC_API_KEY=sk-ant-...
LLMGW_REQUESTING_PRODUCT=agent-gym
LLMGW_REQUESTING_FEATURE=evaluation
LOG_LEVEL=INFO
```

---

## Design Patterns

1. **Strategy Pattern**: Agent interface allows different implementations
2. **Factory Pattern**: `Orchestrator._create_agent()` creates appropriate agent
3. **Builder Pattern**: Complex Pydantic models with validation
4. **Template Method**: `SuccessChecker` with criterion-specific `_check_*` methods
5. **Decorator Pattern**: `@handle_criterion_errors` wraps all checkers
6. **Singleton Pattern**: `settings` global instance
7. **Facade Pattern**: Orchestrator simplifies complex subsystem interactions
8. **Discriminated Union**: Success criteria and template sources

---

## Development Workflow

### Setup

```bash
git clone <repo>
cd coder_eval
uv venv .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env
# Edit .env with API keys
```

### **MANDATORY Verification Steps**

**CRITICAL**: Run after EVERY implementation phase:

```bash
# 1. Format
uv run ruff format coder_eval/ tests/

# 2. Lint
uv run ruff check coder_eval/ tests/

# 3. Type check
uv run pyright

# 4. Test
uv run pytest tests/ -v

# All must pass before proceeding
```

### Makefile Commands

```bash
make install    # Install with dev dependencies + pre-commit hooks
make format     # Auto-format code
make check      # Run linting
make typecheck  # Run type checking
make test       # Run test suite
make test-cov   # Tests with coverage report
make security   # Run security scans (pip-audit + bandit)
make verify     # Run ALL verification steps (CI equivalent)
make clean      # Clean build artifacts
```

### GitHub Actions CI/CD

**PR Quality Checks** (`.github/workflows/pr-checks.yml`):
- Phase 1: Format + lint (fail early)
- Phase 2: Type checking
- Phase 3: Security scanning (pip-audit, bandit)
- Phase 4: Test suite with coverage (≥80% required)
- Single-job strategy, <2 min runtime

**CodeQL Security** (`.github/workflows/codeql.yml`):
- Weekly security analysis
- Also runs on PRs to main/develop

**Pre-commit Hooks** (`.pre-commit-config.yaml`):
- Ruff v0.8.4 (format + lint with auto-fixes)
- Standard checks (whitespace, EOF, YAML, large files, merge conflicts)
- Pyright (manual stage only)

---

## Extension Points

### Adding a New Agent

1. Create class in `agents/` implementing `Agent` ABC
2. Add agent type to `AgentKind` enum
3. Register in `Orchestrator._create_agent()`

### Adding a New Success Criterion

1. Define model inheriting `BaseSuccessCriterion` in `models.py`
2. Add to `SuccessCriterion` union
3. Implement `_check_*` method in `evaluator.py`
4. Add to `SuccessChecker._checkers` dispatch table

### Adding a New Sandbox Driver

1. Update `SandboxConfig.driver` type in `models.py`
2. Implement `_setup_*` method in `sandbox.py`
3. Add to dispatch in `Sandbox.setup()`

---

## Testing Architecture

**252 tests, 86% coverage**

**Test Categories**:
- Models: Pydantic validation, discriminated unions
- Sandbox: Lifecycle, commands, templates, snapshots
- Agent: SDK integration, telemetry, file tracking
- Evaluator: All 9 criterion types, LLM reviewer
- Orchestrator: Evaluation loop, feedback, snapshots
- CLI: Commands, argument parsing
- Utilities: Logging, path utils, reports, analysis, scorers

**Run tests**:
```bash
pytest tests/ -v                  # All tests
pytest tests/test_sandbox.py      # Specific module
pytest --cov=coder_eval tests/    # With coverage
```

---

## Task Definition Format (YAML)

```yaml
task_id: "example_task"
description: "Description of the task"
initial_prompt: "Please implement X"
max_iterations: 3

agent:
  type: "claude-code"
  permission_mode: "acceptEdits"
  allowed_tools: ["Read", "Write", "Bash"]

sandbox:
  driver: "tempdir"
  python_version: "3.13"
  env_packages: ["pytest", "pylint"]

  template_sources:
    - type: "template_dir"
      path: "./templates/python-starter"

  snapshots:
    mode: "hybrid"
    checkpoint_frequency: 5

success_criteria:
  - type: "pytest"
    path: "tests/"
    description: "All tests pass"
    weight: 2.0
    pass_threshold: 0.9

  - type: "pylint_score"
    path: "solution.py"
    description: "Code quality >= 8.5/10"
    weight: 1.5
    pass_threshold: 0.85

llm_reviewer:
  enabled: true
  model: "anthropic.claude-3-5-sonnet-20240620-v1:0"

reference:
  file: "reference_solution.py"
```

---

## Troubleshooting

**Common Issues**:

1. **"ModuleNotFoundError"** - Activate venv: `source .venv/bin/activate && uv pip install -e .`
2. **"claude command not found"** - Install: `brew install claude`
3. **"uv command not found"** - Install: `brew install uv` or `pip install uv`
4. **Agent timeout errors** - Increase `timeout` in sandbox limits or task `max_iterations`

---
## Final notes
- When doing code review, reach out to gemini 2.5 pro, codex and anthropic/claude-sonnet-4.5 through Zen MCP server
- When reaching consensus between model, reach out to gemini 2.5 pro, codex and anthropic/claude-sonnet-4.5 through Zen MCP server
- Any temporary files should be created in `tmp/` folder, NOT `/tmp` folder

## Contact and Support

- **Documentation**: [README.md](README.md), [REFERENCE.md](docs/REFERENCE.md)
- **Testing Guide**: [TESTING_GUIDE.md](docs/TESTING_GUIDE.md)
- **CI/CD**: [GITHUB-HOOKS-V2.md](docs/GITHUB-HOOKS-V2.md)

**Version**: 0.1.0
**Last Updated**: 2025-11-04
**Status**: Production-ready with CI/CD
