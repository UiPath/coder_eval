---
allowed-tools: Read(*), Glob(*), Grep(*), Bash(ls:*), Bash(uv run coder-eval plan:*), Write(tasks/*), Agent
description: Create evaluation task YAML files from a natural language description
---

## Context

You are creating coder_eval task YAML files. The user's request is: `$ARGUMENTS`

This skill generates well-structured, minimal task definitions that follow project conventions. Tasks use simple prompts — state the goal and expected output, let the agent figure out the approach.

A single user request can produce **multiple task files** — e.g., "create tasks for all uip maestro flow registry subcommands" should produce one task per subcommand (pull, list, search, get, etc.).

## Step 1: Understand the Request

Parse the user's description to determine:
- **What the task tests** — which tool, SDK, CLI, or capability?
- **How many tasks** — does the description cover one operation or multiple? If the user says "create tasks for X, Y, and Z" or "create tasks for all <subcommands>", produce one task file per distinct operation.
- **Difficulty** — smoke test, basic, intermediate, or complex?
- **Dependencies** — does it need network, packages, template files, or external services?

If the description is vague, make reasonable assumptions and note them in the output.

## Step 2: Research Existing Tasks

Before creating, check for overlap:
- Use `Glob` to find existing task files: `tasks/**/*.yaml`
- Use `Grep` to search for related task IDs or keywords
- If similar tasks exist, read them to follow the same conventions (naming, tags, criteria patterns)
- If the task already exists, tell the user and suggest modifications instead

Also read `docs/TASK_DEFINITION_GUIDE.md` for the authoritative reference on task structure.

## Step 3: Design the Task

### Task ID
- Lowercase kebab-case, unique, descriptive
- Pattern: `<domain>-<action>` (e.g., `uipath-process-list`)

### Initial Prompt
- **Keep it minimal**: State the goal and expected output. Let the agent figure out the approach.
- Example: "Use the `uip` CLI to list Flow processes and save the results to processes.json."
- **Key rule**: The prompt should read like a natural human request, NOT leak implementation details that criteria check. Criteria validate; prompts instruct.
- Don't include step-by-step instructions, specific flags, or format specifications — that's what makes a task a good test of agent capability.

### Success Criteria
Choose criteria types based on what needs to be verified:

| What to check | Criterion type | When to use |
|---------------|---------------|-------------|
| File was created | `file_exists` | Basic existence check |
| File has expected content | `file_contains` | String presence/absence |
| File + content + regex | `file_check` | Unified check (preferred over file_exists + file_contains) |
| JSON structure/values | `json_check` | JSON validation + JMESPath assertions |
| Script runs / tests pass | `run_command` | Exit code + optional stdout matching or float scoring (e.g. run `pytest` and check exit code / parse output) |
| Regex match on file | `file_matches_regex` | Binary regex match on file content |
| Code similarity vs reference | `reference_comparison` | AST/token/complexity/quality similarity vs a reference solution |
| Subjective / open-ended quality | `llm_judge` | An LLM grades the artifacts (+ optional trajectory / reference) against a rubric prompt |
| Deep, tool-using verdict | `agent_judge` | Spawns a Claude Code SDK sub-agent to investigate the sandbox and return a JSON verdict (expensive) |
| Agent used a specific tool | `command_executed` | Verify the agent ran expected commands |
| Agent tool-call efficiency | `commands_efficiency` | Score tool-call count against an expected budget |
| Agent engaged a skill | `skill_triggered` | Did the agent invoke the target skill (Skill tool / file read)? |
| Observed vs expected label | `classification_match` | File-based label match for classification suites (emits P/R/F1) |
| UiPath agent eval | `uipath_eval` | UiPath agent evaluation results |

**Criteria design rules:**
- Every task needs at least one criterion that validates the **output content**, not just existence
- Use `run_command` with `expected_stdout` + `stdout_match: regex` to validate script output
- Use `command_executed` sparingly — only when verifying the agent used a specific tool matters. Set `require_success: false` unless the command must succeed.
- Use `file_check` instead of separate `file_exists` + `file_contains` when checking the same file
- Set `weight` to reflect importance: 0.5 for nice-to-have, 1.0 for standard, 1.5-2.0 for critical
- Default `pass_threshold: 0.9` is fine for most criteria. Use `1.0` only for binary checks.

### Sandbox Configuration
```yaml
sandbox:
  driver: "tempdir"
  python: {}              # Creates venv with no extra packages
  # python:
  #   env_packages: [pytest, requests]  # If packages are needed
```

Add `template_sources` if the task needs starter files (e.g., a pre-existing codebase to modify).

### Agent Configuration
Only include if the task needs non-default settings:
```yaml
agent:
  type: "claude-code"
  permission_mode: "acceptEdits"
  allowed_tools: ["Bash", "Read", "Write"]  # Minimal set needed

run_limits:
  max_turns: 15                              # Estimate: ~2x expected commands
```

### Tags
Apply relevant tags from the project conventions:
- **Difficulty**: `smoke`, `basic`, `intermediate`
- **Quality**: `golden` (high-confidence reference tasks)
- **Content**: `pure-python`, `network`, `integration`
- **Domain**: `flow`, `is`, `uipcli`, `uipath-python`, `uipath-langchain`

## Step 4: Write the Task File(s)

Write YAML file(s) to `tasks/` using the appropriate subdirectory. If creating multiple tasks, write them all:
- `tasks/` for general tasks
- `tasks/uipath_flow/` for UiPath Flow tasks
- `tasks/uipath_is/` for Integration Service tasks
- Create new subdirectories if needed for a new domain

**File naming**: `<task_id_with_underscores>.yaml` (convert kebab-case ID to snake_case filename)

### Task YAML Template

```yaml
task_id: "<kebab-case-id>"
description: "<one-line description of what this task tests>"
initial_prompt: |
  <natural language instruction for the agent>
tags: [<tag1>, <tag2>]

sandbox:
  driver: "tempdir"
  python: {}

success_criteria:
  - type: "<criterion_type>"
    description: "<human-readable description>"
    # ... type-specific fields
    weight: 1.0
```

**Note**: Do NOT include an `agent` block unless the task specifically needs non-default settings (e.g., restricted `allowed_tools`, specific `max_turns`). The agent config is resolved from the experiment's 5-layer merge — hardcoding it in every task defeats experiment-level control.

## Step 5: Validate

After writing, validate each task file:
- Run `uv run coder-eval plan tasks/<new_file>.yaml` to validate the schema through Pydantic. Fix any errors reported.
- Verify all criteria reference files/commands that the prompt instructs the agent to create
- Ensure the prompt doesn't leak criteria details (e.g., don't say "make sure the file contains X" if a file_contains criterion checks for X)

## Output

After creating the task(s), summarize in a table:

| File | Task ID | Criteria | Tags |
|------|---------|----------|------|
| tasks/domain/foo.yaml | domain-foo | 4 | smoke, basic |
| tasks/domain/bar.yaml | domain-bar | 3 | basic |

Then list:
- Any assumptions made
- How to run: `uv run coder-eval run tasks/<path>/*.yaml` (or individual file paths)

## Criteria Type Reference

For exact YAML syntax, fields, and examples for all criteria types, **read `docs/TASK_DEFINITION_GUIDE.md`** using the Read tool. The guide is the single source of truth.

Quick reference of available types:

| Type | Scoring | Key fields |
|------|---------|------------|
| `file_exists` | Binary | `path` |
| `file_contains` | Fractional | `path`, `includes`, `excludes` |
| `file_check` | Fractional | `path`, `includes`, `excludes`, `patterns` (preferred over file_exists + file_contains) |
| `json_check` | Fractional | `path`, `schema`, `assertions` (JMESPath) |
| `run_command` | Binary / Continuous | `command`, `expected_exit_code`, `expected_stdout`, `stdout_match` |
| `file_matches_regex` | Binary | `path`, `pattern` |
| `reference_comparison` | Continuous | `agent_file`, `reference_file` (requires `reference` block) |
| `command_executed` | Fractional | `tool_name`, `command_pattern`, `min_count`, `require_success` |
| `commands_efficiency` | Continuous | `expected_commands` |
| `classification_match` | Binary | `path`, `expected_label`, `allowed_labels`, `case_sensitive` |
| `skill_triggered` | Binary | `expected_skill`, `skill_name` |
| `llm_judge` | Continuous | `prompt`, `files`, `include_reference`, `include_agent_output`, `include_tool_calls` |
| `agent_judge` | Continuous | `prompt`, `files`, `include_*` (runs with evaluator creds — see the guide's SECURITY note) |
| `uipath_eval` | Fractional | `agent_name`, `eval_set`, `thresholds` |
