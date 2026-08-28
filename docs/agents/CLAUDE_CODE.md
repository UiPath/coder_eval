---
description: >-
  Configure and run the default Claude Code agent in Coder Eval — the full
  agent-config surface, direct vs. Bedrock authentication, permission modes,
  sandbox isolation, skills/plugins, early stop, and token telemetry.
---

# Running Claude Code in Coder Eval

## Overview

**Claude Code is the default agent** in Coder Eval — `agent.type: claude-code`. It
ships in the base install (no extra needed; the `claude-agent-sdk` is a core
dependency), and most tasks and tutorials assume it. This guide is the reference
for its config surface, authentication, and telemetry; for the agent-agnostic task
schema see the [Task Definition Guide](../TASK_DEFINITION_GUIDE.md).

Because `agent.type` defaults to `claude-code` in `experiments/default.yaml`, you can
omit the `type` field entirely.

## Setup

Nothing beyond the base install:

```bash
uv tool install coder-eval        # or: pip install coder-eval
```

Provide model credentials (see [Authentication](#authentication)) and you're ready:

```bash
coder-eval run tasks/hello_date.yaml     # claude-code is the default
```

## Authentication

Claude Code routing follows the `--backend` flag (or `API_BACKEND` env var):

### Direct (default)

`--backend direct` calls the Anthropic API. The SDK picks its own credential, in
order:

1. `ANTHROPIC_API_KEY` in the environment / `.env`, or
2. a cached `claude login` (subscription) session.

No key is validated at startup — the SDK fails with a clear error if none is found.
The `llm_judge` / `agent_judge` criteria also need `ANTHROPIC_API_KEY` under the
direct backend (they call `api.anthropic.com`).

### AWS Bedrock

`--backend bedrock` routes through Bedrock with bearer-token auth. Set in `.env`:

| Variable | Purpose |
| --- | --- |
| `AWS_BEARER_TOKEN_BEDROCK` | Bedrock bearer token (required) |
| `AWS_REGION` | Bedrock region, e.g. `eu-north-1` (required) |
| `BEDROCK_MODEL` | Cross-region model id, e.g. `eu.anthropic.claude-sonnet-5` (required) |
| `BEDROCK_SMALL_MODEL` | Small/fast model id (falls back to the main model) |

The agent sets `CLAUDE_CODE_USE_BEDROCK=1` and forwards these into the SDK
subprocess. `BEDROCK_MODEL` is the route-level default; an explicit `--model` /
`-D agent.model=…` / task `agent.model` always wins over it, and bare aliases are
auto-qualified to a regional inference profile (`eu.` / `us.` / `apac.` / `global.`).

> **For official benchmarking use the direct API** — the SDK reports accurate
> token/cost there. See [User Guide → API Routing](../USER_GUIDE.md#api-routing--benchmarking).

## Agent config surface

All fields live under `agent:` in a task (or an experiment variant). Only `type` is
required; everything else has a default.

```yaml
agent:
  type: claude-code
  model: claude-sonnet-5                 # optional; omit to use the route default
  permission_mode: acceptEdits           # default | acceptEdits | plan | bypassPermissions
  allowed_tools: ["Read", "Write", "Bash"]
  disallowed_tools: ["WebSearch"]
  setting_sources: []                     # [] = fully isolated (recommended, see below)
  plugins:
    - type: local
      path: "$SKILLS_PLUGIN_PATH"
  system_prompt: "You are a careful engineer."   # or system_prompt_file
  claude_settings:
    permissions:
      deny: ["Read(/etc/**)"]
  sdk_options:
    effort: high
```

| Field | Type / default | Meaning |
| --- | --- | --- |
| `type` | `"claude-code"` | Agent kind (the default). |
| `model` | `str \| null` | Specific model id. Omit to use the backend/route default. |
| `permission_mode` | default **`acceptEdits`** | `default` / `acceptEdits` / `plan` / `bypassPermissions` — semantics come from the Claude Code SDK. `plan` is read-only; `bypassPermissions` grants full autonomy. |
| `allowed_tools` | `list[str] \| null` | Tool allowlist. Unset ⇒ all tools allowed. |
| `disallowed_tools` | `list[str] \| null` | Tool denylist. (`ToolSearch` is always appended for Bedrock parity.) |
| `plugins` | `list[{type: local, path}]` | Local plugin roots; `$VAR` in `path` is expanded and resolved to an absolute path. **`path` must hold a `skills/` subdirectory** — a bare directory of skill directories loads nothing here, though Codex and Antigravity accept it. See [Harness parity](HARNESS_PARITY.md#agentpluginspath-accepts-different-depths-per-harness). |
| `system_prompt` | `str \| null` | **Appended** to the default Claude Code system prompt (via the SDK's `claude_code` preset) — the default's behavioral guidance is kept unless `system_prompt_mode: replace` opts out. Mutually exclusive with `system_prompt_file`. An empty or whitespace-only value is treated as unset. |
| `system_prompt_mode` | `"append"` (default) / `"replace"` | `replace` sends `system_prompt` as the **entire** system prompt (no preset) and requires a non-blank `system_prompt` / `system_prompt_file` (validated at load). Used by judge sub-agents and the user simulator, which must not carry the coding-agent persona; rarely needed in tasks — see [the migration note](#migrating-tasks-that-set-system_prompt). |
| `system_prompt_file` | `str \| null` | Path (relative to the task YAML) loaded into `system_prompt` at resolution. Works with either `system_prompt_mode`. |
| `setting_sources` | `list["user"\|"project"\|"local"] \| null` | Which host setting sources the SDK reads. Default resolves to `["project"]`. See [Sandbox isolation](#sandbox-isolation). |
| `claude_settings` | `str \| dict \| null` | Passed to the SDK `--settings`. A dict is JSON-serialized; a str is a settings file path. Use `permissions.deny` to block tools/paths. |
| `sdk_options` | `dict` (default `{}`) | Pass-through for `ClaudeAgentOptions` fields Coder Eval doesn't own (e.g. `effort`). Validated at load — an unknown or framework-owned key is a hard error. |
| `ignore_patterns` | `list[str] \| null` | Gitignore-style overrides for the workspace copy used by judge sub-agents (supports `!` negation). |

> `sdk_options` is a deliberate escape hatch. Framework-owned keys (`model`,
> `permission_mode`, `allowed_tools`, `mcp_servers`, `resume`, `max_turns`,
> `setting_sources`, `include_partial_messages`, …) are rejected there — set those
> through their typed fields or `-D run_limits.*`. MCP servers are not a YAML field.

> **System-prompt reproducibility.** In `append` mode the preset's *dynamic
> sections* (working directory, git status, auto-memory) are excluded so the system
> prompt stays identical across runs — the per-run sandbox tempdir path would
> otherwise be baked into it, breaking prompt caching and run comparability. The
> SDK re-injects the stripped content into the first user message, so the agent
> loses nothing. Note the default-prompt baseline tracks the installed Claude Code
> CLI version; `environment_info.claude_code_cli` in `run.json` records which
> version a run used, and `environment_info.system_prompt_semantics`
> (`append` / `replace`) records the prompt regime — runs predating that marker
> used replace-on-set / empty-on-unset semantics and are not score-comparable.

### Migrating tasks that set `system_prompt`

`system_prompt` used to **replace** Claude Code's default system prompt. It now
appends to it. If your task or experiment sets `system_prompt`, the agent gains back
every default behavioral instruction it was previously running without — parallel
tool-call batching, conciseness rules, the `Read`/`Grep`/`Glob` tool preferences, and
the default security guardrails.

That is a genuine behavior change, so **scores are not comparable across this
boundary**. Pick one:

- **Keep the repair (recommended).** Do nothing. Re-baseline any threshold or
  reference score the task gates on, and expect turn counts to drop on tasks that
  depend on batched tool calls.
- **Preserve the old behavior.** Add `system_prompt_mode: replace` to the `agent:`
  block. The configured prompt again becomes the entire system prompt. Only do this
  if the task *intends* to run without the default guidance — a prompt that merely
  adds sandbox policy or a persona does not.

Segment dashboards on `environment_info.system_prompt_semantics` to keep the two
regimes in separate cohorts. Note the append-mode baseline also tracks the installed
CLI version, so a `CLAUDE_CODE_VERSION` bump becomes a score-affecting change
(attributable via `environment_info.claude_code_cli`).

> **Consumers reading `sdk_options.system_prompt`.** The persisted value changed
> shape: a plain `str` in the old regime, a `SystemPromptPreset` dict
> (`{type, preset, exclude_dynamic_sections, append}`) in append mode. Code that
> string-handles that field needs to branch on the type — see
> [Report schema](../REPORT_SCHEMA.md).

### Setting fields from the CLI

Any of these merge-resolve through `-D` / `--set` (see
[User Guide → CLI overrides](../USER_GUIDE.md#cli-commands)):

```bash
coder-eval run tasks/hello_date.yaml \
  -D agent.model=claude-opus-5 \
  -D agent.permission_mode=plan \
  -D agent.sdk_options.effort=high
```

## Sandbox isolation

`setting_sources` controls whether the host project's settings leak into the run.
The default (`["project"]`) lets the SDK discover `.mcp.json` — but it also pulls in
the host project's `CLAUDE.md`, settings, and hooks, which for this repo is 20 KB+
injected into every API call (inflating cache-creation tokens and cost).

**For tasks that don't need host MCP servers, set `setting_sources: []`** for full
isolation from the host `CLAUDE.md`/settings/hooks. (Judge sub-agents and the user
simulator force `[]` for the same reason.)

## Skills & plugins

- **Skills** are engaged via Claude's explicit `Skill` tool call; the
  [`skill_triggered`](../TASK_DEFINITION_GUIDE.md#skill_triggered) criterion detects
  that call and scores skill-activation suites.
- **Plugins** are supplied as `plugins: [{type: local, path: …}]`; the path is
  env-expanded and resolved to an absolute path before being handed to the SDK.
  It must name a **plugin root** — a directory holding `skills/`, so the skill
  resolves at `<path>/skills/<name>/SKILL.md`. One level deeper loads nothing, with
  no error: every positive row of an activation suite then scores 0 and the suite
  reports recall 0.0, which reads exactly like a skill that never triggers. Codex
  and Antigravity scan both depths, so this is claude-code-only — see
  [Harness parity](HARNESS_PARITY.md#agentpluginspath-accepts-different-depths-per-harness).
- A plugin root loads **everything** the plugin declares, not only `skills/`: an
  `agents/`, `commands/` or `hooks/` directory beside it becomes visible to the
  evaluated agent too. Point at a minimal root when the suite must measure one
  skill in isolation.
- **`PLUGIN_TOOLS_DIR`** pins the canonical `node_modules/@uipath` directory for
  UiPath CLI plugin discovery; when unset the sandbox derives it from the resolved
  `uip` binary. See [User Guide → Environment Variables](../USER_GUIDE.md#environment-variables).

## Early stop

Claude Code supports the cooperative early-stop seam, as do the
[Codex](CODEX.md) and [Antigravity](ANTIGRAVITY.md) agents. When a criterion
carries a `stop_early:` block, a single-shot run ends cleanly at the next
tool-call boundary once its **armed** criteria (those carrying a
`stop_early:` block) are
decided — so a raised `max_turns` isn't wasted on a smoke run. Early stop errors at
resolution for any agent that does not declare `supports_cooperative_stop`. See the
[Task Definition Guide](../TASK_DEFINITION_GUIDE.md) for the full contract.

## Telemetry

Claude Code produces the richest telemetry of the agents:

- **Authoritative billing** comes from the SDK's cumulative `model_usage` on the
  terminal result message and reconciles to `total_cost_usd` — this already includes
  sub-agent consumption that the per-message stream under-reports.
- **Sub-agent accounting** is derived by grouping `parent_tool_use_id`-tagged
  assistant messages; there is no separate per-sub-agent field. The terminal
  sub-agent generation (delivered as the Agent tool result, never streamed) is
  synthesized into one message.
- **Reconciliation message.** Because the per-message stream slightly under-reports
  the turn total (a fixed ~512-token input slice is billed on no streamed message,
  and sub-agent input/cache only partially bubbles up), each turn carries one
  synthetic `role="reconciliation"` entry holding the per-bucket residual. The
  invariant: summing the token buckets across `TurnRecord.messages` equals the turn
  total exactly. It carries no cost and is excluded from turn/generation counts.

Set the `CODER_EVAL_RAW_SDK_LOG` environment variable to `1` to dump every raw SDK
event to the task log.

## Lifecycle & robustness

- **Session resume** across turns via the SDK `resume` option; the session id only
  advances on clean (non-error) turns.
- **Timeouts** are enforced by a `ThreadedWatchdog` (an OS-thread timer immune to
  event-loop stalls) plus an in-loop wall-clock guard; on breach it SIGKILLs the CLI
  subprocess and raises `TurnTimeoutError` with a partial `TurnRecord` preserved.
- **Crashes** raise `AgentCrashError` with assembled stderr; the orchestrator drains
  the partial turn and rolls back. Cost is backfilled from the rate card when a run
  is killed before a terminal result message arrives.

## References

- [Task Definition Guide](../TASK_DEFINITION_GUIDE.md) — the full task/criterion schema
- [User Guide](../USER_GUIDE.md) — CLI, backends, environment variables
- [Codex Agent Guide](CODEX.md) · [Antigravity (Gemini) Agent Guide](ANTIGRAVITY.md)
- [Extending Coder Eval](../EXTENDING.md) — how agents register via the plugin SPI
