---
description: >-
  Run OpenCode, the open-source terminal coding agent, as the agent under
  evaluation in Coder Eval — installation, provider authentication, model
  selection, and how its event stream maps to sandboxed, weighted scoring.
---

# Running OpenCode in Coder Eval

## Overview

[OpenCode](https://opencode.ai) is an open-source terminal coding agent. Coder Eval
drives it **non-interactively**:

```bash
opencode run --format json -m <provider/model> --dir <sandbox> --auto --pure "<prompt>"
```

`--format json` streams **newline-delimited JSON events** on stdout, one event per
line. `OpenCodeAgent` reduces that stream into the standardized event protocol
(`AgentStart` / `TurnStart` / `ToolStart` / `ToolEnd` / `TurnEnd` / `AgentEnd`) and
lets `EventCollector` build the `TurnRecord`, exactly like every other harness.

Because OpenCode is model-agnostic, this is the cheapest way to evaluate a broad
set of open-weight models (DeepSeek, Kimi, GLM, …) through a single agent.

## Setup

### 1. Install the OpenCode CLI

OpenCode is a **Node** CLI, not a Python package:

```bash
npm install -g opencode-ai
opencode --version
```

The `coder-eval[opencode]` extra exists for symmetry with the other harnesses and
carries **no Python dependencies** — the agent shells out to the binary above and
imports no third-party package:

```bash
uv sync --extra opencode   # documents the opt-in; installs no extra packages
```

> The `opencode-ai` package on **PyPI** is an HTTP client for `opencode serve`, a
> different integration surface than the CLI this harness drives. You do not need
> it.

If the binary is missing, the task fails at `start()` with an actionable error
naming the install command, rather than failing obscurely mid-run.

### 2. Authentication

OpenCode resolves credentials itself, per provider. Either log in once:

```bash
opencode auth login
```

…or export the provider key, which Coder Eval forwards into the subprocess
environment (it also picks up keys from `.env`, since `config.py` calls
`load_dotenv(override=True)`):

```bash
export OPENROUTER_API_KEY="sk-or-..."   # OpenRouter (any model)
export DEEPSEEK_API_KEY="sk-..."        # DeepSeek first-party
```

Check what a given model id resolves to with:

```bash
opencode models | grep <model>
```

## Usage

### Command line

```bash
uv run coder-eval run tasks/opencode_smoke_test.yaml
uv run coder-eval run tasks/my_task.yaml -D agent.type=opencode -D agent.model=openrouter/deepseek/deepseek-v4-pro
```

### Task definition (YAML)

```yaml
agent:
  type: "opencode"
  # provider/model, exactly as `opencode models` prints it.
  model: "openrouter/deepseek/deepseek-v4-pro"
  permission_mode: "acceptEdits"
  variant: "high"   # optional: provider reasoning effort
  pure: true        # optional (default): run with --pure, no host plugins
```

### Model selection

`agent.model` is passed through verbatim to `-m`, so it must be OpenCode's
`provider/model` form. The provider prefix decides which credential is used:

| `agent.model` | Provider | Credential |
|---|---|---|
| `openrouter/deepseek/deepseek-v4-pro` | OpenRouter | `OPENROUTER_API_KEY` |
| `deepseek/deepseek-v4-pro` | DeepSeek direct | `DEEPSEEK_API_KEY` |

OpenCode speaks OpenRouter natively, so it does **not** need the LiteLLM proxy —
that shim exists to translate Anthropic ↔ OpenAI for the Claude Code SDK.

### `variant`

Provider-specific reasoning effort (`minimal` / `high` / `max`, provider
dependent), forwarded as `--variant`. Omit to take the provider default.

### `pure`

Defaults to `true`, forwarding `--pure` so the sandbox is isolated from host-level
OpenCode plugin configuration. This mirrors the rationale behind the Claude agent's
`setting_sources: []`. Set to `false` to load host plugins deliberately.

## Permissions

Every `permission_mode` except `plan` passes `--auto`, auto-approving tool use.
This is required for unattended evaluation — without it OpenCode blocks on an
interactive approval prompt and the turn runs to its timeout. Use
`permission_mode: plan` when you explicitly want approvals withheld.

## Telemetry

Mapping from the CLI's event vocabulary onto `TurnRecord`:

| OpenCode event | Becomes |
|---|---|
| `step_start` | `TurnStartEvent` (one inner turn) |
| `text` | `TextChunkEvent` + `agent_output` |
| `tool_use` | `ToolStartEvent` + `ToolEndEvent` (one terminal event carries both) |
| `step_finish` | `TurnEndEvent` + per-step tokens/cost, one `AssistantMessage` |
| `error` | `AgentCrashError` with the partial turn preserved |

Token buckets come from `step_finish.tokens`. Two conventions for `tokens.input`
exist in the wild, and the stream's own `total` arbitrates **per step**:

- **flat** (what the current CLI emits, verified live —
  `7966 = 6796 + 128 + 18 + 1024` exactly): `input` already *is* the fresh slice,
  and `total = input + output + reasoning + cache.read + cache.write`;
- **nested** (the OpenAI `prompt_tokens` convention): cached tokens are counted
  inside `input`, `total = input + output + reasoning`, so the fresh slice is
  `input - cache.read - cache.write`.

With no cache traffic the two agree. With no usable `total` the flat reading is
taken — logged as a warning when cache traffic is present, since the convention
then cannot be verified. A `total` matching **neither** also warns (once per
turn) that the bucket mapping may no longer match the CLI — so a drifting schema
is visible in `task.log` instead of quietly mis-costing every run. Reasoning
tokens are folded into `output_tokens` (they bill at the output rate) while
remaining visible as `reasoning_tokens` per message. This keeps the
reconciliation invariant exact: summing the four buckets across
`TurnRecord.messages` equals `token_usage`.

Real per-call cost rides on `step_finish.cost` and lands on
`token_usage.total_cost_usd`, so runs are costed from the provider's own
accounting rather than the static rate card. The rate card
(`calculate_cost` over the captured buckets) fills two gaps so the run total
never books tokens with no money: a stream that reports **no** cost at all (a
provider or auth mode that omits it, or a turn that died before its first
`step_finish`), and a stream that reports **`cost: 0`** for tokens the rate
card prices above zero — OpenCode reports 0 when its own model registry has no
price for the model, or under subscription-style auth, and neither means the
tokens were free (the fallback logs a warning naming the substituted amount). A
*non-zero* cost the CLI reported always wins, and a genuinely free model still
resolves to $0 because its rate entry is absent or all-zero.

Tool names are normalized to the canonical (Claude) vocabulary on capture —
`bash` → `Bash`, `read` → `Read`, `write`/`edit`/`patch` → `Write`/`Edit`, and so
on; an unmapped tool keeps its own name. This is what lets one
`command_executed` criterion (which filters on `tool_name` and reads a `Bash`
call's `command` parameter) score identically whether the run used Claude, Codex
or OpenCode.

> These event names are the CLI's own compact vocabulary. They are **not** the
> `session.next.*` names in the OpenAPI schema served by `opencode serve` — that
> describes the HTTP/SSE surface and does not apply here.

Vocabulary drift is crashed, not scored: a turn whose CLI exits cleanly but whose
stream contained **no recognized events** captured zero telemetry (zero turns,
tokens and cost) while file-based criteria could still pass on whatever the agent
did — a success that silently vanishes from every aggregate. The turn is failed
with an error naming the unrecognized event types it saw instead.

## Known limitations

- **`allowed_tools` / `disallowed_tools` / `system_prompt` / `system_prompt_file` /
  `plugins` are not enforced.** The CLI exposes no equivalent knob, so these are
  dropped — `start()` logs a warning naming each one it saw (`experiments/default.yaml`
  sets `allowed_tools` on every task, so expect it on a default run). Do not rely on
  them as a boundary here, and note that skill-injection suites, which depend on
  `plugins`, cannot run on this harness.
- **`max_turns` counts OpenCode's native steps.** One step = one assistant
  generation (`step_start`/`step_finish`) and may carry several tool calls;
  `max_turns: N` allows N complete steps, then the run finalizes cleanly as
  `max_turns_exhausted`. This is the claude-code-style native unit, not the
  visible-turn unit Codex/Antigravity use — see
  [Run-Limit Parity](HARNESS_PARITY.md) before holding `max_turns` constant
  across harnesses.
- **No sub-agent attribution.** OpenCode's CLI stream does not expose nested agent
  generations, so per-sub-agent token grouping (available for Claude and Codex) is
  not derivable.
- **Cooperative stop is at event granularity.** `should_stop` is polled between
  events and honored by terminating the CLI, so `stop_early` works, but the cut
  lands on an event boundary rather than mid-tool.
- **Pipe and process teardown.** `opencode run` leaves a local server child
  holding the inherited stdout/stderr pipes, so EOF never arrives on its own. The
  agent races each read against process exit, bounds the post-exit drain, and
  bounds the final reap by the turn deadline (a CLI that closes its stream but
  never exits is cut as a timeout/crash, not waited out). stderr gets its own
  concurrent reader from the moment the CLI starts — draining it only afterwards
  would let a full stderr pipe block the child mid-write and stall stdout with it.
  Each invocation runs in its own process group (`start_new_session`), and
  `kill()` / `kill_sync()` / `stop()` sweep that group with SIGKILL, so the server
  child is reaped rather than leaked across a batch; OpenCode persists sessions on
  disk, so `--session` continuity survives the sweep.

## Troubleshooting

**`No endpoints available matching your guardrail restrictions and data policy`**
— an OpenRouter *account* setting, not a Coder Eval problem. The model's serving
providers are all excluded by your account's privacy/guardrail configuration.
Verify independently with a direct API call, then adjust at
[openrouter.ai/settings/privacy](https://openrouter.ai/settings/privacy).

**Task fails with `emitted no recognized events`** — the CLI exited cleanly but
nothing on its stdout matched the event vocabulary above, so the turn would have
scored with zero telemetry; the harness fails it instead. The error names the
event types it did see. Check the raw stream with
`opencode run --format json ... > raw.jsonl` and compare the `type` values
against the table above — an OpenCode upgrade that renames them needs a matching
harness update.

## References

- [OpenCode docs](https://opencode.ai/docs/) · [CLI reference](https://opencode.ai/docs/cli/)
- [Extending Coder Eval](../EXTENDING.md) — the agent plugin SPI
- [Task Definition Guide](../TASK_DEFINITION_GUIDE.md)
