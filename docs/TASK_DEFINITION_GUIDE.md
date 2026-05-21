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
  - [agent_judge](#agent_judge)
- [Sandbox Snapshots](#sandbox-snapshots)
- [Reference Solutions](#reference-solutions)
- [Pre-Run Commands](#pre-run-commands)
- [Post-Run Commands](#post-run-commands)
- [Simulation (Multi-Turn User Dialog)](#simulation-multi-turn-user-dialog)
- [Command Telemetry](#command-telemetry)
- [Complete Example](#complete-example)

## Task YAML Structure

Every task is a YAML file with this top-level structure:

```yaml
task_id: "my_task"                    # Unique identifier (required)
description: "What this task tests"   # Human-readable description (required)
initial_prompt: "Instructions..."     # Prompt sent to the agent (required)
tags: [smoke, golden, pure-python]    # Optional tags for filtering (kebab-case)

agent: { ... }                        # Agent configuration (optional, resolved from experiment)
sandbox: { ... }                      # Sandbox configuration (optional, defaults to tempdir)
success_criteria: [ ... ]             # List of criteria (required, at least 1)

reference: { ... }                    # Optional reference solution
pre_run: [ ... ]                      # Optional pre-run commands (before agent starts)
post_run: [ ... ]                     # Optional post-run commands
```

## Tags

Tags categorize tasks for selective execution. Each tag is lowercase kebab-case and may
optionally be namespaced as `key:value` where both sides are kebab-case.

```yaml
tags: [smoke, golden, uipath-python, lifecycle:generate, connector:google-tasks]
```

Namespaced tags let downstream tools slice on a single dimension (e.g. ADX queries like
`where tag startswith "connector:"`). Bare tags continue to work with existing
`--tags` filters.

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
| `template` | Uses template sources |
| `network` | Requires network access |

**CLI filtering:**

```bash
coder-eval run tasks/*.yaml --tags smoke-pass     # Smoke tasks expected to succeed
coder-eval run tasks/*.yaml --tags smoke-fail     # Smoke tasks expected to fail (failure-detection sentinels)
coder-eval run tasks/*.yaml --tags smoke          # Umbrella tag: all smoke tasks (pass + fail buckets)
coder-eval run tasks/*.yaml --tags golden,basic   # Run golden OR basic tasks
coder-eval run tasks/*.yaml --exclude-tags example # Skip example tasks
```

## Agent Configuration

```yaml
# Run-time caps (turns, wall-clock, tokens, USD) live under run_limits
run_limits:
  max_turns: 20                       # Optional: cap on inner-loop turns per iteration
  turn_timeout: 300                   # Optional: per-communicate() timeout in seconds
  task_timeout: 600                   # Optional: wall-clock cap across all iterations

agent:
  type: "claude-code"                 # Agent type — optional if supplied via experiment / --type
  permission_mode: "acceptEdits"      # Permission mode (see below)
  allowed_tools:                      # Tools the agent can use
    - "Read"
    - "Write"
    - "Bash"
  model: "claude-sonnet-4-20250514"   # Optional: specific model
  sdk_options:                        # Optional: Claude Code SDK pass-through
    effort: high                      # any non-framework-managed ClaudeAgentOptions field
```

**`sdk_options`** is a typed pass-through dict for Claude Code SDK
`ClaudeAgentOptions` fields that coder_eval doesn't own directly. Keys are
validated against the SDK's dataclass at YAML load; framework-managed keys
(`model`, `allowed_tools`, `permission_mode`, `hooks`, `mcp_servers`, …)
are rejected. Deep-merged across the 5-layer config chain. Override via
CLI with the repeatable `--sdk-option KEY=VALUE`. See the
[SDK pass-through feature spec](features/2026-05-18-sdk-pass-through.md)
for the full reference, including the valid keys list.


**Permission Modes:**
- `auto` — Agent decides when to ask for permission
- `acceptEdits` — Auto-accept file edits (recommended for evaluations)
- `plan` — Agent proposes changes, waits for approval
- `bypassPermissions` — No permission checks (use with caution)

### `max_turns`, `task_timeout`, `turn_timeout` location

These live under `run_limits:` on the task — alongside the token / USD
budget caps. They are scenario constraints; the agent identity (type,
model, etc.) is conceptually separate. See
[`docs/features/2026-05-11-run-limits.md`](features/2026-05-11-run-limits.md)
for the full reference.

> **Migration callout:** Setting `max_turns` or `turn_timeout` under
> `agent:` still works via a deprecation shim that hoists them into
> `run_limits.*` and emits a `DeprecationWarning`. **The shim is removed
> on 2026-05-20.** Move both fields out of `agent:` before that date.

## Sandbox Configuration

The `sandbox` block is optional. When omitted, it defaults to `driver: "tempdir"` with standard Python environment.

```yaml
sandbox:
  driver: "tempdir"                   # Sandbox type ("tempdir" or "docker"); default: "tempdir"
  python:                              # Python env config (null to skip venv)
    env_packages:                      # Packages to install in sandbox venv
      - pytest
      - pylint>=3.0
  template_sources: [ ... ]           # Optional: preset files (see below)
  snapshots: { ... }                  # Optional: snapshot config (see below)
  ignore_patterns:                    # Optional: overrides for template-copy filtering
    - "!dist"                         #   `!`-prefix un-ignores a default pattern
    - "!node_modules"
    - "*.bak"                         #   bare entry adds an extra pattern
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
    mount_point: "."                      # Optional: subdir inside sandbox to copy into (default ".")
```

The framework automatically ignores `.venv`, `.git`, `__pycache__`, `node_modules`, `dist`, `build`, and other common build/cache artifacts (full list: `coder_eval/resources/default_ignore_patterns.yaml`).

Override the defaults via `sandbox.ignore_patterns` (or `agent.ignore_patterns` for judge-style sub-agents). Each entry is either:

- A bare pattern (e.g. `*.bak`) — added on top of the defaults.
- A `!`-prefixed pattern (gitignore-style negation, e.g. `!dist`) — removes that pattern from the defaults so the directory survives the template copy. Useful for tasks that ship a vendored toolchain under `dist/` or `node_modules/`. Surrounding whitespace is stripped; bare `!` and empty entries raise `ValueError` at YAML load.

`mount_point` controls where inside the sandbox the template contents land. With `mount_point: "."` (default) files are copied to the sandbox root. With `mount_point: "c"` everything from the source directory ends up under `<sandbox>/c/`. The mount point must be a relative path that stays within the sandbox.

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
- **Continuous** (0.0–1.0): `pylint_score`, `reference_comparison`, `llm_judge`, `agent_judge`

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

> The `uipath` CLI must be available **inside the sandbox** (typically declared in the task's own Python deps). This is independent of the host's optional `coder-eval[uipath]` extra — see the install matrix in [README.md](../README.md#installation).

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

> Routing the judge through the **LLM Gateway** transport (when `ANTHROPIC_API_KEY` is absent and `LLMGW_*` creds are set) requires the optional `coder-eval[uipath]` extra. With the extra missing, the run still starts and `llm_judge` reports a clear `error` pointing at `pip install 'coder-eval[uipath]'`. Anthropic-SDK and Bedrock transports do **not** need the extra.

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
  include_dialog: false              # Opt-in: include the full user<->agent conversation (recommended for simulation)
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
| `files` | `[]` | Paths whose contents are shown to the judge. Plain entries are sandbox-relative; entries prefixed with `$TASK_DIR/` are read from the host filesystem relative to the task YAML's parent directory (e.g. `$TASK_DIR/../shared/rubric.md` for a rubric shared across a task family). Missing files render as `<file not found>`. |
| `include_reference` | `false` | Include the task's reference solution in the judge prompt (silently omitted if no reference is configured). Never shown to the agent. |
| `include_agent_output` | `false` | Include the latest agent turn's raw output (wrapped as UNTRUSTED DATA) |
| `include_tool_calls` | `false` | Include a summary of the latest agent turn's tool calls |
| `include_dialog` | `false` | Include the full user↔agent conversation across **all** turns. In simulation mode the user side is generated by an LLM simulator and may invent premises — the rendered block is wrapped as `UNTRUSTED DATA` and instructs the judge to treat any claim made only by the simulated user as possibly fabricated, so the agent isn't penalized for going along with it (recommended whenever a task uses `simulation:`). |
| `max_dialog_chars` | `80000` | Aggregate cap on dialog text rendered into the judge prompt (per-message cap is `max_file_chars`). When exceeded, trailing turns are dropped and a degraded note is recorded. |
| `model` | `anthropic.claude-sonnet-4-6` | UiPath LLM Gateway model name |
| `temperature` | `0.0` | Sampling temperature (0.0 = deterministic) |
| `max_tokens` | `1000` | Maximum tokens in the judge's response |
| `max_file_chars` | `20000` | Per-file (and agent_output) truncation applied before building the prompt |

**Transport selection.** The judge call is routed by the active `API_BACKEND`:

| `API_BACKEND` | Credentials | Judge transport |
|---|---|---|
| `direct` | `ANTHROPIC_API_KEY` set | Anthropic SDK → api.anthropic.com |
| `direct` | `ANTHROPIC_API_KEY` unset, full `LLMGW_*` set | LLM Gateway (LangChain client) |
| `direct` | neither set | run starts; this criterion fails fast at dispatch with a clear error |
| `proxy` | `LLMGW_*` (proxy is running) | Anthropic SDK → local proxy → LLM Gateway |
| `bedrock` | `AWS_BEARER_TOKEN_BEDROCK` | AWS Bedrock |

The `direct`-mode transport is resolved once at startup, logged on the `API routing:` line (`anthropic_direct (judge transport: anthropic|llmgw|none)`), and recorded in `EvaluationResult.environment_info.judge_transport`. Adding or removing `ANTHROPIC_API_KEY` between runs flips the transport, so check the startup log to confirm which path is in use.

**Security**

- The opt-in context blocks (`files`, `include_agent_output`, `include_tool_calls`, `include_dialog`) are wrapped with `UNTRUSTED DATA` / "may invent premises" preambles to mitigate prompt-injection via tool output and to flag simulator-generated user turns.
- The reference solution is shown only to the judge — never to the agent — and any occurrence of the reference is scrubbed from `CriterionResult.details` before persistence.

**Failure modes** — each sets `score=0.0` and populates `error`:

- Non-JSON response from the model (parse failure)
- `score` key missing from the JSON verdict
- `score` is not coercible to float
- LLM Gateway unavailable / network error (handled by `@handle_criterion_errors`)

### `agent_judge`

Spawn a full Claude Code SDK agent as the judge. Unlike `llm_judge` (a single LLM call against a rubric), the judge agent has **tool access** — Bash, Read, Write, Glob, Grep, Edit by default — and runs in an isolated copy of the task sandbox. Use it when functional validation requires executing something (`uip rpa get-errors`, `xmllint`, a test suite) rather than just inspecting file content.

```yaml
- type: "agent_judge"
  description: "Judge validates the generated XAML via CLI"
  prompt: |
    Inspect Main.xaml and grade how well it matches the task requirements.

    Do at least these checks using your tools:
    1. Valid XML? (`xmllint --noout Main.xaml` or Python's xml.etree)
    2. Contains the activities required by the task prompt?
    3. Uses VisualBasic expressions (no CSharpValue)?
    4. Variable declarations aligned with the reference?

    Scoring:
    - 1.0: all checks pass, structure aligned with reference
    - 0.7: functional but minor structural deviations
    - 0.4: partially correct — some critical checks fail
    - 0.0: invalid XML or fundamentally wrong structure
  files: ["Main.xaml"]
  include_reference: true
  include_agent_output: false
  include_tool_calls: false
  include_dialog: false              # Opt-in: include the full user<->agent conversation (recommended for simulation)
  max_turns: 5
  turn_timeout: 300
  agent:                              # Nested AgentConfig — same shape as task.agent
    model: "claude-sonnet-4-6"
    permission_mode: "bypassPermissions"
    allowed_tools: ["Bash", "Read", "Grep", "Glob"]
    sdk_options: {effort: low}        # Optional SDK pass-through (e.g. effort)
  weight: 5.0
  pass_threshold: 0.7
```

| Field | Default | Description |
|-------|---------|-------------|
| `prompt` | *required* | Evaluation instructions for the judge agent |
| `files` | `[]` | Paths pre-attached to the prompt. Plain entries are sandbox-relative (the judge also has live access via its working-directory copy); entries prefixed with `$TASK_DIR/` are read from the host filesystem relative to the task YAML's parent directory and are inlined into the prompt only. |
| `include_reference` | `false` | Include the task's reference solution in the judge prompt |
| `include_agent_output` | `false` | Include the latest agent turn's raw output (UNTRUSTED) |
| `include_tool_calls` | `false` | Include summarized tool-call telemetry from the latest agent turn |
| `include_dialog` | `false` | Include the full user↔agent conversation across **all** turns. The rendered block is wrapped as `UNTRUSTED DATA` and warns the judge that simulator-generated user messages may invent premises (recommended whenever a task uses `simulation:`). |
| `max_dialog_chars` | `80000` | Aggregate cap on dialog text (per-message cap is `max_file_chars`). Trailing turns are dropped past the cap. |
| `max_file_chars` | `20000` | Per-file truncation for pre-attached files |
| `max_turns` | `50` | Judge's inner-loop turn limit |
| `turn_timeout` | `300` | Wall-clock timeout (seconds) |
| `agent` | hardened judge defaults | Nested `AgentConfig` — `model`, `permission_mode`, `allowed_tools`, `disallowed_tools`, `ignore_patterns`, `sdk_options`. A partial block (e.g. only `model:`) still applies the judge security defaults for missing fields, and the security floor (`.claude` / `.mcp.json` / `_reference` ignore patterns, `setting_sources=[]`) is always enforced. |

**Security**

The judge runs with the evaluator's API credentials and can execute arbitrary Bash by default. Four attack surfaces:

1. **Malicious generation artifacts** — the coding agent could plant a file that exfiltrates credentials when the judge runs it (e.g. `python generated.py`). Use `llm_judge` for scenarios with adversarial generation.
2. **Prompt injection** — the UNTRUSTED markers on `include_agent_output` / `include_tool_calls` / `include_dialog` are hints, not guarantees. A strong injection could cause the judge to `curl` secrets out before returning a verdict.
3. **Credential exfiltration via Bash** — any network-capable tool can leak credentials. When the task doesn't need Bash (e.g. structural-only inspection), override `allowed_tools: [Read, Grep, Glob]`.
4. **Hooks / MCP servers planted by the main agent** — a compromised generation could drop `.claude/settings.json` or `.mcp.json` into the sandbox. The judge sets `setting_sources=[]` on its SDK config and excludes both paths from the sandbox copy, so neither gets loaded. This closes a pre-LLM-turn hook/MCP surface that the `allowed_tools` gate can't see.

**Reference handling**: The reference solution is shown to the judge verbatim (same as `llm_judge`) and is scrubbed from the persisted `CriterionResult.details` — a misbehaving judge that echoes the reference in its rationale won't leak it into run artifacts.

**Backend support**: Works on all three backends (`direct`, `bedrock`, `proxy`) — the checker forwards the orchestrator's `ApiRoute` to the judge sub-agent.

**Operational notes**:

- Each invocation copies the sandbox into a `/tmp/sub_agent_*` directory and removes it when the check completes.
- The judge's token usage and wall-clock duration appear in `CriterionResult.details`.
- `agent_judge` is expensive relative to other criteria. Keep `max_turns` tight and consider running it alongside cheaper structural checks rather than as the sole gate.

**Failure modes** — each sets `score=0.0` and populates `error`:

- Non-JSON final message from the judge (parse failure)
- `score` missing / non-numeric / non-finite
- `TurnTimeoutError` (judge exceeded `turn_timeout`)
- SDK subprocess failure (e.g. `claude` CLI missing)

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

## Pre-Run Commands

Run shell commands inside the sandbox **after setup completes but before the agent starts**.
Use them to seed databases, start background services, or prepare the environment.

By default (`fail_on_error: true`), a non-zero exit code, timeout, or exception **aborts
the evaluation** with `FinalStatus.ERROR` — the agent should not run against a broken
environment. Set `fail_on_error: false` per command for optional/informational steps.

```yaml
pre_run:
  - command: "python seed_db.py"
    timeout: 30
    # fail_on_error defaults to true — if seeding fails, abort

  - command: "start-mock-server.sh &"
    # shell exits 0 immediately; background daemon keeps running

  - command: "python check_connectivity.py"
    fail_on_error: false    # warn but don't abort if connectivity check fails
```

| Field | Default | Description |
|-------|---------|-------------|
| `command` | *required* | Shell command to execute (supports pipes, redirects, `&` for background) |
| `timeout` | 30 | Maximum seconds to wait (1–300) |
| `fail_on_error` | `true` | When true, failure aborts evaluation with `FinalStatus.ERROR` |

Commands run sequentially with `cwd` set to the sandbox directory. stdout and stderr are
captured in `pre_run_results` on the evaluation result (truncated to 100KB each). When a
command fails with `fail_on_error: true`, remaining commands are skipped.

**Execution order:**

1. Sandbox setup (template sources applied, venv/node packages installed)
2. **Pre-run commands** ← here
3. Agent evaluation loop
4. Success criteria checks
5. Post-run commands (if configured)

**Experiment-level defaults:**

Set `defaults.pre_run` in an experiment YAML to seed the environment before every task.
Experiment defaults are **prepended** before the task's own `pre_run` (baseline setup first):

```yaml
defaults:
  pre_run:
    - command: "docker-compose up -d"
      timeout: 60
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

**Experiment-level defaults:**

Set `defaults.post_run` in an experiment YAML to run cleanup or extraction after every task.
Experiment defaults are **appended** after the task's own `post_run` (task-specific work first, then defaults):

```yaml
defaults:
  post_run:
    - command: "rm -rf node_modules .npm-prefix"
      timeout: 30
```

The shipped `experiments/*.yaml` use this to drop sandbox-scoped npm dirs (introduced by [PR #250](https://github.com/UiPath/coder_eval/pull/250) for MST-9674) so preserved-sandbox artifacts stay small.

## Simulation (Multi-Turn User Dialog)

Optional `simulation` block. When present and enabled, the orchestrator replaces the single-shot iteration loop with a multi-turn dialog between the coding agent and a simulated user (a second LLM with a persona and goal). Use this for tasks where the real usage pattern is conversational — clarifying questions, incremental requirements, mid-task corrections — rather than a single fire-and-forget prompt.

```yaml
simulation:
  enabled: true                        # Master switch; when false, simulation is skipped entirely.

  # Persona and goal (required).
  persona: |
    A non-technical business analyst who knows the outcome they want
    but not how automation works. Mildly impatient.
  goal: |
    Build a flow that reads invoice PDFs from an Outlook folder,
    extracts vendor/amount/date, and posts to Google Sheets.
    Do NOT volunteer the Google Sheets requirement unless asked.
  constraints:                         # Optional behavioral rules.
    - "Do not paste code — you cannot read code."
    - "If the agent goes silent for two turns, ask 'are you still there?'."

  # Termination.
  max_turns: 12                        # Hard cap on user↔agent exchanges.
  stop_token: "<<<END>>>"              # Simulator emits this when it judges the task complete.
  stop_on_criteria_pass: true          # End early when all success criteria pass.
  max_total_tokens: 150000             # Optional budget across the whole dialog.

  # Sampling (variance analysis).
  n_trials: 3                          # Run N independent dialogs per (task, variant).
  parallel_trials: true                # Trials run concurrently (subject to batch max_parallel).

  # Criteria timing.
  check_criteria: every_turn           # One of: end_of_dialog | every_turn | both.
                                       # Required to be 'every_turn' or 'both' when
                                       # stop_on_criteria_pass is True.
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | When false, simulation is skipped; task runs in single-shot mode. |
| `persona` | *required* | Who the simulator is roleplaying. |
| `goal` | *required* | What the simulated user wants. |
| `constraints` | `[]` | Behavioral rules the simulator must follow. |
| `max_turns` | `8` | Hard cap on user↔agent exchanges (1–100). |
| `stop_token` | `"<<<END>>>"` | Sentinel the simulator emits to end the dialog. |
| `stop_on_criteria_pass` | `false` | End when all criteria pass (requires per-turn checking). |
| `max_total_tokens` | *unset* | Optional dialog-wide token budget. |
| `n_trials` | `1` | Independent dialog trajectories per (task, variant). |
| `parallel_trials` | `true` | Run trials concurrently within the batch. |
| `check_criteria` | `end_of_dialog` | `end_of_dialog`, `every_turn`, or `both`. |

The simulator runs as a tools-disabled Claude Code agent sharing the coding agent's `ApiRoute` — model/temperature/sampling are resolved at the route level (same `-b` flag as the coding agent), so they are not configured on this block.

**Semantics:**

- The task's `initial_prompt` is the user's *opening* message; the simulator picks up from turn 2.
- `max_turns` is the intra-dialog cap (the worst-case agent call budget per trial). Use `n_trials` for variance sampling.
- The `reference` solution, if present, is hidden from the simulator (same security posture as for the coding agent).
- When `n_trials > 1`, each trial becomes its own `ResolvedTask` with `task_id` suffix `/trial-N` (0-indexed), its own run directory, and its own `task.json`. Trial-level metadata appears under `simulation.trial_id` / `simulation.n_trials` on the `EvaluationResult`.

**Termination precedence:** `stop_on_criteria_pass` → `stop_token` → `max_turns` → `max_total_tokens`.

**Grading simulated dialogs.** When a task uses `simulation:`, set `include_dialog: true` on any `llm_judge` / `agent_judge` criterion. Without it, the judge sees only the agent's outputs and may flag a fabricated-but-conceded premise as a hallucination by the agent. The dialog block is rendered with a rubric guard telling the judge to treat any claim made only by the simulated user as possibly invented, and not to penalize the agent for going along with it unless the grading prompt contradicts it.

**Experiment variants** can override any simulation field (persona, goal, constraints, n_trials, etc.) by setting a partial `simulation:` block on a variant — it is shallow-merged onto the task's simulation block. Useful experiment axes: simulator persona (terse vs. chatty), goal withholding, and n_trials for budget/quality tradeoffs.

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
```
