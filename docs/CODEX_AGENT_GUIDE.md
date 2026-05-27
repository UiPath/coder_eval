# Codex Agent Implementation

## Overview

A new `CodexAgent` has been added to coder_eval that integrates OpenAI's Codex SDK. The implementation mirrors the structure of `ClaudeCodeAgent` and provides seamless integration with the evaluation framework.

## Setup

### 1. Install the Codex SDK

Install coder-eval with the `codex` extra:

```bash
pip install 'coder-eval[codex]'
```

This installs:
- `openai-codex` - The official Codex Python SDK (pinned via git in `pyproject.toml`)
- `openai-codex-cli-bin` - Platform-specific Codex CLI binaries (pulled in transitively)

### 2. Authentication

Codex requires authentication. Options:

```python
# Option 1: API Key (direct)
await codex_client.login_api_key("your-api-key")

# Option 2: ChatGPT (interactive)
await codex_client.login_chatgpt()

# Option 3: Device Code Flow
await codex_client.login_chatgpt_device_code()
```

`CodexAgent.start()` calls `login_api_key` automatically when `CODEX_API_KEY`, `OPENAI_API_KEY`, or `AZURE_OPENAI_API_KEY` is present in the environment. Without a key it falls back to any existing ChatGPT login.

## Usage

### Command Line

Run a task with Codex agent:

```bash
coder-eval run tasks/codex_example.yaml --type codex
```

Or override agent type for all tasks in an experiment:

```bash
coder-eval run experiments/example.yaml --type codex
```

### Task Definition (YAML)

Specify Codex in task YAML:

```yaml
agent:
  type: codex
  permission_mode: acceptEdits
  allowed_tools:
    - Bash
    - Read
    - Write
  disallowed_tools:
    - Edit
  plugins:
    - type: local
      path: "$PLUGIN_PATH"

success_criteria:
  - type: file_exists
    path: "src/solution.py"
    description: "Solution file must exist"
```

Valid `permission_mode` values:
- `default` - Standard access, requires approval on failure
- `acceptEdits` - Automatically accept file edits, no filesystem restrictions
- `plan` - Read-only sandbox, approval required for any changes
- `bypassPermissions` - Full access, no approvals needed

### Skills (SKILL.md)

