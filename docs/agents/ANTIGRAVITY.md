---
description: >-
  Run Google Antigravity (Gemini) as the agent under evaluation in Coder Eval —
  installation, authentication, model and skill configuration, and how its
  telemetry maps to sandboxed, weighted scoring.
---

# Running Google Antigravity (Gemini) in Coder Eval

## Overview

Coder Eval can run Google's **Antigravity** agent — powered by **Gemini** — as the
agent under evaluation. Set `agent.type: antigravity` in a task and the rest of the
framework (sandbox, scoring, telemetry, reports) works unchanged.

Under the hood `AntigravityAgent` drives the Antigravity SDK's **local harness**
(the `localharness` binary bundled in the wheel) rather than the branded `agy` CLI
or the remote Interactions API. The local harness is the only surface that can (a)
authenticate headlessly with an API key and (b) make edits land in the sandbox
working directory — both required for an unattended eval.

## Setup

### 1. Install the Antigravity SDK

```bash
pip install 'coder-eval[antigravity]'
```

This pulls in `google-antigravity` (pinned to `0.1.8`), whose wheel bundles the
platform `localharness` binary. As with the other agents the SDK is imported lazily
— a base install without the extra still runs end-to-end; Antigravity tasks fail at
dispatch with a clear hint to install the extra.

### 2. Authentication

Antigravity authenticates against the **Gemini Developer API** with a single
credential:

```bash
GEMINI_API_KEY=<your-gemini-api-key>
```

