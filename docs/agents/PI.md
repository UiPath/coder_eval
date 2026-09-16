---
description: >-
  Run Pi, the pi Node coding agent, as the agent under evaluation in Coder
  Eval — installation, provider authentication, model selection, the enforced
  and divergent config fields, and how its event stream maps to sandboxed,
  weighted scoring.
---

# Running Pi in Coder Eval

## Overview

[Pi](https://pi.dev/) is a Node terminal coding agent (the `pi` CLI). Coder Eval
drives it in **JSON print mode**:

```bash
pi -p --mode json --no-context-files --no-approve \
   --session-dir <dir> --session-id <id> --model <provider/id> -- "<prompt>"
```

`--mode json` streams **newline-delimited JSON events** on stdout, one event per
line. `PiAgent` reduces that stream into the standardized event protocol
(`AgentStart` / `TurnStart` / `ToolStart` / `ToolEnd` / `TurnEnd` / `AgentEnd`)
and lets `EventCollector` build the `TurnRecord`, exactly like every other
harness.

Because Pi is model-agnostic, this is a cheap way to evaluate a broad set of
open-weight models (Kimi, DeepSeek, GLM, …) through a single agent. Pi honors
`system_prompt` (via `--append-system-prompt`), `allowed_tools`,
`disallowed_tools` and `permission_mode: plan` (via `--tools` /
`--exclude-tools`).

## Setup

### 1. Install the Pi CLI

Pi is a **Node** CLI, not a Python package:

```bash
npm install -g @earendil-works/pi-coding-agent
pi --version
```

The `coder-eval[pi]` extra exists for symmetry with the other harnesses and
carries **no Python dependencies** — the agent shells out to the binary above and
imports no third-party package:

```bash
uv sync --extra pi   # documents the opt-in; installs no extra packages
```

If the binary is missing, the task fails at `start()` with an actionable error
naming the install command, rather than failing obscurely mid-run.

### 2. Authentication

Pi addresses a model as `provider/id` and reads the provider key from the
environment, which Coder Eval forwards into the subprocess (it also picks up keys
from `.env`, since `config.py` calls `load_dotenv(override=True)`):

```bash
export OPENROUTER_API_KEY="sk-or-..."   # OpenRouter (any model)
```

## Usage

### Command line

```bash
uv run coder-eval run tasks/pi_smoke_test.yaml
uv run coder-eval run tasks/my_task.yaml -D agent.type=pi -D agent.model=openrouter/moonshotai/kimi-k3
```

### Task definition (YAML)

```yaml
agent:
  type: "pi"
  # provider-prefixed model id; no separate --provider needed.
  model: "openrouter/moonshotai/kimi-k3"
  permission_mode: "bypassPermissions"
  thinking_level: "medium"   # optional: reasoning effort (see below)
```

### Model selection

`agent.model` is passed through verbatim to `--model`, so it must be Pi's
provider-prefixed `provider/id` form. The provider prefix decides which
credential is used:

| `agent.model` | Provider | Credential |
|---|---|---|
| `openrouter/moonshotai/kimi-k3` | OpenRouter | `OPENROUTER_API_KEY` |

Pi speaks OpenRouter natively, so it does **not** need the LiteLLM proxy — that
shim exists to translate Anthropic ↔ OpenAI for the Claude Code SDK.

> On some OpenRouter accounts a model is blocked by an account **guardrail**
> (`404 "model blocked by guardrail"`). That is an account setting, not a Coder
> Eval problem — adjust it at
> [openrouter.ai/settings/privacy](https://openrouter.ai/settings/privacy), or
> pick another model. `openrouter/moonshotai/kimi-k3` runs cleanly on the dev
> account and is the smoke task's default.

### `thinking_level`

Pi's reasoning effort, forwarded as `--thinking`. Accepts the seven-value set
`off` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max` (a strict
superset of the Antigravity `thinking_level`), defaulting to `medium`.

### Config fields and their Pi flags

Per-field contract and tool names (generated): [Harness Parity § Agent-field contract](HARNESS_PARITY.md#agent-field-contract).

| Field | Pi flag |
|---|---|
| `system_prompt` | `--append-system-prompt <text>` (appended, semantics `append`) |
| `allowed_tools` | `--tools <csv>`, or `--no-tools` when no name has a Pi equivalent |
| `disallowed_tools` | `--exclude-tools <csv>` (subtracted from `--tools` when both are set) |
| `permission_mode: plan` | the Write, Edit and Bash equivalents added to the denied set |
| `thinking_level` | `--thinking <level>` |
| `model` | `--model <provider/id>` |
| `plugins` | `--skill <dir>` per resolved skills dir |

Claude tool names map to Pi's lowercase built-ins by inverting the telemetry map
(`Bash` → `bash`, `Edit` → `edit,multiedit,patch`, `Glob` → `find`, …). A canonical
name with no Pi equivalent restricts nothing; an allowlist of only such names disables
every tool. Pi's `ls` has no canonical name, so any allowlist denies it. An empty `allowed_tools: []` restricts nothing, as on
Claude Code.

## Permissions

`permission_mode: plan` is read-only: the Write, Edit and Bash equivalents are
denied. `permission_mode: bypassPermissions` runs every permitted tool without
approval. `default` and `acceptEdits` have no Pi meaning and are rejected at
resolution. Pi headless print mode auto-runs tools, and Coder Eval always passes
`--no-approve` so the run never blocks.

## Multi-turn and simulation

A standard task reaches its solution inside a **single** `communicate()` call —
Pi runs its own multi-step agent loop there (verified: a write+read task ran
three internal `turn_start` steps, all tools executed). A simulation / dialog
task calls `communicate()` once per user turn and relies on the agent remembering
prior turns, so `PiAgent` reuses a per-agent `--session-dir` + stable
`--session-id` on **every** invocation: the first call creates the session, later
calls resume it. The session tempdir lives outside the sandbox working dir and
the staged reference dir, so it never pollutes graded files or trips
reference-integrity, and it is removed in `stop()`. `pi_session_id` is recorded
per task under `environment_info`.

## Telemetry

Mapping from the CLI's event vocabulary onto `TurnRecord`:

| Pi event | Becomes |
|---|---|
| `turn_start` | `TurnStartEvent` (one inner turn; the unit `max_turns` counts) |
| `message_update` (`text_delta`) | `TextChunkEvent` + `agent_output` |
| `tool_execution_start` | `ToolStartEvent` |
| `tool_execution_end` | `ToolEndEvent` |
| `turn_end` | `TurnEndEvent` + per-turn tokens/cost, one `AssistantMessage` |
| `agent_settled` (or stdout EOF) | the single terminal `AgentEndEvent` |

Token buckets come from `turn_end.message.usage`, read **once per turn** and
**summed** across turns (per-generation, not cumulative). Pi's `input` is already
the fresh input slice, so it maps straight to `uncached_input_tokens`; `cacheRead`
and `cacheWrite` map to the cache buckets. `reasoning` tokens fold into
`output_tokens` (they bill at the output rate) while remaining visible as
`reasoning_tokens` per message. This keeps the reconciliation invariant exact:
summing the four buckets across `TurnRecord.messages` equals `token_usage`.

Real per-call cost rides on `turn_end.message.usage.cost.total` and lands on
`token_usage.total_cost_usd`, so runs are costed from the provider's own
accounting rather than the static rate card. No `pricing.py` entry is needed for
a Pi model; the rate card (`calculate_cost` over the captured buckets) is only a
fallback for a stream that omits cost entirely. A genuinely free model still
resolves to $0.

Tool names and argument keys are normalized to the canonical (Claude) vocabulary
on capture — `write` → `Write`, `read` → `Read`, `bash` → `Bash`, and a
`Read`/`Write`/`Edit` call's `path` argument → `file_path` — so one
`command_executed` criterion scores identically whether the run used Claude,
Codex, OpenCode or Pi. An unmapped tool keeps its own name.

### Auto-retry

Pi retries a transient/provider error **internally**: it emits another
`agent_start` / `turn_*` / `agent_end` cycle in the same invocation, marking the
first `agent_end` with `willRetry: true`. `PiAgent` treats `agent_end` as
non-terminal and finalizes exactly once at `agent_settled` (or stdout EOF), so a
retried invocation still produces a single `AgentEndEvent` with the cycles' usage
merged. The internal retry is bounded by `turn_timeout` / `task_timeout`.

### Drift is crashed, not scored

A turn whose CLI exits cleanly but which captured **no recognized events** (an
upgrade renamed the vocabulary) is failed rather than reported as a clean empty
success — the error names the unrecognized event types it saw. Intentional cuts
(`should_stop`, `max_turns`) are exempt.

> **Zero-usage turn.** A provider that reports no usage yields an all-zero
> `token_usage`. Pi does **not** hard-fail such a turn (its multi-provider surface
> makes a blanket fail brittle); the turn is scored and a warning is logged. If
> you need strict token accounting, prefer a provider whose stream reports usage.

## Running in Docker

Pi **is supported under `--driver docker`.** The pinned Pi CLI is baked into
`docker/Dockerfile` (`ARG PI_VERSION`, alongside the claude-code CLI), and
`OPENROUTER_API_KEY` is forwarded into the container by the docker driver's
default `env_passthrough` allowlist — so `--driver docker --type pi` runs
end-to-end with no per-task `env_passthrough_extra`:

```bash
export OPENROUTER_API_KEY="sk-or-..."
uv run coder-eval run tasks/pi_smoke_test.yaml --driver docker
```

**Docker is the recommended driver for untrusted / adversarial Pi runs.** Tool
restrictions limit which tools the model may call, not what a permitted `bash`
call can do, so the **container is the confinement boundary**. Under `tempdir` there is no such
boundary; the agent runs with the host's own permissions. Prefer `--driver
docker` whenever the task prompt or workspace is not fully trusted.

## Known limitations

- **`plugins` skills are injected via `--skill`.** Each `type: local` plugin root
  is resolved to its skills dir (`<root>/skills`, holding `<name>/SKILL.md`) and
  passed to the CLI as a `--skill <dir>` argument — the same `_plugin_skill_dirs`
  resolver OpenCode uses — and recorded as `pi_skill_paths` in `environment_info`.
  Pi therefore **can run activation suites**: `skill_triggered` detects Pi's
  engagement agent-agnostically (the agent `read`s the full `SKILL.md`, a
  `read`→`Read` call whose `path` matches `skills/<name>/`). Use the **plugin-root
  shape** (`<path>/skills/<name>/SKILL.md`) for activation suites: a bare skills dir
  still *loads* (the resolver's fallback passes it as `--skill <dir>`), but the read
  path then lacks the `skills/<name>/` segment `skill_triggered` matches on, so the
  suite scores recall 0 even though the skill ran — see
  [Harness Parity § plugin-path depth](HARNESS_PARITY.md). A plugin's non-skill
  assets (agents/hooks/commands/MCP) are not wired.
- **`system_prompt_file` is not read by the adapter.** Use `system_prompt` (inline)
  instead — it is enforced via `--append-system-prompt`.
- **`max_turns` counts Pi's native agent-loop turns.** One `turn_start` = one
  agent-loop step; `max_turns: N` allows N complete turns, then the run finalizes
  cleanly as `max_turns_exhausted`. See
  [Run-Limit Parity](HARNESS_PARITY.md) before holding `max_turns` constant across
  harnesses.
- **No sub-agent attribution.** Pi's CLI stream does not expose nested agent
  generations, so per-sub-agent token grouping (available for Claude and Codex) is
  not derivable.
- **Cooperative stop is at event granularity.** `should_stop` is polled between
  events and honored by terminating the CLI, so `stop_early` works, but the cut
  lands on an event boundary rather than mid-tool. Pi streams incrementally, so
  this genuinely cuts spend mid-run.

## References

- [Pi](https://pi.dev/)
- [Extending Coder Eval](../EXTENDING.md) — the agent plugin SPI
- [Task Definition Guide](../TASK_DEFINITION_GUIDE.md)
