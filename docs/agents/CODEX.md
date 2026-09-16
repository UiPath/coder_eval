---
description: >-
  Run OpenAI Codex as the agent under evaluation in Coder Eval — installation,
  authentication, task configuration, and how Codex telemetry maps to sandboxed,
  weighted scoring.
---

# Running OpenAI Codex in Coder Eval

## Overview

Coder Eval can run OpenAI's Codex as the agent under evaluation, via the official Codex SDK. The `CodexAgent` mirrors the structure of `ClaudeCodeAgent` and plugs into the same sandbox, scoring, and telemetry pipeline — set `agent.type: codex` in a task and the rest of the framework works unchanged.

## Setup

### 1. Install the Codex SDK

Install coder-eval with the `codex` extra:

```bash
pip install 'coder-eval[codex]'
```

This installs:
- `openai-codex` - The official Codex Python SDK (from PyPI)
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

`CodexAgent.start()` calls `login_api_key` automatically when **`CODEX_API_KEY`** is present in the environment. Without a key it falls back to any existing ChatGPT login. (Only `CODEX_API_KEY` is read — not `OPENAI_API_KEY`/`AZURE_OPENAI_API_KEY`; point `CODEX_API_KEY` at whichever endpoint's key you use.)

### Endpoint routing

| Env var | Purpose |
|---|---|
| `CODEX_API_KEY` | Auth key/token for the selected endpoint (required for headless runs). |
| `CODEX_BASE_URL` | Route to a custom endpoint. **Unset → standard OpenAI platform** (api.openai.com). Set → custom provider (gateway or Azure). |
| `CODEX_MODEL` | Fallback model when `agent.model` is unset. On Azure this is the **deployment name**. |
| `CODEX_API_VERSION` | Azure only: the required `api-version` query param. Leave unset for OpenAI/gateways. |

**Standard OpenAI:** leave `CODEX_BASE_URL` unset, set `CODEX_API_KEY` to an OpenAI `sk-…` key and `CODEX_MODEL` to a Codex/Responses-capable model.

**Azure OpenAI:**
```bash
CODEX_BASE_URL=https://<your-resource>.openai.azure.com/openai
CODEX_API_VERSION=2025-04-01-preview   # required by Azure
CODEX_MODEL=<your-deployment-name>     # deployment, not the base model id
CODEX_API_KEY=<azure-openai-key>
```
This registers a custom Codex model provider (`base_url` + `env_key=CODEX_API_KEY` + `query_params={api-version}` + `wire_api=responses`). The Codex CLI only supports the Responses wire API (it rejects `wire_api=chat` as "no longer supported"), so the protocol is fixed. If your Azure deployment requires the key in an `api-key` header rather than `Authorization: Bearer`, that needs an additional provider `http_headers`/`env_http_headers` entry — open an issue if you hit that.

## Usage

### Command Line

Run a task with Codex agent:

```bash
coder-eval run tasks/agents/codex_hello_world.yaml --type codex
```

Or override agent type for all tasks in an experiment:

```bash
coder-eval run experiments/model-comparison.yaml --type codex
```

### Task Definition (YAML)

Specify Codex in task YAML:

```yaml
agent:
  type: codex
  plugins:
    - type: local
      path: "$PLUGIN_PATH"

success_criteria:
  - type: file_exists
    path: "src/solution.py"
    description: "Solution file must exist"
```

A Codex task that sets `permission_mode`, `allowed_tools` or `disallowed_tools` is
rejected at resolution: Codex honors none of them (see
[Permission and Tool Mapping](#permission-and-tool-mapping)).

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

Per-field contract (generated): [Harness Parity § Agent-field contract](HARNESS_PARITY.md#agent-field-contract). `permission_mode`, `allowed_tools` and
`disallowed_tools` are rejected at load on Codex.

Codex runs with `sandbox: full-access` and `approval_mode: deny_all` on every run. Its own OS sandbox fails silently on the hosts Coder Eval runs on, so the isolation boundary is the task's driver: use `driver: docker` for untrusted evals.

`deny_all` means *run autonomously, never prompt, no server-side reviewer*. Coder Eval uses it because the alternative (`auto_review`) adds a server-side reviewer that can spuriously return `declined` under gateway load.

Codex honors none of `permission_mode`, `allowed_tools` and `disallowed_tools`. Its config has no top-level key that restricts the built-in `shell` / `apply_patch` tools (`enabled_tools` / `disabled_tools` exist only per MCP server), so Coder Eval forwards nothing for them.

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
| **System prompt** | `system_prompt` appended to the default prompt (SDK `claude_code` preset) | `system_prompt` passed as `developer_instructions` on top of the Codex base prompt |
| **Session Resume** | `--resume {session_id}` | Via thread ID |
| **Permissions** | `permission_mode` + `allowed_tools` + `disallowed_tools` | Not supported; always full-access |
| **`max_turns`** | Native SDK turn cap (assistant messages) | Visible-turn cap (tool calls), enforced on the notification pump |
| **Early stop** | Supported (cooperative `should_stop`, polled between messages) | Supported — polled after each streamed notification; the in-flight turn is interrupted best-effort |

Run-limit semantics per harness: [Run-Limit Parity](HARNESS_PARITY.md).

## Known Limitations

1. **Tool-name collapse** - Codex reports shell tools (`Read`/`Grep`/`Bash`) all as shell commands, surfaced as `Bash` telemetry; name-keyed criteria that distinguish these tools aren't meaningful across agents.
2. **`skill_triggered` criterion** - Codex has no distinct `Skill` tool (it engages a skill by reading its files via shell), so the criterion detects Codex engagement from that file-read signal (a command referencing `skills/<name>/`) instead of a `Skill` tool call. The file-read signal is weaker than Claude's explicit invocation.
3. **`permission_mode`, `allowed_tools`, `disallowed_tools`** - Codex honors none of them; a Codex task that sets any of them is rejected at load.
4. **Authentication** - Requires `CODEX_API_KEY` in the environment (point it at whichever endpoint's key you use — OpenAI, gateway, or Azure); the agent calls `login_api_key` when a key is present. `OPENAI_API_KEY`/`AZURE_OPENAI_API_KEY` are NOT read.
5. **Model field** - `TurnRecord.model_used` reflects the pinned `agent.model`; the Codex `Turn` payload itself doesn't carry the resolved model.
6. **Skills with Windows paths** - Symlink creation may fail on Windows; agent falls back to copying (slower).
7. **No `system_prompt_mode`** - `replace` semantics are Claude-Code-only. `system_prompt` is always appended as `developer_instructions`; setting `system_prompt_mode` on a Codex `agent:` block is a validation error (unknown field).

## Migrating tasks that set `system_prompt`

`system_prompt` was previously **ignored** on Codex tasks — silently dropped, so the
task ran on Codex's base prompt alone. It is now forwarded as
`developer_instructions`, layered on top of that base prompt. Any Codex task setting
the field now actually receives those instructions, so **scores are not comparable
across this boundary**. Two things to check:

- A prompt written for Claude (naming `Read`/`Grep`/`Glob`, or Claude tool etiquette)
  is now live on Codex, where those tool names don't exist.
- There is **no opt-out** (see Known Limitations #7). To restore the old behavior,
  remove `system_prompt` from the Codex variant — otherwise re-baseline.

## Future Enhancements

- [ ] Implement session-based resume (thread ID tracking)
- [ ] Strengthen the Codex `skill_triggered` signal — it currently infers engagement from a file read, weaker than Claude's `Skill` tool call
- [ ] Capture the resolved model from the SDK (vs. the pinned config value)

## Testing

Run the included test tasks:

```bash
# Basic functionality test
coder-eval run tasks/agents/codex_hello_world.yaml

# Skills discovery test (requires PLUGIN_PATH environment variable)
export PLUGIN_PATH=~/path/to/skills
coder-eval run tasks/agents/codex_skills_test.yaml
```

## References

- [Codex SDK Documentation](https://developers.openai.com/codex/sdk)
- [Codex CLI Guide](https://developers.openai.com/codex/cli)
- [Codex GitHub Repository](https://github.com/openai/codex)
