---
description: >-
  Run the OpenHands Software Agent SDK as the agent under evaluation in Coder
  Eval — installation, the model-prefix provider routing, the optional LiteLLM
  proxy, and how its telemetry maps to sandboxed, weighted scoring.
---

# Running OpenHands in Coder Eval

## Overview

Coder Eval can run the **OpenHands Software Agent SDK** as the agent under
evaluation. Set `agent.type: openhands` in a task and the rest of the framework
(sandbox, scoring, telemetry, reports) works unchanged.

OpenHands is a **model-agnostic** harness: it drives any model reachable through
its bundled LiteLLM, resolving the provider from the `agent.model` **prefix**
(`anthropic/…`, `openai/…`, `openrouter/…`, `bedrock/…`, `litellm_proxy/…`) with
no per-provider branch in Coder Eval. This makes it the "universal harness" for
**isolate-the-model** comparisons — e.g. Claude-Code-on-Sonnet vs
OpenHands-on-Sonnet, or Codex-on-GPT vs OpenHands-on-GPT — where the harness (not
the model) is the variable. It is the recommended way to evaluate OSS/open-weight
models (GLM / DeepSeek / Kimi via OpenRouter) natively, with no
Anthropic-impersonation proxy on that path.

Under the hood `OpenHandsAgent` builds a fresh OpenHands `Conversation` per turn
(a `LocalWorkspace` pointed at the sandbox working directory) with the `terminal`
and `file_editor` tools, and drives the SDK's synchronous `run()` loop to
completion, mapping its event stream onto Coder Eval's standard telemetry.

## Setup

### 1. Install the OpenHands SDK

```bash
pip install 'coder-eval[openhands]'
```

This pulls in `openhands-sdk` and `openhands-tools` (both pinned to `1.40.0`),
which bundle LiteLLM. As with the other agents the SDK is imported lazily — a base
install without the extra still runs end-to-end; OpenHands tasks fail at `start()`
with a clear hint to install the extra.

### 2. Authentication — the provider key follows the model prefix

OpenHands' internal LiteLLM resolves the provider from the `agent.model` prefix and
reads the matching key from the environment automatically — there is **no single
`OPENHANDS_API_KEY`**:

| `agent.model` prefix          | Env var read           |
| ----------------------------- | ---------------------- |
| `anthropic/claude-sonnet-4-6` | `ANTHROPIC_API_KEY`    |
| `openai/gpt-5.4`              | `OPENAI_API_KEY`       |
| `openrouter/z-ai/glm-5.2`     | `OPENROUTER_API_KEY`   |
| `bedrock/…`                   | `AWS_*`                |
| `litellm_proxy/<alias>`       | `OPENHANDS_BASE_URL` (proxy) |

Set the one that matches your grid in `.env` (or the environment). All of these,
plus `OPENHANDS_BASE_URL` and `OPENHANDS_MODEL`, are on the container env-passthrough
allowlist, so they are forwarded automatically under the Docker driver.

### Direct vs proxy — a per-experiment env choice, not a code branch

- **Direct (default).** Leave `OPENHANDS_BASE_URL` unset. Each provider is called
  natively; cost is OpenHands' native LiteLLM **estimate**; no sidecar.
- **Proxy (opt-in).** Set `OPENHANDS_BASE_URL` to a LiteLLM proxy and use a
  `litellm_proxy/<alias>` model. Use this for a single egress point (governance /
  EU-residency / allowlist / gateway caching), or for uniform actual-cost across
  harnesses. Coder Eval's `x-ce-*` cost-correlation headers are forwarded to the
  proxy on this path only (the direct path uses the native estimate).

