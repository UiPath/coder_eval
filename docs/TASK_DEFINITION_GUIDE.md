# Task Definition Guide

Complete reference for defining evaluation tasks in coder_eval.

## Table of Contents

- [Task YAML Structure](#task-yaml-structure)
- [Agent Configuration](#agent-configuration)
- [Sandbox Configuration](#sandbox-configuration)
- [Template Sources](#template-sources)
- [Success Criteria](#success-criteria)
  - [Continuous Scoring](#continuous-scoring)
  - [file_exists](#file_exists)
  - [file_contains](#file_contains)
  - [file_check](#file_check)
  - [json_check](#json_check)
  - [run_command](#run_command)
  - [pytest](#pytest)
  - [file_matches_regex](#file_matches_regex)
  - [pylint_score](#pylint_score)
  - [reference_comparison](#reference_comparison)
  - [command_executed](#command_executed)
  - [uipath_eval](#uipath_eval)
  - [llm_judge](#llm_judge)
- [Sandbox Snapshots](#sandbox-snapshots)
- [LLM Reviewer](#llm-reviewer)
- [Reference Solutions](#reference-solutions)
- [Post-Run Commands](#post-run-commands)
- [Command Telemetry](#command-telemetry)
- [Complete Example](#complete-example)

## Task YAML Structure

Every task is a YAML file with this top-level structure:

```yaml
task_id: "my_task"                    # Unique identifier (required)
description: "What this task tests"   # Human-readable description (required)
initial_prompt: "Instructions..."     # Prompt sent to the agent (required)
max_iterations: 3                     # Max agent iterations (required)
tags: [smoke, golden, pure-python]    # Optional tags for filtering (kebab-case)

agent: { ... }                        # Agent configuration (optional, resolved from experiment)
sandbox: { ... }                      # Sandbox configuration (required)
success_criteria: [ ... ]             # List of criteria (required, at least 1)

llm_reviewer: { ... }                 # Optional LLM reviewer
reference: { ... }                    # Optional reference solution
post_run: [ ... ]                     # Optional post-run commands
```

## Tags

Tags categorize tasks for selective execution. Tags must be lowercase kebab-case strings.

```yaml
tags: [smoke, golden, uipath-python]
```

**Well-known tags:**

| Tag | Purpose |
|-----|---------|
| `smoke` | Quick sanity check, should always pass |
| `golden` | High-confidence reference tasks for framework validation |
| `basic` | Simple tasks testing core functionality |
| `integration` | Requires external services or network |
| `example` | Demonstration/tutorial tasks, not for CI |
| `uipath-python` | Uses UiPath Python SDK |
| `uipath-langchain` | Uses UiPath + LangChain integration |
| `pure-python` | No external SDK dependencies |
| `llm-review` | Includes LLM reviewer step |
| `template` | Uses template sources |
| `network` | Requires network access |

**CLI filtering:**

```bash
coder-eval run tasks/*.yaml --tags smoke          # Only run smoke tasks
coder-eval run tasks/*.yaml --tags golden,basic   # Run golden OR basic tasks
coder-eval run tasks/*.yaml --exclude-tags example # Skip example tasks
```

## Agent Configuration

```yaml
agent:
  type: "claude-code"                 # Agent type (currently only "claude-code")
  permission_mode: "acceptEdits"      # Permission mode (see below)
  allowed_tools:                      # Tools the agent can use
    - "Read"
    - "Write"
    - "Bash"
  model: "claude-sonnet-4-20250514"   # Optional: specific model
```

**Permission Modes:**
- `auto` — Agent decides when to ask for permission
- `acceptEdits` — Auto-accept file edits (recommended for evaluations)
- `plan` — Agent proposes changes, waits for approval
- `bypassPermissions` — No permission checks (use with caution)

## Sandbox Configuration

```yaml
sandbox:
  driver: "tempdir"                   # Sandbox type ("tempdir" or "docker")
  python:                              # Python env config (null to skip venv)
    env_packages:                      # Packages to install in sandbox venv
      - pytest
      - pylint>=3.0
  template_sources: [ ... ]           # Optional: preset files (see below)
  snapshots: { ... }                  # Optional: snapshot config (see below)
  limits:                             # Optional: resource limits
    timeout: 300                       # Enforced via subprocess timeout
    max_memory_mb: 512                 # NOT enforced (reserved for future use)
    max_disk_mb: 1024                  # NOT enforced (reserved for future use)
```

## Template Sources

Tasks can start with preset files instead of an empty sandbox. Multiple sources are applied sequentially (last wins for conflicts).

### Git Repository

```yaml
template_sources:
  - type: "repo"
    url: "https://github.com/user/repo.git"
    commit: "abc123"                  # Optional: pin to specific commit
```

**Note:** If using `repo` source, it must be first in the list.

### Template Directory

Copy a local directory into the sandbox:

```yaml
template_sources:
  - type: "template_dir"
    path: "../templates/python-starter"  # Relative to task YAML file
```

The framework automatically ignores `.venv`, `.git`, `__pycache__`, and `node_modules`.

### Inline Starter Files

Define files directly in YAML (ideal for 1–3 files):

```yaml
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
              pass  # TODO: Implement
```

### Combining Sources

```yaml
template_sources:
  - type: "template_dir"
    path: "../templates/python-base"
  - type: "starter_files"
    files:
      - path: "requirements.txt"
        content: "pytest>=8.0\npylint>=3.0"
```

### Template Sources in Experiment Variants

Experiment variants can add `template_sources` that are **appended after** the task's own template sources. This is useful for injecting variant-specific context (like a `CLAUDE.md` hint file) without duplicating the base task or creating separate template directories.

```yaml
# experiments/my-experiment.yaml
variants:
  - variant_id: baseline
    agent:
      model: "claude-sonnet-4-20250514"

  - variant_id: with-context-hint
    agent:
      model: "claude-sonnet-4-20250514"
    template_sources:
      - type: "starter_files"
        files:
          - path: "CLAUDE.md"
            content: |
              The UiPath flows are in folder ID abc-123-def.
              Use this folder when interacting with the Orchestrator API.
```

In this example, the `with-context-hint` variant gets the same sandbox as `baseline`, plus a `CLAUDE.md` file written into the sandbox root. Since variant template sources are appended last, they can also overwrite files from earlier sources (last-wins).

This pattern is especially useful for A/B testing whether additional context improves agent performance.

## Success Criteria

Every task needs at least one success criterion. The framework supports 14 criterion types.

### Continuous Scoring

All criteria share these fields:

| Field | Default | Description |
|-------|---------|-------------|
| `description` | — | Human-readable description (required) |
| `weight` | 1.0 | Relative importance for weighted score |
| `pass_threshold` | 0.9 | Minimum score (0.0–1.0) to pass |

**Scoring types:**
- **Binary** (1.0 or 0.0): `file_exists`, `run_command`, `file_matches_regex`
- **Fractional** (0.0–1.0): `file_contains`, `file_check`, `json_check`, `pytest`, `command_executed`, `uipath_eval`
- **Continuous** (0.0–1.0): `pylint_score`, `reference_comparison`, `llm_judge`

**Task success:** ALL criteria must score >= their `pass_threshold`.

**Weighted score:** `weighted_score = sum(score * weight) / sum(weight)` — calculated regardless for quality assessment.

### `file_exists`

Checks if a file exists. **Binary scoring.**

```yaml
- type: "file_exists"
  path: "app.py"
  description: "app.py must be created"
```

### `file_contains`

Checks if a file contains (or doesn't contain) specific strings. **Fractional scoring:** average of (includes matched / total) and (excludes absent / total).

```yaml
- type: "file_contains"
  path: "app.py"
  includes:                           # Strings that must be present
    - "Hello"
    - "import datetime"
  excludes:                           # Optional: strings that must NOT be present
    - "TODO"
    - "FIXME"
  description: "File must contain required strings"
  weight: 1.0
  pass_threshold: 0.9
```

### `file_check`

Unified file check that combines existence, string includes/excludes, and regex patterns into a single criterion. **Fractional scoring:** average of active sub-check scores. Replaces common `file_exists` + `file_contains` + `file_matches_regex` combinations.

File existence is implicit — if the file doesn't exist, score is 0.0. If no sub-checks are specified, it behaves as a pure existence check.

```yaml
# Full example with all features
- type: "file_check"
  path: "main.py"
  includes:                           # Strings that must be present
    - "from uipath import UiPath"
    - "def main"
  excludes:                           # Strings that must NOT be present
    - "import os"
  patterns:                           # Regex patterns to check
    - pattern: "def main\\(.*\\):"
      must_match: true                # true = must match (default), false = must NOT match
      flags: 0                        # Regex flags (default: 0)
  description: "main.py exists with correct imports and structure"
  weight: 1.0
  pass_threshold: 0.9

# Minimal: existence-only check (equivalent to file_exists)
- type: "file_check"
  path: "app.py"
  description: "app.py must be created"
```

| Field | Default | Description |
|-------|---------|-------------|
| `path` | *required* | Path to the file (relative to sandbox root) |
| `includes` | `[]` | Strings that must be present |
| `excludes` | `[]` | Strings that must NOT be present |
| `patterns` | `[]` | Regex pattern objects (`pattern`, `must_match`, `flags`) |

**Scoring:** Only active categories (non-empty lists) contribute to the average. For example, specifying only `includes` means the score equals the includes score alone — it is not inflated by absent categories.

### `json_check`

Validates a JSON file: existence, parse-ability, JSON Schema conformance, and JMESPath assertions. **Fractional scoring.**

File existence and valid JSON are implicit — if the file is missing or unparseable, score is 0.0. If no sub-checks are specified, it's a pure "is valid JSON" check.

```yaml
# Minimal: just validate JSON syntax
- type: "json_check"
  path: "data.json"
  description: "data.json is valid JSON"

# Schema validation only
- type: "json_check"
  path: "output.json"
  json_schema: "schemas/output_schema.json"
  description: "Output conforms to expected schema"

# JMESPath assertions only
- type: "json_check"
  path: "report.json"
  assertions:
    - expression: "status"
      expected: "success"
    - expression: "length(results)"
      operator: "gte"
      expected: 1
    - expression: "metadata.version"
      operator: "regex"
      expected: "^\\d+\\.\\d+\\.\\d+$"
  description: "Report has correct structure and values"

# Both schema + assertions
- type: "json_check"
  path: "result.json"
  json_schema: "schemas/result_schema.json"
  assertions:
    - expression: "status"
      expected: "completed"
    - expression: "items[?active].name"
      operator: "exists"
  description: "Result is valid and has expected values"
```

| Field | Default | Description |
|-------|---------|-------------|
| `path` | *required* | Path to the JSON file (relative to sandbox root) |
| `json_schema` | `null` | Path to a JSON Schema file (relative to sandbox root) |
| `assertions` | `[]` | List of JMESPath assertions (see below) |

**Assertion fields:**

| Field | Default | Description |
|-------|---------|-------------|
| `expression` | *required* | JMESPath expression to evaluate |
| `operator` | `"equals"` | One of: `equals`, `not_equals`, `contains`, `gt`, `gte`, `lt`, `lte`, `type`, `regex`, `exists` |
| `expected` | `null` | Expected value (required for all operators except `exists`) |

**Scoring:** Only active categories (schema, assertions) contribute. Schema scoring is binary (1.0/0.0). Assertions score = passed / total. When both are used, final score = average of the two category scores.

### `run_command`

Runs a command and checks the exit code, with optional stdout matching. **Binary scoring.**

```yaml
# Simple exit-code check
- type: "run_command"
  command: "python app.py"
  timeout: 30                         # Timeout in seconds (default: 30)
  expected_exit_code: 0               # Expected exit code (default: 0)
  description: "Script must run successfully"
  weight: 2.0

# With stdout matching (replaces former program_stdout_equals)
- type: "run_command"
  command: "python hello.py"
  expected_stdout: "Hello, World!"    # Optional: check stdout content
  stdout_match: "exact"               # "exact" (default), "contains", or "regex"
  description: "Script must output the correct text"
```

| Field | Default | Description |
|-------|---------|-------------|
| `command` | *required* | Command to execute |
| `timeout` | 30 | Timeout in seconds |
| `expected_exit_code` | 0 | Expected exit code |
| `expected_stdout` | `null` | When set, stdout is also checked |
| `stdout_match` | `"exact"` | Match mode: `exact` (stripped), `contains` (substring), `regex` (pattern) |

### `pytest`

Runs pytest and scores based on test results. **Fractional scoring:** `tests_passed / tests_total`.

```yaml
- type: "pytest"
  path: "tests/"                      # Test directory or file (default: ".")
  args: ["-v"]                        # Additional pytest arguments
  timeout: 60                         # Timeout in seconds (default: 60)
  description: "All tests must pass"
  weight: 3.0
  pass_threshold: 0.9                 # 90% of tests must pass
```

### `file_matches_regex`

Checks if file content matches a regular expression pattern. **Binary scoring.**

```yaml
- type: "file_matches_regex"
  path: "config.py"
  pattern: "^API_KEY = ['\"]\\w+['\"]$"
  must_match: true                    # true = must match; false = must NOT match
  flags: 0                            # Regex flags (re.IGNORECASE=2, re.MULTILINE=8)
  description: "Config must define API_KEY"
```

### `pylint_score`

Runs pylint and evaluates code quality. **Continuous scoring:** pylint score (0–10) normalized to 0.0–1.0.

```yaml
- type: "pylint_score"
  path: "src/"
  min_score: 8.5                      # Optional: minimum in pylint's 0-10 scale
  pass_threshold: 0.85                # Alternative: 0.0-1.0 scale (default: 0.9)
  fail_under: 5.0                     # Optional: pylint --fail-under flag
  args: ["--disable=C0111"]           # Additional pylint arguments
  rcfile: ".pylintrc"                 # Optional: path to config file
  timeout: 120                        # Timeout in seconds (default: 120)
  description: "Code must meet quality standards"
  weight: 1.5
```

**Notes:**
- `min_score` (0–10 scale) overrides `pass_threshold` when both are set
- Install pylint in sandbox: `python: { env_packages: [pylint>=3.0] }`

### `reference_comparison`

Compares agent's code with a reference solution using similarity scoring. **Continuous scoring.** Requires a `reference` block at the task level.

```yaml
- type: "reference_comparison"
  agent_file: "solution.py"           # Agent's output file (relative to sandbox)
  comparison_method: "ast"            # Method: "ast", "token", or "complexity"
  similarity_threshold: 0.8           # Minimum similarity (0.0-1.0)
  description: "Solution must match reference structure"
  weight: 2.0
```

**Comparison methods:**
- `ast` — Abstract Syntax Tree similarity (structure-based)
- `token` — Token-based similarity (implementation details)
- `complexity` — Cyclomatic complexity comparison

### `command_executed`

Checks whether the agent executed specific tools/commands during evaluation. Inspects `CommandTelemetry` records from agent turns. **Fractional scoring:** matched commands / `min_count`.

```yaml
- type: "command_executed"
  tool_name: "Bash"                   # Tool name filter (null = any tool)
  command_pattern: "curl.*wttr\\.in"  # Regex to match command parameters (null = any)
  min_count: 1                        # Minimum matching commands required (default: 1)
  require_success: true               # Only count successful commands (default: false)
  description: "Agent must use curl to fetch weather"
```

### `uipath_eval`

Evaluates a UiPath agent against a named evaluation set. **Fractional scoring:** metrics passed / total metrics.

```yaml
- type: "uipath_eval"
  agent_name: "my-agent"
  eval_set: "regression-v1"
  thresholds:
    accuracy: 0.8
    f1: 0.75
  description: "Agent must meet accuracy and F1 thresholds"
```

| Field | Default | Description |
|-------|---------|-------------|
| `agent_name` | *required* | Name of the UiPath agent to evaluate |
| `eval_set` | *required* | Evaluation set identifier |
| `thresholds` | *required* | Minimum acceptable value per metric (metric passes if value >= threshold) |

### `llm_judge`

Have an LLM grade the task against a rubric written in the task YAML. **Continuous scoring** from a JSON verdict `{"score": 0.0-1.0, "rationale": "..."}`; parse failure, non-numeric score, or LLM error all produce `score=0.0` with an `error` populated.

```yaml
- type: "llm_judge"
  description: "Implementation follows the rubric"
  prompt: |
    Grade the implementation on correctness and idiomatic style.
    - 1.0: correct and idiomatic
    - 0.5: correct but not idiomatic
    - 0.0: incorrect or missing
  files: ["main.py", "tests/test_main.py"]
  include_reference: true            # Opt-in: show reference solution to the judge (never to the agent)
  include_agent_output: false        # Opt-in: include the latest turn's raw agent output
  include_tool_calls: false          # Opt-in: include a summary of the latest turn's tool calls
  model: "anthropic.claude-sonnet-4-6"
  temperature: 0.0
  max_tokens: 1000
  max_file_chars: 20000              # Per-file content truncation
  weight: 2.0
  pass_threshold: 0.7
```

| Field | Default | Description |
|-------|---------|-------------|
| `prompt` | *required* | Grading instructions shown to the judge |
| `files` | `[]` | Sandbox-relative paths whose contents are shown to the judge (missing files render as `<file not found>`) |
| `include_reference` | `false` | Include the task's reference solution in the judge prompt (silently omitted if no reference is configured). Never shown to the agent. |
| `include_agent_output` | `false` | Include the latest agent turn's raw output (wrapped as UNTRUSTED DATA) |
| `include_tool_calls` | `false` | Include a summary of the latest agent turn's tool calls |
| `model` | `anthropic.claude-sonnet-4-6` | UiPath LLM Gateway model name |
| `temperature` | `0.0` | Sampling temperature (0.0 = deterministic) |
| `max_tokens` | `1000` | Maximum tokens in the judge's response |
| `max_file_chars` | `20000` | Per-file (and agent_output) truncation applied before building the prompt |

**Requires** UiPath LLM Gateway access (see `.env.example` for configuration).

**Security**

- The three opt-in context blocks (`files`, `include_agent_output`, `include_tool_calls`) are wrapped with an `UNTRUSTED DATA` preamble to mitigate prompt-injection via tool output.
- The reference solution is shown only to the judge — never to the agent — and any occurrence of the reference is scrubbed from `CriterionResult.details` before persistence.

**Failure modes** — each sets `score=0.0` and populates `error`:

- Non-JSON response from the model (parse failure)
- `score` key missing from the JSON verdict
- `score` is not coercible to float
- LLM Gateway unavailable / network error (handled by `@handle_criterion_errors`)

## Sandbox Snapshots

Capture sandbox state after each agent iteration for debugging and analysis.

```yaml
sandbox:
  snapshots:
    mode: "hybrid"                    # disabled, full, incremental, hybrid
    checkpoint_frequency: 5           # Full snapshot every N iterations (hybrid only)
    ignore_patterns:                  # Optional: patterns to exclude
      - "*.log"
      - "temp_*"
```

**Modes:**
- `disabled` (default) — No snapshots
- `full` — Complete sandbox copy every iteration
- `incremental` — Changed files only
- `hybrid` — Full at checkpoints, incremental between (recommended for production)

**CLI overrides:**

```bash
coder-eval run tasks/*.yaml --snapshot-mode full
coder-eval run tasks/*.yaml --snapshot-mode hybrid --snapshot-checkpoint-freq 3
```

**Snapshot output:**

```
runs/{timestamp}/{task_id}/snapshots/
├── iteration_1/
│   ├── manifest.json                 # Metadata (timestamp, mode, file_count, etc.)
│   └── [files...]
├── iteration_2/
│   └── ...
```

## LLM Reviewer

Optional qualitative feedback from an LLM (via UiPath LLM Gateway). Called when a task fails criteria to generate improvement suggestions.

```yaml
llm_reviewer:
  enabled: true
  model: "anthropic.claude-3-5-sonnet-20240620-v1:0"
  temperature: 0.0
  max_tokens: 1000
  prompt: |                           # Optional: custom review prompt
    Evaluate if the code is complete and follows best practices:
    1. Is it well-structured?
    2. Does it handle edge cases?
```

**Requires** UiPath LLM Gateway access (see `.env.example` for configuration).

## Reference Solutions

Define a reference solution for `reference_comparison` criteria:

```yaml
# From a file (relative to task YAML)
reference:
  file: "reference_solution.py"

# Or inline
reference:
  code: |
    def fibonacci(n):
        if n <= 1:
            return n
        return fibonacci(n - 1) + fibonacci(n - 2)
```

## Post-Run Commands

Run shell commands inside the sandbox after evaluation completes. Post-run commands are **informational only** — they never affect pass/fail status. Use them for artifact generation, data extraction, validation reports, or cleanup.

```yaml
post_run:
  - command: "python3 validate_flow.py output.flow 12 4"
    timeout: 60

  - command: "cat results.json | jq '.summary'"

  - command: "tar czf /tmp/artifacts.tar.gz ."
    timeout: 120
```

| Field | Default | Description |
|-------|---------|-------------|
| `command` | *required* | Shell command to execute (supports pipes, redirects, etc.) |
| `timeout` | 30 | Maximum seconds to wait (1–300) |

Commands run sequentially with `cwd` set to the sandbox directory. stdout and stderr are captured on the `post_run_results` field of the evaluation result (truncated to 100KB each).

## Command Telemetry

The framework automatically tracks all agent commands. No configuration needed.

**What's tracked:**
- Tool name and parameters
- Duration (millisecond precision)
- Status (success, error, unknown)
- Execution sequence within each turn

**Token usage** is also tracked (input/output tokens per turn) for cost analysis.

Results include aggregated statistics:

```json
{
  "command_stats": {
    "total_commands": 42,
    "commands_by_tool": { "Read": 15, "Write": 12, "Bash": 10 },
    "total_command_time_ms": 8543.2,
    "success_rate": 0.95,
    "slowest_commands": [...]
  }
}
```

## Complete Example

A full-featured task definition using most features:

```yaml
task_id: "calculator_agent"
description: "Create a calculator agent using LangGraph"
max_iterations: 5

initial_prompt: |
  Create a calculator agent using StateGraph that performs
  basic arithmetic operations (+, -, *, /).

agent:
  type: "claude-code"
  permission_mode: "acceptEdits"
  allowed_tools: ["Read", "Write", "Bash"]

sandbox:
  driver: "tempdir"
  python:
    env_packages:
      - pytest
      - pylint>=3.0
  template_sources:
    - type: "template_dir"
      path: "../templates/python-starter"
  snapshots:
    mode: "hybrid"
    checkpoint_frequency: 3

success_criteria:
  - type: "file_exists"
    path: "main.py"
    description: "main.py must exist"
    weight: 0.5

  - type: "file_contains"
    path: "main.py"
    includes: ["StateGraph", "BaseModel"]
    description: "Must use required libraries"
    weight: 2.0

  - type: "run_command"
    command: "python -m py_compile main.py"
    timeout: 10
    description: "Valid Python syntax"

  - type: "pytest"
    path: "tests/"
    timeout: 60
    description: "All tests pass"
    weight: 3.0
    pass_threshold: 1.0

  - type: "pylint_score"
    path: "main.py"
    min_score: 8.0
    description: "Code quality >= 8/10"
    weight: 1.5

  - type: "reference_comparison"
    agent_file: "main.py"
    comparison_method: "ast"
    similarity_threshold: 0.7
    description: "Code structure matches reference"
    weight: 2.5

  - type: "command_executed"
    tool_name: "Bash"
    command_pattern: "python.*main\\.py"
    min_count: 1
    description: "Agent must run the script"

reference:
  code: |
    from pydantic import BaseModel
    from langgraph.graph import StateGraph, START, END

    class Input(BaseModel):
        a: float
        b: float
        operator: str

    class Output(BaseModel):
        result: float

    def calculate(state: Input) -> Output:
        ops = {"+": lambda: state.a + state.b,
               "-": lambda: state.a - state.b,
               "*": lambda: state.a * state.b,
               "/": lambda: state.a / state.b if state.b != 0 else 0}
        return Output(result=ops.get(state.operator, lambda: 0)())

    builder = StateGraph(state_schema=Input, input=Input, output=Output)
    builder.add_node("calculate", calculate)
    builder.add_edge(START, "calculate")
    builder.add_edge("calculate", END)
    graph = builder.compile()

llm_reviewer:
  enabled: true
  model: "anthropic.claude-3-5-sonnet-20240620-v1:0"
  prompt: |
    Evaluate completeness and code quality:
    1. Does it implement all 4 arithmetic operations?
    2. Does it handle edge cases (division by zero)?
    3. Is the code clean and well-structured?
```