CodexAgent supports SKILL.md files following the [Agent Skills open standard](https://agentskills.io/specification). Skills are discovered from:

1. **config.plugins** - Local plugins with `type: local` and `path` pointing to a skills directory
2. **plugin_tools_dir** parameter - Runtime plugin directory passed to `start()`

Skills are symlinked (or copied) to `.agents/skills/` where the Codex CLI auto-discovers them. Environment variables in plugin paths (`$VAR`, `${VAR}`) are expanded at runtime.

Example with environment variable:
```yaml
agent:
  type: codex
  plugins:
    - type: local
      path: "$SKILLS_PLUGIN_PATH"
```

Set environment variable:
```bash
export SKILLS_PLUGIN_PATH=~/uipath/uipath-claude-plugins/plugins/uipath-coded-agents
coder-eval run tasks/my_task.yaml
```

## Architecture

### Class Hierarchy

```
Agent (ABC)
└── CodexAgent
    ├── Codex SDK Client (openai_codex.Codex)
    ├── Thread Management (thread_start, turn.stream)
    └── Streaming telemetry (commands, token usage, agent text)
```

### Key Methods

- **`start(working_directory)`** - Initialize Codex client and set working directory
- **`communicate(user_input, timeout, stream_callback)`** - Execute one turn with Codex
- **`stop()`** - Clean up resources
- **`get_state()`** - Return current agent state
- **`discard_pending_turn()`** - Rollback on failure

### TurnRecord Format

Each turn returns a `TurnRecord` with:
- `iteration` - Turn number
- `user_input` - The prompt sent
- `agent_output` - assembled from the streamed `agentMessage` deltas
- `commands` - `CommandTelemetry` for each shell command (`Bash`) and apply_patch file change (`Write`)
- `timestamp` - When the turn completed
- `duration_seconds` - Wall-clock execution time
- `token_usage` - input/output/cache-read token counts (from the SDK token-usage stream)
- `model_used` - the pinned `agent.model`, when set

## Implementation Details

### Timeout Handling

The agent uses a `ThreadedWatchdog` to enforce wall-clock timeouts. If a turn exceeds the deadline, a `TurnTimeoutError` is raised with a partial `TurnRecord` preserved in `pending_turn`.

### Error Recovery

On failure, the agent:
1. Sets `pending_turn` to a `crashed=True` TurnRecord with captured telemetry
2. Raises `AgentCrashError` or `TurnTimeoutError`
3. The orchestrator reads `pending_turn` and calls `discard_pending_turn()` to roll back state

### Permission and Tool Mapping

The agent maps `permission_mode` to the Codex SDK's `SandboxMode` and `ApprovalMode` when starting a thread:

| `permission_mode` | `sandbox` | `approval_mode` |
|-------------------|-----------|-----------------|
| `bypassPermissions` | `danger-full-access` | `auto_review` |
| `acceptEdits` | `workspace-write` | `auto_review` |
| `default` | `workspace-write` | `auto_review` |
| `plan` | `read-only` | `deny_all` |

`allowed_tools` / `disallowed_tools` are normalized (`Bash` → `shell`, `Write`/`Edit` → `apply_patch`, etc.) and passed as `enabled_tools` / `disabled_tools` in the thread `config`. **Note:** the Codex SDK does not currently enforce `disabled_tools`; do not rely on it as a security boundary (the agent logs a warning when it is set).

### Skills Discovery

The agent sets up SKILL.md files (Agent Skills open standard) in `.agents/skills/` directory:

1. Scans `config.plugins` for local plugins with `path` field
2. Checks `plugin_tools_dir` parameter passed to `start()`
3. Expands environment variables in paths (`$PLUGIN_PATH`, `${PLUGIN_PATH}`)
4. Symlinks skill directories (falls back to copying if symlink fails)
5. Codex CLI auto-discovers skills in `.agents/skills/`

### Async Integration

The Codex SDK is synchronous. The agent uses `_run_async()` helper to detect and await coroutines, preserving the async interface.

## Differences from Claude Code Agent

| Feature | Claude Code | Codex |
|---------|------------|-------|
| **SDK Type** | Subprocess (CLI via JSON generator) | Sync client (app-server subprocess) |
| **Command Tracking** | Full telemetry (tool name, params, duration) | Streamed telemetry: shell → `Bash`, apply_patch → `Write` |
| **Model Selection** | Direct via `--model` or config | `agent.model` pinned into `thread_start` |
| **Session Resume** | `--resume {session_id}` | Via thread ID |
| **Permissions** | `permission_mode` + `allowed_tools` | `permission_mode` → sandbox/approval + `allowed_tools`/`disallowed_tools` → thread config |
| **Tool Enforcement** | Not enforced by coder_eval wrapper | `enabled_tools` honored; `disabled_tools` NOT enforced by the SDK |

## Known Limitations

1. **Tool-name collapse** - Codex reports shell tools (`Read`/`Grep`/`Bash`) all as shell commands, surfaced as `Bash` telemetry; name-keyed criteria that distinguish these tools aren't meaningful across agents.
2. **`skill_triggered` criterion** - Codex invokes skills as shell commands (no distinct `Skill` tool), so a `tool_name == "Skill"` match will not fire for Codex.
3. **`disallowed_tools`** - passed to the SDK but not enforced; not a security boundary.
4. **Authentication** - Requires `CODEX_API_KEY` or `OPENAI_API_KEY` (or Azure equivalents) in the environment; the agent calls `login_api_key` when a key is present.
5. **Model field** - `TurnRecord.model_used` reflects the pinned `agent.model`; the Codex `Turn` payload itself doesn't carry the resolved model.
6. **Skills with Windows paths** - Symlink creation may fail on Windows; agent falls back to copying (slower).

## Future Enhancements

- [ ] Implement session-based resume (thread ID tracking)
- [ ] Surface a `Skill`-typed signal so `skill_triggered` works for Codex
- [ ] Capture the resolved model from the SDK (vs. the pinned config value)

## Testing

Run the included test tasks:

```bash
# Basic functionality test
coder-eval run tasks/codex_hello_world.yaml

# Tool restriction test (verifies disallowed_tools enforcement)
coder-eval run tasks/codex_disallowed_tools_test.yaml

# Skills discovery test (requires PLUGIN_PATH environment variable)
export PLUGIN_PATH=~/path/to/skills
coder-eval run tasks/codex_skills_test.yaml
```

Example unit test to verify agent setup:

```python
import pytest
from coder_eval.models import AgentKind, AgentConfig
from coder_eval.agents.codex_agent import CodexAgent
from coder_eval.agent import AgentState

def test_codex_agent_initialization():
    """Verify CodexAgent can be instantiated with valid config."""
    config = AgentConfig(
        type=AgentKind.CODEX,
        permission_mode="acceptEdits",
        allowed_tools=["Bash", "Read", "Write"],
    )
    agent = CodexAgent(config)
    assert agent.get_state() == AgentState.WORKING
    assert agent.config.type == AgentKind.CODEX

def test_tool_name_mapping():
    """Verify Claude Code tool names map to Codex SDK names."""
    from coder_eval.agents.codex_agent import _CLAUDE_TO_CODEX_TOOL_MAP

    assert _CLAUDE_TO_CODEX_TOOL_MAP["Bash"] == "shell"
    assert _CLAUDE_TO_CODEX_TOOL_MAP["Write"] == "apply_patch"
    assert _CLAUDE_TO_CODEX_TOOL_MAP["Edit"] == "apply_patch"
    assert _CLAUDE_TO_CODEX_TOOL_MAP["Read"] == "shell"

def test_permission_mode_mapping():
    """Verify permission_mode values map to ThreadOptions."""
    from coder_eval.agents.codex_agent import (
        _PERMISSION_MODE_TO_SANDBOX,
        _PERMISSION_MODE_TO_APPROVAL,
    )

    assert _PERMISSION_MODE_TO_SANDBOX["acceptEdits"] == "workspace-write"
    assert _PERMISSION_MODE_TO_APPROVAL["acceptEdits"] == "auto_review"
    assert _PERMISSION_MODE_TO_SANDBOX["plan"] == "read-only"
    assert _PERMISSION_MODE_TO_APPROVAL["plan"] == "deny_all"
```

## References

- [Codex SDK Documentation](https://developers.openai.com/codex/sdk)
- [Codex CLI Guide](https://developers.openai.com/codex/cli)
- [Codex GitHub Repository](https://github.com/openai/codex)