> **Operational caveat (OpenRouter data policy).** Some OpenRouter accounts enforce
> a global data-policy/privacy guardrail that **404s _direct_ calls** ("No endpoints
> available matching your guardrail restrictions and data policy"). If you hit this,
> either relax the account privacy policy at `openrouter.ai/settings/privacy`, or
> route through the **LiteLLM proxy** (`OPENHANDS_BASE_URL` + a `litellm_proxy/…`
> model), which is unaffected. The proxy path is the reliable default until the
> account policy is relaxed.

## Usage

### Command line

```bash
coder-eval run tasks/agents/openhands_hello_world.yaml --type openhands
```

Or override the agent type for every task in an experiment:

```bash
coder-eval run experiments/harness-comparison.yaml --type openhands
```

### Task definition (YAML)

```yaml
agent:
  type: openhands
  model: openrouter/z-ai/glm-5.2   # carries the LiteLLM provider prefix
  permission_mode: bypassPermissions

run_limits:
  max_turns: 20
  task_timeout: 600
  turn_timeout: 300

success_criteria:
  - type: file_exists
    path: "hello.py"
    description: "hello.py must be created"
```

### Model selection

The resolved model is the first of:

1. `agent.model` in the task YAML (or `--model`)
2. `OPENHANDS_MODEL` in the environment

There is **no built-in default** — OpenHands is multi-provider, so a run with no
model configured fails fast at `communicate()` with a clear error. Always pin
`agent.model` (with its provider prefix) for reproducible runs.

## Permissions & tools — important differences

**OpenHands runs its tools directly on the host** (`LocalWorkspace`), and does
**not** honor `permission_mode` / `allowed_tools` / `disallowed_tools` as a
security boundary. The trust boundary for an OpenHands run is therefore the
**sandbox**, not the agent config: run untrusted tasks under the
[Docker driver](../DOCKER_ISOLATION.md); the `tempdir` driver is a working
directory, not a confinement boundary. This mirrors how Claude Code / Codex /
Antigravity already run there. The agent is given exactly two tools — a shell
(`terminal`) and file editing (`file_editor`).

## Telemetry

Each `communicate()` call is one logical turn; the standard `TurnRecord` is built by
the shared `EventCollector`, so OpenHands runs report the same per-turn structure as
every other agent.

- **Commands.** Each `ActionEvent` is captured as `CommandTelemetry` (with the
  action's inputs as `parameters`) and resolved on its matching `ObservationEvent`
  (`action_id` ↔ `tool_call_id`); a tool failure arrives as a separate
  `AgentErrorEvent` and closes the call as an error. Orphaned tool calls (start with
  no result) are force-closed as `unresolved` at turn end.
- **Tokens.** OpenHands' accumulated `Metrics` map to Coder Eval's four buckets.
  Because OpenHands' `prompt_tokens` **already includes** the cache buckets, the
  fresh/uncached slice is `prompt_tokens − cache_read − cache_write`; `cache_write`
  maps to `cache_creation`, `cache_read` to `cache_read`, and `completion_tokens`
  (which already includes reasoning, billed as output by LiteLLM) to `output`. The
  turn cost is OpenHands' native LiteLLM **estimate** (`accumulated_cost`), kept even
  for a model our rate card can't price. OpenHands surfaces usage only as a post-run
  aggregate (no per-generation token stream in events), so the whole turn total is
  booked as the `EventCollector`'s single reconciliation entry — the transcript token
  buckets still sum exactly to the turn total.

## Known limitations

1. **`skill_triggered` is not implemented for OpenHands.** OpenHands has no
   Claude-style Skill tool, so a task that gates on `skill_triggered` scores it as
   not-triggered for this agent. Exclude / treat it as N/A for cross-harness scoring
   (a filesystem-read detector is a follow-up).
2. **Watchdog-only timeout.** There is no native turn timeout; the deadline is
   enforced by Coder Eval's `ThreadedWatchdog`, which calls `conversation.pause()`.
   A cooperative pause may not stop a non-yielding native call promptly;
   `kill_sync()` best-effort `close()` is the backstop.
3. **Native cost estimate.** The direct path records OpenHands' LiteLLM cost
   *estimate*, not a proxy-measured actual cost. For uniform actual-cost across
   harnesses, use the proxy path.
4. **No sandbox isolation of its own.** Tools run on the host; rely on the Docker
   driver for untrusted tasks (see above).

## Benchmark validity (harness-neutral criteria)

A harness leaderboard (`agent.type × agent.model`, expressible today as experiment
variants — see `experiments/harness-comparison.yaml`) is only valid if criteria
grade **outcomes**, not harness-internal signals. `skill_triggered` inspects
Claude's Skill tool, so OpenHands scores 0 on it *by construction* — that measures
"Claude-shapedness", not task quality. Auditing the UiPath criteria for
harness-neutrality is a prerequisite for a fair cross-harness leaderboard and is a
separate workstream.

## Running in Docker

The `docker` driver works the same as for other agents (see
[Docker Isolation](../DOCKER_ISOLATION.md)). Install the `[openhands]` extra into
the image, and the provider keys (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` /
`OPENROUTER_API_KEY`), `OPENHANDS_BASE_URL`, and `OPENHANDS_MODEL` are on the
container env passthrough allowlist, so they are forwarded automatically.

## References

- [A/B Experiments](../AB_EXPERIMENTS.md) — how the harness × model grid is expressed
- [Codex Agent Guide](CODEX.md) — the sibling third-party-agent guide
- [Antigravity Agent Guide](ANTIGRAVITY.md) — the sibling local-harness guide
- [Extending Coder Eval](../EXTENDING.md) — how agents register via the plugin SPI