Set it in `.env` (or the environment). That is the *only* credential Antigravity
reads — there is **no base-URL, project, or region override** for this agent
(unlike Codex's `CODEX_BASE_URL` / Azure routing). If `GEMINI_API_KEY` is unset the
SDK raises a clear error on first use.

> The `--backend` flag (`direct` / `bedrock`) governs *Claude* routing only; it has
> no effect on the Antigravity agent.

## Usage

### Command line

```bash
coder-eval run tasks/agents/antigravity_hello_world.yaml --type antigravity
```

Or override the agent type for every task in an experiment:

```bash
coder-eval run experiments/model-comparison.yaml --type antigravity
```

### Task definition (YAML)

```yaml
agent:
  type: antigravity
  model: gemini-3.1-pro-preview   # optional; see "Model selection" below
  thinking_level: medium          # minimal | low | medium | high (default: medium)
  plugins:
    - type: local
      path: "$SKILLS_PLUGIN_PATH"  # a directory of skills (SKILL.md), env-expanded

run_limits:
  max_turns: 5
  task_timeout: 360
  turn_timeout: 300

success_criteria:
  - type: file_exists
    path: "hello.py"
    description: "hello.py must be created"
```

Two runnable examples ship in the repo:

- `tasks/agents/antigravity_hello_world.yaml` — `tempdir` driver
- `tasks/agents/antigravity_hello_world_docker.yaml` — `docker` driver

### Model selection

The resolved model is the first of:

1. `agent.model` in the task YAML
2. `ANTIGRAVITY_MODEL` in the environment
3. the built-in default, **`gemini-3.5-flash`**

> **Note:** the shipped example tasks pin `gemini-3.1-pro-preview`. Pin `agent.model`
> explicitly in any task you care about reproducing — don't rely on the built-in
> default, which tracks the current recommended coding model and may change.

### `thinking_level`

Antigravity exposes a `thinking_level` field (`minimal` / `low` / `medium` /
`high`, default `medium`) that maps to Gemini's thinking budget. This is
Antigravity-specific — Claude Code and Codex don't take this field. Thinking tokens
are billed as **output** tokens (see [Telemetry](#telemetry)).

### `system_prompt`

`agent.system_prompt` is passed to the SDK as `system_instructions`, whose string
shorthand maps to `TemplatedSystemInstructions` — a named section **appended** to
the harness's default system instructions, never a replacement. This matches the
append-only semantics of the shared config field across agents (Claude Code appends
via the `claude_code` preset; Codex via `developer_instructions`).

### Skills (SKILL.md)

Antigravity supports [Agent Skills](https://agentskills.io/specification)
(`SKILL.md`) natively. Skill directories are discovered from:

1. `agent.plugins` entries with `type: local` and a `path` (env vars in the path are
   expanded at runtime), and
2. the runtime plugin directory passed to `start()`.

For each source, the harness is handed whichever of `<source>/skills` or `<source>`
directly contains subdirectories with a `SKILL.md`. Unlike Codex — which symlinks
skills into `.agents/skills/` — Antigravity is given the search-path roots directly
via the SDK's `skills_paths`, and those roots are also added to the harness
`workspaces` allowlist (otherwise the agent would find a skill but be denied the
`SKILL.md` read as out-of-workspace). The agent logs a loud warning if a skills path
can't be resolved or if zero skills are discovered.

## Permissions & tools — important differences

Per-field contract and tool names (generated): [Harness Parity § Agent-field contract](HARNESS_PARITY.md#agent-field-contract).

The uniform fields become SDK tool-call policies (`google.antigravity.hooks.policy`):

| Field | Policies |
|---|---|
| none set | `allow_all()` — every tool call, including `run_command`, is approved |
| `allowed_tools` | `deny_all()`, then `allow(tool)` for each mapped tool and for `finish` |
| `disallowed_tools` | `deny(tool)` for each mapped tool |
| `permission_mode: plan` | `deny` on `create_file`, `edit_file` and `run_command` (read-only) |
| `permission_mode: bypassPermissions` | no extra rule |

`default` and `acceptEdits` have no Antigravity meaning and are rejected at resolution.

Claude tool names map to harness tools by inverting the telemetry map (`Bash` →
`run_command`, `Write` → `create_file`, `Edit` → `edit_file`, `Read` → `view_file`,
…). A specific rule outranks the wildcard one in the SDK, and a specific deny
outranks a specific allow, so a denied tool stays denied. `finish` is always allowed
under an allowlist and never denied, because the harness ends a turn with it. An empty
`allowed_tools: []` restricts nothing, as on Claude Code. A name with no
Antigravity equivalent restricts nothing. File tools also stay restricted to the
configured `workspaces` (the sandbox working directory plus any skill roots); an
out-of-workspace path is a specific deny.

Policies decide which calls run; a denied tool is still visible to the model. They do
not confine what a permitted `run_command` can do, so the trust boundary for an
untrusted run is still the **sandbox**: use the [Docker driver](../DOCKER_ISOLATION.md).

## Telemetry

Each `communicate()` call is one logical turn; the standard `TurnRecord` is built by
the shared `EventCollector`, so Antigravity runs report the same per-turn structure
as every other agent.

- **Tool-name normalization.** Antigravity's built-in tool names are mapped to the
  canonical Claude-style names so cross-agent criteria work: `run_command` → `Bash`,
  `create_file` → `Write`, `edit_file` → `Edit`, `view_file` → `Read`,
  `search_directory` → `Grep`, `find_file` → `Glob`, `list_directory` → `LS`,
  `start_subagent` → `Agent`, `search_web` → `WebSearch`. Argument keys are also
  normalized (e.g. `command_line` → `command`) so `command_executed` criteria key on
  the same params across agents.
- **Tokens.** Gemini usage maps to Coder Eval's four buckets: uncached input, cache
  read (`cached_content_token_count`), output, and **`cache_creation` is always 0**
  (Gemini bills no separate cache-write). Thinking tokens are folded into **output**.
  Per-generation usage is cut into `AssistantMessage`s that sum exactly to the turn
  total, so there is typically no reconciliation residual.
- **Commands.** Each tool call is captured as `CommandTelemetry` with status, an
  untruncated `result_summary`, error message, and duration. Harness-appended result
  fields (`exit_code`, `combined_output`, `diff_block`, `stdout`/`stderr`, …) are
  stripped from the recorded `parameters` so tool *output* never leaks into the
  recorded call — which also keeps it from false-positiving a `skill_triggered`
  substring match.

## Known limitations

1. **No endpoint routing.** Only `GEMINI_API_KEY` + `ANTIGRAVITY_MODEL` are read —
   there is no base-URL, project, region, or gateway override.
2. **Default-model drift.** The runtime fallback (`gemini-3.5-flash`) may differ from
   what a given release's docs or example tasks pin; always set `agent.model`
   explicitly for reproducible runs.
3. **`kill_sync()` is best-effort.** The SDK's cancel/disconnect are async-only, so
   the watchdog's synchronous kill only flips agent state to `ERROR`; real teardown
   happens on the subsequent async `stop()`.
4. **Denied tools stay visible.** A policy denial rejects the call after the model
   makes it, so a denied tool can still cost tokens on a retry.
5. **`max_turns` counts visible turns.** One `communicate()` is a single SDK turn here,
   so the cap counts resolved tool calls instead, enforced on the step loop. See
   [Run-Limit Parity](HARNESS_PARITY.md).
6. **Shell commands over ~10s are moved to the background.** The localharness has a
   10-second maximum synchronous wait; past it the command becomes a background task
   and the model gets a task id, not a result. The turn polls for that result instead
   of finalizing on an idle step stream, so slow work does complete — but the wait is
   bounded by 80% of `turn_timeout`, and a job that outlives it is force-closed as
   `result_status: unknown` and graded as an ordinary low score rather than a timeout.
   Measured in [Run-Limit Parity](HARNESS_PARITY.md).

## Running in Docker

The `docker` driver works the same as for other agents (see
[Docker Isolation](../DOCKER_ISOLATION.md)). The `localharness` binary ships inside
the image via the `[antigravity]` extra, and `GEMINI_API_KEY` + `ANTIGRAVITY_MODEL`
are on the container env passthrough allowlist, so they are forwarded automatically.

```bash
coder-eval run tasks/agents/antigravity_hello_world_docker.yaml
```

## References

- [Agent Skills specification](https://agentskills.io/specification)
- [Codex Agent Guide](CODEX.md) — the sibling third-party-agent guide
- [Claude Code Agent](CLAUDE_CODE.md) — the default agent
- [Extending Coder Eval](../EXTENDING.md) — how agents register via the plugin SPI
