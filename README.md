coder_eval
==========

A robust, extensible framework for evaluating AI coding agents with comprehensive sandboxing, reproducibility, and data-driven analysis.

## Table of Contents

- [Features](#features)
- [Quick Start](#quick-start)
  - [Installation](#installation)
  - [Configuration](#configuration)
  - [Running Your First Evaluation](#running-your-first-evaluation)
- [Task Definition](#task-definition)
- [Task Templates](#task-templates)
- [Continuous Scoring](#continuous-scoring)
- [Sandbox Snapshots](#sandbox-snapshots)
- [Success Criteria Types](#success-criteria-types)
- [LLM Reviewer](#llm-reviewer-optional)
- [CLI Commands](#cli-commands)
- [Development](#development)
- [Architecture](#architecture)
- [Configuration](#configuration-1)
- [Troubleshooting](#troubleshooting)
- [Roadmap](#roadmap)
- [License](#license)

## Features

- **Declarative Tasks**: Define tasks in YAML with pinned dependencies and clear success criteria
- **Continuous Scoring**: Weighted scoring system with configurable thresholds and fractional credit for partial success
- **Hardened Sandboxing**: Isolated execution environments with resource limits and virtual environments
- **Sandbox Snapshots**: Capture complete sandbox state after each iteration for debugging and analysis
- **Agent Abstraction**: Generic agent interface supporting multiple coding agents (Claude Code, and extensible to others)
- **Dual Evaluation**: Objective success criteria plus optional qualitative LLM review
- **Comprehensive Tracking**: Full traceability of every action, log, and file change with detailed telemetry
- **Reference Comparison**: Code similarity scoring using AST, token, and complexity analysis
- **CLI-First**: User-friendly command-line interface with validation, execution, and reporting

## Quick Start

### Installation

#### Prerequisites

- **Python 3.13+** - Required for the framework
- **Claude CLI** - Required for Claude Code agent ([install guide](https://docs.anthropic.com/claude/docs/claude-code))
  ```bash
  brew install claude  # macOS
  ```
- **uv 0.8+** - Fast Python package manager ([install guide](https://docs.astral.sh/uv/))
  ```bash
  brew install uv  # macOS, or: pip install uv
  ```
- **Private Index Access** - For LLM Gateway client installation, you need to be a member of:
  - Azure Group: [UiPath Engineering.Cloud](https://portal.azure.com/#view/Microsoft_AAD_IAM/GroupDetailsMenuBlade/~/Overview/groupId/a029832f-e86f-4e6e-abbf-6afcfec5c778/menuId/)
  - This provides access to the private PyPI index for `uipath_llmgw_client`

#### Install Steps

```bash
# Clone the repository
git clone <repo-url>
cd coder_eval

# Create virtual environment (optional but recommended)
uv venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install package with development dependencies
uv pip install -e ".[dev]"

# Or use Makefile (recommended for development)
make install  # Installs package + sets up pre-commit hooks
```

**Note**: The project recently migrated from `uv sync` with `dependency-groups` to `uv pip install` with `optional-dependencies` for better Dependabot compatibility.

### Configuration

1. Copy the example environment file:
```bash
cp .env.example .env
```

2. Edit `.env` and add your API keys:
```bash
ANTHROPIC_API_KEY=your_key_here
```

### Running Your First Evaluation

1. Validate a task (dry-run):
```bash
coder-eval plan tasks/hello_date.yaml
```

2. Run the evaluation:
```bash
coder-eval run tasks/hello_date.yaml
```

3. View the results:
```bash
# Use the report command to view the markdown report
coder-eval report runs/latest

# Or view files directly
cat runs/latest/run-report.md

# Or view the JSON summary
cat runs/latest/run-summary.json
```

## Task Definition

Tasks are defined in YAML files. Here's a simple example:

```yaml
task_id: "hello_world"
description: "Create a Python script that prints Hello, World!"
initial_prompt: "Create a Python file named hello.py that prints 'Hello, World!'"
max_iterations: 2

agent:
  type: "claude-code"
  permission_mode: "acceptEdits"
  allowed_tools: ["Read", "Write", "Bash"]

sandbox:
  driver: "tempdir"
  python_version: "3.13"
  env_packages: []

success_criteria:
  - type: "file_exists"
    path: "hello.py"
    description: "The file hello.py must be created."

  - type: "run_command"
    command: "python hello.py"
    timeout: 10
    description: "The script must execute successfully."
```

## Task Templates

Tasks can start with preset files instead of an empty sandbox, making it easier to evaluate agents on existing codebases or with starter code.

### Template Sources

You can use **multiple template sources sequentially** (applied in order):

#### 1. Git Repository

Clone a git repository as the base:

```yaml
sandbox:
  driver: tempdir
  python_version: "3.13"
  template_sources:
    - type: "repo"
      url: "https://github.com/user/repo.git"
      commit: "abc123"  # Optional: pin to specific commit
```

#### 2. Template Directory

Copy a local directory into the sandbox:

```yaml
sandbox:
  driver: tempdir
  python_version: "3.13"
  template_sources:
    - type: "template_dir"
      path: "./templates/python-starter"
  env_packages:
    - pytest
```

The agent will start with all files from the template directory already in the sandbox. The framework automatically ignores `.venv`, `.git`, `__pycache__`, and other common artifacts.

**Example**: See [tasks/fibonacci_with_template.yaml](tasks/fibonacci_with_template.yaml)

#### 3. Inline Starter Files

Define files directly in the task YAML:

```yaml
sandbox:
  driver: tempdir
  python_version: "3.13"
  template_sources:
    - type: "starter_files"
      files:
        - path: "README.md"
          content: |
            # My Project
            Instructions for the agent...
        - path: "src/main.py"
          content: |
            def main():
                # TODO: Implement
                pass
```

This is ideal for simple tasks with 1-3 files.

**Example**: See [tasks/inline_starter_example.yaml](tasks/inline_starter_example.yaml)

#### 4. Multiple Sequential Sources

Apply multiple template sources in sequence (last wins for conflicts):

```yaml
sandbox:
  driver: tempdir
  python_version: "3.13"
  template_sources:
    # Start with base template
    - type: "template_dir"
      path: "./templates/python-base"
    # Add task-specific files
    - type: "starter_files"
      files:
        - path: "requirements.txt"
          content: "pytest>=8.0\npylint>=3.0"
```

**Note**: If using `repo` source, it must be first in the list.

### Best Practices

- **Local development**: Use `template_dir` for quick iteration on templates
- **Simple tasks**: Use `starter_files` for 1-3 files defined inline
- **Existing codebases**: Use `repo` for real repositories
- **Relative paths**: Template directory paths are resolved relative to the task YAML file
- **Multiple sources**: Combine sources for flexibility (base template + task-specific files)

### Template Examples

The framework includes example templates in the `templates/` directory:

- `templates/python-starter/`: A basic Python project with source and test structure

## Continuous Scoring

The framework supports **weighted continuous scoring** to provide nuanced evaluation beyond pass/fail:

### Key Features

- **Weighted Criteria**: Assign importance to each criterion with the `weight` field (default: 1.0)
- **Pass Thresholds**: Set minimum scores required with `pass_threshold` (default: 0.9 = 90%)
- **Fractional Scoring**: Get partial credit for partial success (e.g., 7/10 tests = 0.7 score)
- **Weighted Score**: Aggregate quality metric calculated as `Σ(score × weight) / Σ(weight)`

### Example with Weights and Thresholds

```yaml
success_criteria:
  - type: "pytest"
    path: "tests/"
    description: "Core tests must pass"
    weight: 3.0              # Critical - 3x importance
    pass_threshold: 1.0      # Must be perfect (100%)

  - type: "file_contains"
    path: "README.md"
    includes: ["Installation", "Usage"]
    description: "Documentation should be present"
    weight: 1.0              # Standard importance
    pass_threshold: 0.7      # 70% is acceptable (one of two sections)
```

### Scoring Behavior

**Binary Scoring** (1.0 or 0.0):
- `file_exists`, `run_command`, `program_stdout_equals`

**Fractional Scoring** (0.0 to 1.0):
- `file_contains`: Average of (includes found / total) and (excludes absent / total)
- `pytest`: (tests passed / total tests)
- `pylint_score`: (pylint score / 10.0)

**Task Pass/Fail**:
- Task succeeds only when **all** criteria score >= their `pass_threshold`
- `weighted_score` is calculated regardless for quality assessment
- Results show both binary SUCCESS/FAILURE and continuous weighted_score

## Sandbox Snapshots

The framework can automatically capture the complete state of the sandbox after each agent iteration, enabling detailed debugging, progress tracking, and failure analysis.

### Snapshot Modes

Configure snapshots in your task YAML:

```yaml
sandbox:
  driver: "tempdir"
  python_version: "3.13"

  snapshots:
    mode: "hybrid"              # disabled, full, incremental, or hybrid
    checkpoint_frequency: 5     # Full snapshot every N iterations (hybrid mode only)
    ignore_patterns:            # Optional: patterns to exclude from snapshots
      - "*.log"
      - "temp_*"
      - "__pycache__"
```

**Available Modes**:

- **`disabled`** (default): No snapshots created, minimal disk usage
- **`full`**: Complete sandbox copy after every iteration (highest disk usage, easiest debugging)
- **`incremental`**: Only copy changed files (storage-efficient, requires tracking file changes)
- **`hybrid`**: Full snapshots at checkpoints (every N iterations), incremental between checkpoints (balanced approach)

### CLI Overrides

Override snapshot settings for all tasks via command line:

```bash
# Enable full snapshots for all tasks
coder-eval run tasks/*.yaml --snapshot-mode full

# Enable hybrid mode with checkpoints every 3 iterations
coder-eval run tasks/*.yaml --snapshot-mode hybrid --snapshot-checkpoint-freq 3

# Disable snapshots for all tasks (override task YAML)
coder-eval run tasks/*.yaml --snapshot-mode disabled
```

**Note**: CLI overrides apply globally but preserve task-specific `ignore_patterns`.

### Snapshot Directory Structure

Snapshots are stored in the run directory:

```
runs/2025-10-20_14-30-00/
├── hello_world/
│   ├── snapshots/
│   │   ├── iteration_1/          # Incremental snapshot
│   │   │   ├── app.py
│   │   │   └── manifest.json
│   │   ├── iteration_2/          # Full checkpoint snapshot
│   │   │   ├── app.py
│   │   │   ├── tests/
│   │   │   │   └── test_app.py
│   │   │   └── manifest.json
│   │   └── iteration_3/          # Incremental snapshot
│   │       ├── app.py
│   │       └── manifest.json
│   ├── result.json
│   └── task.log
```

Each snapshot includes a `manifest.json` with metadata:

```json
{
  "created_at": "2025-10-20T14:30:15.123456",
  "iteration": 2,
  "mode": "full",
  "size_bytes": 4096,
  "file_count": 3,
  "changed_files": ["app.py"],
  "base_iteration": null
}
```

### Use Cases

- **Debugging Failures**: Examine exact sandbox state when task failed
- **Progress Tracking**: See agent's incremental changes across iterations
- **Regression Analysis**: Compare snapshots to identify when bugs were introduced
- **Training Data**: Capture agent behavior for model improvement
- **Audit Trail**: Complete forensic record of all file changes

### Best Practices

- **Development**: Use `mode: full` for maximum visibility while debugging tasks
- **Production**: Use `mode: hybrid` with `checkpoint_frequency: 5` for balanced storage/debugging
- **CI/CD**: Use `mode: disabled` or `--snapshot-mode disabled` to minimize disk usage
- **Large Codebases**: Add comprehensive `ignore_patterns` to exclude dependencies, logs, build artifacts
- **Storage Planning**: Full snapshots can use significant disk space; monitor `runs/` directory size

### Example: Snapshot with Ignore Patterns

```yaml
sandbox:
  driver: "tempdir"
  python_version: "3.13"
  env_packages:
    - pytest

  snapshots:
    mode: "hybrid"
    checkpoint_frequency: 2
    ignore_patterns:
      - "*.log"          # Ignore all log files
      - "*.pyc"          # Ignore compiled Python
      - "temp_*"         # Ignore temporary files
      - "build/"         # Ignore build directory
      - "dist/"          # Ignore distribution directory
      - ".pytest_cache/" # Ignore pytest cache
```

**Note**: The framework automatically excludes `.venv`, `.git`, and `__pycache__` from all snapshots.

## Success Criteria Types

The framework supports 9 criterion types for evaluating agent performance:

### `file_exists`
Checks if a file exists at the specified path.

```yaml
- type: "file_exists"
  path: "app.py"
  description: "The file app.py must exist."
  weight: 1.0
  pass_threshold: 1.0
```

### `file_contains`
Checks if a file contains (or doesn't contain) specific strings. Supports fractional scoring.

```yaml
- type: "file_contains"
  path: "app.py"
  includes: ["Hello", "import"]
  excludes: ["TODO", "FIXME"]
  description: "The file must contain required imports."
  weight: 1.0
  pass_threshold: 0.9
```

**Scoring**: Average of (includes matched / total) and (excludes absent / total)

### `run_command`
Runs a command and checks its exit code.

```yaml
- type: "run_command"
  command: "python app.py"
  timeout: 30
  expected_exit_code: 0
  description: "The script must run successfully."
  weight: 2.0
  pass_threshold: 1.0
```

### `program_stdout_equals`
Runs a command and checks if stdout matches expected output.

```yaml
- type: "program_stdout_equals"
  command: "python app.py"
  expected_output: "Hello, World!"
  timeout: 10
  description: "The script must output the correct text."
  weight: 1.0
  pass_threshold: 1.0
```

### `pytest`
Runs pytest and provides fractional scoring based on test results.

```yaml
- type: "pytest"
  path: "tests/"
  args: ["-v"]
  timeout: 60
  description: "All tests must pass."
  weight: 3.0
  pass_threshold: 0.9
```

**Scoring**: tests_passed / tests_total (e.g., 7/10 = 0.7)

### `file_matches_regex`
Checks if file content matches (or doesn't match) a regular expression pattern.

```yaml
- type: "file_matches_regex"
  path: "config.py"
  pattern: "^API_KEY = ['\"]\\w+['\"]$"
  must_match: true
  description: "Config must define API_KEY"
  weight: 1.0
  pass_threshold: 1.0
```

**Parameters**:
- `pattern`: Regular expression pattern
- `must_match`: If true, pattern must match; if false, pattern must not match
- `flags`: Optional regex flags (e.g., `re.IGNORECASE`)

### `code_lints`
Runs a generic code linter and checks for clean execution (binary pass/fail).

```yaml
- type: "code_lints"
  linter: "ruff"
  path: "src/"
  args: ["check", "--select=E,F"]
  allow_warnings: false
  timeout: 60
  description: "Code must pass ruff checks"
  weight: 1.5
  pass_threshold: 1.0
```

**Parameters**:
- `linter`: Command to run (e.g., "ruff", "flake8", "eslint")
- `allow_warnings`: If true, warnings don't fail the check
- `args`: Additional arguments to pass to linter

### `pylint_score`
Run pylint static analysis and evaluate code quality on a 0-10 scale with continuous scoring.

```yaml
- type: "pylint_score"
  path: "src/"
  min_score: 8.5
  description: "Code must meet high quality standards"
  weight: 1.5
  pass_threshold: 0.85
```

**Parameters**:
- `path` (str): Path to analyze (file or directory)
- `pass_threshold` (float): Minimum normalized score (0.0-1.0) to pass. Default: 0.9
- `min_score` (float, optional): Minimum score in pylint's native 0-10 scale (overrides pass_threshold)
- `fail_under` (float, optional): Pylint's --fail-under flag (0-10)
- `args` (list[str]): Additional pylint arguments
- `rcfile` (str, optional): Path to .pylintrc configuration file
- `timeout` (int): Timeout in seconds. Default: 120
- `weight` (float): Relative importance. Default: 1.0

**Score Calculation**:
- Pylint's score (0-10) is normalized to 0.0-1.0
- Score reflects overall code quality (errors, warnings, conventions, refactoring)
- Provides continuous scoring for gradual quality assessment

**Tips**:
- Install pylint in sandbox: `env_packages: [pylint>=3.0.0]`
- Use `min_score` for intuitive 0-10 scale
- Use `pass_threshold` for consistency with other criteria
- Adjust timeout for large codebases (default: 120s)
- Use `rcfile` for project-specific rules

### `reference_comparison`
Compares agent's code output with a reference solution using similarity scoring.

```yaml
- type: "reference_comparison"
  agent_file: "solution.py"
  comparison_method: "ast"  # ast, token, or complexity
  similarity_threshold: 0.8
  description: "Solution must be similar to reference"
  weight: 2.0
  pass_threshold: 0.8
```

**Comparison Methods**:
- `ast`: Abstract Syntax Tree similarity (structure-based)
- `token`: Token-based similarity (implementation details)
- `complexity`: Cyclomatic complexity comparison

**Reference Source**: Defined at task level in `reference:` field (see Task Definition)

## Command Telemetry

The framework automatically tracks all agent commands and tool usage:

### What's Tracked

- **Tool name and parameters**: Every tool invocation
- **Duration**: Millisecond-precise timing for each command
- **Status**: Success, error, or unknown
- **Sequence**: Order of operations within each turn

### Command Statistics

After evaluation completes, results include comprehensive statistics:

```json
{
  "command_stats": {
    "total_commands": 42,
    "commands_by_tool": {
      "Read": 15,
      "Write": 12,
      "Bash": 10,
      "Edit": 5
    },
    "total_command_time_ms": 8543.2,
    "avg_command_time_ms": 203.4,
    "slowest_commands": [
      {
        "tool": "Bash",
        "duration_ms": 1523.4,
        "parameters": {"command": "pytest tests/"}
      }
    ],
    "successful_commands": 40,
    "failed_commands": 2,
    "success_rate": 0.95,
    "most_common_sequence": "Read → Edit → Write"
  }
}
```

### Use Cases

- **Performance Analysis**: Identify slow operations
- **Debugging**: Understand command sequences that led to failures
- **Agent Behavior**: Analyze tool usage patterns
- **Optimization**: Find bottlenecks in agent workflows

## LLM Reviewer (Optional)

Enable qualitative feedback from an LLM (via UiPath LLM Gateway):

```yaml
llm_reviewer:
  enabled: true
  model: "anthropic.claude-3-5-sonnet-20240620-v1:0"
  temperature: 0.0
  max_tokens: 1000
```

**Available Models** (via UiPath LLM Gateway):
- `anthropic.claude-3-5-sonnet-20240620-v1:0`
- `gpt-4o-2024-08-06`
- Other models supported by UiPath LLM Gateway

The LLM reviewer provides:
- Qualitative assessment of the agent's work
- Actionable suggestions for improvement
- Guidance on whether to continue or stop

**Note**: Requires UiPath LLM Gateway access via `LLMGW_REQUESTING_PRODUCT` and `LLMGW_REQUESTING_FEATURE` environment variables.

## CLI Commands

### `run` - Execute Evaluations

Run one or more evaluation tasks:

```bash
# Single task
coder-eval run tasks/hello_date.yaml

# Multiple tasks (sequential)
coder-eval run tasks/*.yaml

# Multiple tasks (parallel - up to 3 concurrent)
coder-eval run tasks/*.yaml --max-parallel 3

# With all options
coder-eval run tasks/*.yaml --max-iter 5 --max-parallel 3 --run-dir ./my-run
```

Options:
- `--max-iter, -i`: Override max iterations for all tasks
- `--preserve, -p / --no-preserve, -P`: Preserve sandbox after execution (default: preserve)
- `--run-dir`: Custom run directory (default: auto-generated timestamped directory in `runs/`)
- `--max-parallel, -j`: Maximum number of tasks to run concurrently (default: 1 = sequential)
- `--snapshot-mode`: Override snapshot mode for all tasks (`disabled`, `full`, `incremental`, `hybrid`)
- `--snapshot-checkpoint-freq`: Checkpoint frequency for hybrid mode (default: 5)
- `--verbose, -v`: Enable verbose (DEBUG level) logging - shows LLM prompts/responses, detailed execution traces
- `--log-file`: Write logs to file in addition to console output

#### Parallel Execution

Run multiple tasks concurrently to reduce total evaluation time:

```bash
# Sequential execution (default)
coder-eval run tasks/*.yaml

# Run up to 3 tasks concurrently
coder-eval run tasks/*.yaml --max-parallel 3

# Full parallelism (careful with API rate limits!)
coder-eval run tasks/*.yaml --max-parallel 10
```

**Performance Example**: 5 tasks × 60 seconds each
- Sequential (`--max-parallel 1`): 300 seconds
- Parallel (`--max-parallel 3`): ~120 seconds (2.5x faster ⚡)
- Parallel (`--max-parallel 5`): ~60 seconds (5.0x faster ⚡)

**Best Practices**:
- Start with `--max-parallel 3` for a balance of speed and safety
- Higher values may hit API rate limits (retry logic is automatic)
- Each concurrent task uses ~100-200MB of memory
- Tasks must be independent (no shared state between tasks)
- Monitor API costs with increased parallelism

### `plan` - Validate Tasks

Validate task files without executing (dry-run):

```bash
# Validate single task
coder-eval plan tasks/hello_date.yaml

# Validate multiple tasks
coder-eval plan tasks/*.yaml
```

Checks:
- Task file syntax and schema
- Required CLI tools (claude, uv)
- API keys configuration
- Task configuration validity

### `report` - View or Export Reports

Display or export pre-generated run reports. Reports are automatically created during each run.

```bash
# View latest run report
coder-eval report runs/latest

# View specific run report
coder-eval report runs/2025-10-10_14-30-00

# Export report to file
coder-eval report runs/latest -o summary.md
```

**Auto-generated files**:
- `runs/{timestamp}/run-report.md` - Human-readable markdown report
- `runs/{timestamp}/run-summary.json` - Aggregated statistics (fallback)
- `runs/latest/` - Symlink to most recent run

The report command reads these pre-generated files and displays them. You can also view them directly:

```bash
# View markdown report directly
cat runs/latest/run-report.md

# View JSON summary
cat runs/latest/run-summary.json

# View specific task result
cat runs/latest/task_id/report.json
```

## Development

### Development Workflow

The project includes a comprehensive development workflow with automated quality checks:

#### Makefile Commands

```bash
make install    # Install package + dev dependencies + pre-commit hooks
make format     # Auto-format code with ruff
make check      # Run linting checks
make typecheck  # Run type checking with pyright
make test       # Run test suite
make test-cov   # Run tests with coverage report
make security   # Run security scans (pip-audit + bandit)
make verify     # Run ALL verification steps (CI equivalent)
make clean      # Clean build artifacts
```

#### Pre-commit Hooks

The project uses pre-commit hooks to maintain code quality:

```bash
# Install hooks (done automatically by make install)
pre-commit install

# Run hooks manually
pre-commit run --all-files

# Run specific hook
pre-commit run ruff-format --all-files
```

**Hooks include**:
- **ruff-format**: Auto-format Python code
- **ruff**: Lint with auto-fixes
- **trailing-whitespace**: Remove trailing whitespace
- **end-of-file-fixer**: Ensure files end with newline
- **check-yaml**: Validate YAML syntax
- **check-added-large-files**: Prevent committing large files
- **pyright** (manual): Type checking (run with `--hook-stage manual`)

#### CI/CD Verification Steps

The CI pipeline runs the same checks as local development:

1. **Format**: `ruff format --check coder_eval/ tests/`
2. **Lint**: `ruff check coder_eval/ tests/`
3. **Type Check**: `pyright`
4. **Security**: `pip-audit --desc --skip-editable` + `bandit -r coder_eval/`
5. **Test**: `pytest tests/ -v --cov=coder_eval --cov-fail-under=80`

**Always run `make verify` before pushing to ensure CI will pass.**

### Running Tests

```bash
# All tests (via Makefile)
make test

# Or directly with pytest
pytest tests/

# Specific test file
pytest tests/test_sandbox.py

# With coverage
pytest --cov=coder_eval tests/

# With coverage report
make test-cov
```

**Current Status**: 252 tests, 86% coverage

### Adding a New Agent

1. Create a new agent class in `agents/`:
```python
from coder_eval.agent import Agent, AgentState
from coder_eval.models import TurnRecord

class MyAgent(Agent):
    async def start(self, working_directory: str) -> None:
        # Initialize your agent
        pass

    async def communicate(self, user_input: str) -> TurnRecord:
        # Send message and get response
        pass

    async def stop(self) -> None:
        # Cleanup
        pass

    def get_state(self) -> AgentState:
        # Return current state
        pass
```

2. Add your agent type to `models.py`:
```python
class AgentKind(str, Enum):
    CLAUDE_CODE = "claude-code"
    MY_AGENT = "my-agent"
```

3. Update the orchestrator to support your agent:
```python
# In orchestrator.py _create_agent()
if self.task.agent.type == AgentKind.MY_AGENT:
    from agents.my_agent import MyAgent
    return MyAgent(self.task.agent)
```

### Adding New Success Criteria

1. Define the criterion model in `models.py`:
```python
class MyCustomCriterion(BaseSuccessCriterion):
    type: Literal["my_custom"] = "my_custom"
    # Add your fields
    custom_field: str
```

2. Add to the union type:
```python
SuccessCriterion = Union[
    FileExistsCriterion,
    # ...
    MyCustomCriterion,
]
```

3. Implement the check in `evaluator.py`:
```python
@handle_criterion_errors
def _check_my_custom(self, criterion: MyCustomCriterion) -> CriterionResult:
    # Implement your check logic
    return CriterionResult(...)
```

4. Add to dispatch table in `SuccessChecker.__init__()`:
```python
self._checkers["my_custom"] = self._check_my_custom
```

## Architecture

### Core Components

```
coder_eval/
 models.py        # Pydantic data models (765 lines)
 config.py        # Configuration management
 sandbox.py       # Sandboxed execution environments (625 lines)
 agent.py         # Agent interface (ABC)
 evaluator.py     # Success criteria and LLM review (824 lines)
 orchestrator.py  # Main evaluation loop (500+ lines)
 cli.py           # Command-line interface (400+ lines)
 utils.py         # Version tracking utilities
 logging_config.py # Structured logging
 path_utils.py    # Path and run ID utilities
 reports.py       # Report generation
 analysis.py      # Command statistics
 scorers.py       # Code similarity scorers

agents/
 claude_code_agent.py  # Claude Code agent implementation (374 lines)

tasks/
 *.yaml           # Task definitions

tests/
 test_*.py        # Test suite (252 tests, 86% coverage)
```

For detailed architecture documentation, see [CLAUDE.md](CLAUDE.md).

### Evaluation Flow

1. **Setup**:
   - Create timestamped run directory (`runs/{timestamp}/`)
   - Create per-task subdirectory (`runs/{timestamp}/{task_id}/`)
   - Create sandbox, install dependencies
   - Initialize agent
   - Setup snapshot directory (if enabled)
2. **Iteration Loop**:
   - Send prompt to agent
   - Record agent actions and file changes with telemetry
   - Create snapshot (if enabled)
   - Check success criteria
   - If failed: Generate feedback (LLM or deterministic)
   - Repeat until success or max iterations
3. **Cleanup**:
   - Stop agent
   - Preserve/cleanup sandbox (optionally to `artifacts/` subdirectory)
   - Calculate weighted score
   - Calculate command statistics
   - Save task result to `runs/{timestamp}/{task_id}/report.json`
4. **Finalization**:
   - Generate run-level summary (`run-summary.json`)
   - Generate markdown report (`run-report.md`)
   - Create/update `latest` symlink to current run

## Configuration

### Settings (via .env or environment variables)

- `ANTHROPIC_API_KEY`: Anthropic API key (required for Claude Code)
- `LLMGW_REQUESTING_PRODUCT`: Product name for UiPath LLM Gateway (default: "coder-eval")
- `LLMGW_REQUESTING_FEATURE`: Feature name for UiPath LLM Gateway (default: "evaluation")
- `LOG_LEVEL`: Logging level (default: "INFO")

### Output Directory Structure

All evaluation outputs are stored in a **timestamped run directory** structure:

```
runs/
├── 2025-10-09_15-30-45/          # Timestamped run directory
│   ├── run-summary.json          # Aggregated run statistics
│   ├── run-report.md             # Human-readable run report
│   ├── task1/                    # Per-task subdirectory
│   │   ├── report.json           # Task-specific evaluation result
│   │   ├── task.log              # Per-task log file
│   │   ├── snapshots/            # Iteration snapshots (if enabled)
│   │   └── artifacts/            # Preserved sandbox (default, unless --no-preserve used)
│   └── task2/
│       ├── report.json
│       └── artifacts/
└── latest -> 2025-10-09_15-30-45/ # Symlink to most recent run
```

**Key Features**:
- **Timestamped runs**: Each evaluation run gets a unique directory with format `YYYY-MM-DD_HH-MM-SS`
- **Per-task isolation**: Each task has its own subdirectory within the run
- **Run-level reports**: Aggregated statistics and markdown reports for the entire run
- **Latest symlink**: Convenient access to the most recent run via `runs/latest/`
- **Customizable**: Use `--run-dir` to specify a custom location

**Example**:
```bash
# Auto-generated timestamped directory
coder-eval run tasks/*.yaml
# Creates: runs/2025-10-09_15-30-45/

# Custom directory
coder-eval run tasks/*.yaml --run-dir ./my-evaluation
# Creates: ./my-evaluation/
```

### Sandbox Configuration

- `driver`: Sandbox type (`tempdir` or `docker` - docker is stubbed)
- `template_sources`: List of template sources to apply (see Task Templates section)
- `python_version`: Python version for virtual environment
- `env_packages`: Python packages to install
- `limits`: Resource limits (timeout, memory, disk)
- `snapshots`: Snapshot configuration (see Sandbox Snapshots section)

### Agent Configuration

- `type`: Agent type (`claude-code`)
- `permission_mode`: Permission mode (`auto`, `acceptEdits`, `plan`, `bypassPermissions`)
- `allowed_tools`: List of allowed tools
- `model`: Specific model to use (optional)

### Reference Source Configuration

Define reference solutions for comparison:

```yaml
reference:
  file: "reference_solution.py"  # Path to reference file (relative to task YAML)
  # OR
  code: |
    def solution():
        return "inline reference code"
```

Used by `reference_comparison` success criterion.

## Troubleshooting

### "ANTHROPIC_API_KEY is required"

Make sure you've created a `.env` file with your API key:
```bash
cp .env.example .env
# Edit .env and add your key
```

### "claude command not found"

Install the Claude Code CLI:
```bash
brew install claude  # macOS
# or follow instructions at https://docs.anthropic.com/claude/docs/claude-code
```

### "uv command not found"

Install uv:
```bash
brew install uv  # macOS
# or: pip install uv
```

### "uv pip install" fails with 401 error

Ensure you're a member of the UiPath Engineering.Cloud Azure group for private PyPI index access.

### Tests are failing

Make sure you're in the virtual environment:
```bash
source .venv/bin/activate
pytest
```

### Pre-commit hooks failing

Update hooks to latest version:
```bash
pre-commit autoupdate
pre-commit run --all-files
```

## Roadmap

- [ ] Docker sandbox driver implementation
- [ ] Support for more agents (Aider, Cursor, etc.)
- [ ] Web UI for results visualization
- [x] Batch evaluation with parallel execution (✅ Complete)
- [x] Continuous scoring system (✅ Complete)
- [x] Snapshot system (✅ Complete)
- [x] Command telemetry tracking (✅ Complete)
- [x] Reference comparison (✅ Complete)
- [ ] Token usage and cost tracking
- [ ] Comparative analysis reports
- [ ] Custom hooks for pre/post evaluation steps

## License

MIT

## Contributing

Contributions are welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Run `make verify` to ensure all checks pass
5. Submit a pull request

## Acknowledgments

Built with:
- [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk)
- [Pydantic](https://pydantic.dev/) for data validation
- [Typer](https://typer.tiangolo.com/) for CLI
- [Rich](https://rich.readthedocs.io/) for terminal formatting
- [UiPath LLM Gateway](https://uipath.com) for LLM review capabilities
