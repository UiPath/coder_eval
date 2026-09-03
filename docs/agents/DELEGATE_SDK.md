---
description: >-
  Run UiPath Autopilot's Delegate agent as the agent under evaluation in Coder
  Eval — installing the @uipath/delegate-stdio host from npm, UiPath
  authentication and token refresh, model selection, and how the host's stdio
  event stream maps to sandboxed, weighted scoring.
---

# Running the UiPath Delegate SDK agent in Coder Eval

## Overview

The `delegate-sdk` agent evaluates **UiPath Autopilot's Delegate agent**. Coder Eval
is Python and the Delegate SDK is TypeScript, so `DelegateSdkAgent` drives it through
the [`@uipath/delegate-stdio`](https://www.npmjs.com/package/@uipath/delegate-stdio)
npm package — the Delegate agent exposed as a Node subprocess speaking
newline-delimited JSON over stdin/stdout (called "the host" below). The package is
**self-contained**: `@uipath/delegate-sdk` and its platform runtime are its npm
dependencies and install transitively, so one `npm install` is the complete install.
You install that package, point Coder Eval at it, supply UiPath auth, and run tasks as
usual.

One thing to keep in mind: the Delegate agent's *reasoning* runs in the UiPath
backend, but its **tools execute locally** — the host `chdir`s into the task's sandbox
working directory at init, so file writes and commands land in the sandbox with no
"reconcile back" step. Ordinary file-based criteria (`file_exists`, `run_command`,
`pytest`, …) therefore work unchanged; `tasks/delegate_sdk_smoke_test.yaml` shows
this end to end.

## Setup

### 1. Install Node and the `@uipath/delegate-stdio` host

The host is a **Node** package on the public npm registry, not a Python package:

```bash
npm install @uipath/delegate-stdio
```

This drops the bundle at `node_modules/@uipath/delegate-stdio/dist/delegate_stdio.mjs`
and pulls in `@uipath/delegate-sdk` plus the `@uipath/delegate-runtime-*` interop
binaries for your platform (Linux x64, Windows x64, macOS arm64). There is **no**
separate SDK to set up.

The `coder-eval[delegate-sdk]` extra exists for symmetry with the other harnesses and
carries **no Python dependencies** — the agent shells out to the bundle above:

```bash
uv sync --extra delegate-sdk   # documents the opt-in; installs no extra packages
```

If the bundle cannot be found, the task fails at `start()` with a non-retryable
`AgentConfigError` listing every path it probed and the install command, rather than
failing obscurely mid-run.

> **npm's walk-up gotcha.** `npm install <pkg>` run in a directory with no
> `package.json` silently installs into the nearest *ancestor* that has one — often
> `~/node_modules`. Coder Eval's resolver walks the cwd's ancestors and `~` for exactly
> this reason, so the install is still found; `npm ls @uipath/delegate-stdio` shows
> where it landed.

### 2. Point Coder Eval at the host

**Usually nothing to set.** When neither variable below is set, the bundle is
auto-located by walking up from the current directory through its ancestors and `~`,
the way Node resolves modules. To override the search, set **one** of these:

| Variable | Meaning |
|---|---|
| `DELEGATE_STDIO_NODE_MODULES` | The install root that holds `node_modules/@uipath/...` (probed exactly, no walk-up). |
| `DELEGATE_STDIO_PATH` | Absolute path straight to `dist/delegate_stdio.mjs`. The reliable seam for CI and container images, where the bundle lives outside any cwd ancestor. |

### 3. Choose the cloud environment

| Variable | Default | Purpose |
|---|---|---|
| `DELEGATE_SDK_ENV` | `alpha` | Cloud env slug (`alpha` / `staging` / `production`). The host composes the backend URL from your auth's org/tenant slugs plus this value, so there are normally no backend or interop URLs to configure. |
| `BACKEND_URL` / `INTEROP_URL` | unset | Advanced/local override only: pin the Delegate backend / interop endpoints directly (e.g. a localhost backend). Host precedence: `backendUrl` > `BACKEND_URL` > env slug. With `INTEROP_URL` set the SDK runs connect-only against an externally managed interop instead of spawning its own. |

### 4. Authenticate

Provide UiPath credentials one of two ways. All of them are consumed by the **host
process**, never read by Coder Eval itself:

- **Environment token** — `AUTH_TOKEN`, `TENANT_ID`, `ORG_ID` (plus `USER_ID` for
  service-to-service tokens, and `ORG_LOGICAL_NAME` / `TENANT_NAME` when the env slug
  has to compose the backend URL from the org/tenant *slugs*).
- **Saved login** — a prior `npx @uipath/delegate-cli login --env <env>` (interactive
  browser OAuth) that wrote `~/.aria/sdk-auth.json`. Saved tokens are short-lived
  (about an hour) but refresh while their refresh token is valid.

Your org/tenant must have autopilot-everywhere provisioned on the chosen environment.

An auth failure at startup is **non-retryable**: the agent fails fast with an
`AgentConfigError` (instead of burning the API-error retry budget) and says whether the
saved login is *expired* (and how long ago) or *absent*, plus the exact login command.

#### Staying authenticated past the token TTL

An `AUTH_TOKEN` exported into the environment is frozen at that value, so a run longer
than its TTL starts failing with `401 Invalid token: Signature has expired`. Two
mechanisms cover that, both by keeping a **token file** fresh — the host re-reads it at
init, on every turn, and on its own refresh timer, and applies a newer token live:

- **External refresher** — point `DELEGATE_AUTH_TOKEN_FILE` (or `AUTH_TOKEN_FILE`) at
  a file some other process keeps current. It accepts a `PATH`-style list and takes the
  first readable entry, so one value can name both a host path and the same file's
  bind-mount path inside a container. This always wins.
- **Adapter-side S2S refresher** — when no token file is configured *and* the
  `AUTH_TOKEN` was itself minted from the `LLMGW_CLIENT_ID` / `LLMGW_CLIENT_SECRET` /
  `LLMGW_URL` client-credentials triple present in Coder Eval's environment, the agent
  re-mints that token before each expiry and publishes it through a token file of its
  own. The `LLMGW_*` secret is **stripped from the host environment** (the agent's own
  shell tools inherit that environment, i.e. the code under test), so only the token
  file — not the secret — is reachable from inside the sandbox. It activates only when
  the inherited token's `client_id` claim matches, so it never forces a re-minted
  service token onto a run that authenticated another way. Look for
  `S2S token-file refresher active` (or the `client_id mismatch` / `already configured`
  decline lines) in `task.log` to see which path a run took.

## Usage

### Command line

```bash
uv run coder-eval run tasks/delegate_sdk_smoke_test.yaml
uv run coder-eval run tasks/hello_date.yaml -D agent.type=delegate-sdk -D agent.model=virtuoso-1-5
```

### Task definition (YAML)

```yaml
agent:
  type: "delegate-sdk"
  # Pin a model your tenant + env actually serves (see "Model selection").
  model: "virtuoso-1-5"
  permission_mode: "acceptEdits"
  sdk_options:
    effort: "high"          # optional: low | medium | high | xhigh
  project_id: "project"     # optional: route the local wiki under <sandbox>/projects/<id>/wiki
  plugins:
    - type: local
      path: "$SKILLS_PLUGIN_PATH"

success_criteria:
  - type: file_exists
    path: "string_utils.py"
    description: "Solution file must exist"
```

### Model selection

The Delegate backend serves a model list that is **specific to your tenant and
environment** — UiPath-native models (`virtuoso-1-5`, `gemini-3-5-flash`, …) and
gateway-routed ones (`gpt-5-6-terra`, `kimi-k2-7-code`, …). List exactly what yours
offers:

```bash
npx @uipath/delegate-cli models --env alpha
```

Give Coder Eval the **hyphenated** id (`virtuoso-1-5`); the host converts it to the
backend's underscored form. Do not rely on the framework's inherited default
(`claude-sonnet-4-6` from `experiments/default.yaml`): the deployed alpha backend
rejects it (`Model 'claude_sonnet_4_6' is not available`), so an unpinned delegate run
fails. Pin a served model in the task or with `--model`.

### `sdk_options.effort` — reasoning effort

`sdk_options.effort` (`low` / `medium` / `high` / `xhigh`) sets the model's
reasoning-effort tier; the host forwards it as `user_config.effort` on every chat
request. Any other `sdk_options` keys are accepted and silently ignored, so one
experiment YAML can carry Claude-only options and still drive a `delegate-sdk`
variant. The layer-5 `-D agent.sdk_options.effort=<level>` override works too: the
guard that restricts `sdk_options` overrides is registry-driven and admits every
agent whose config declares the field.

### `project_id` / `session_id` — wiki routing

Both are client-side routing keys forwarded to the SDK. `project_id` binds sessions to
a project so the agent's local wiki lands at `<sandbox>/projects/<project_id>/wiki`
instead of the per-session `<sandbox>/sessions/<sessionId>/wiki`; `session_id` pins
the session id so that per-session directory is deterministic (a pinned id skips
`createSession`, so it must be one the backend accepts). Empty (the default) keeps the
SDK's own behavior.

### `plugins` — skills

A `plugins:` entry with a `path` is mounted as the agent's skills directory: the host
receives `<path>/skills` as `bundledSkillsPath` (the Claude-plugin layout —
`<plugin>/skills/<name>/SKILL.md`). Environment variables in the path (`$VAR`,
`${VAR}`) are expanded. **Only the first plugin is honored**; additional entries are
warned about and ignored. Point at the plugin *root*, never at the skills directory
itself — see [Run-Limit Parity](HARNESS_PARITY.md) for why the wrong depth fails
silently on this harness.

## Telemetry

The host streams one JSON object per line; `DelegateSdkAgent` reduces it into the
standardized event protocol and lets `EventCollector` build the `TurnRecord`:

| Host message | Becomes |
|---|---|
| `event.thinking` / `event.message` | `TextChunkEvent`; a `message` tagged `isStepStart` opens a new generation |
| `event.tool_call` | `ToolStartEvent` + a `CommandTelemetry` (`toolName`, `toolArgs`, `toolId`) |
| `event.tool_result` | `ToolEndEvent` (`toolStatus: failed` → `error`); the untruncated `toolResult` is the `result_summary` |
| `result` | `TurnEndEvent` + `AgentEndEvent` with `response`, `assistantStepCount`, `usage`, `turnUsages`, `model`, `maxStepsReached` |
| `error` | `AgentCrashError` with the partial turn preserved on `pending_turn` |

**Transcript.** `TurnRecord.messages` carries one `AssistantMessage` per backend
round-trip, reconstructed from stream order (a `tool_result` closes a round-trip; the
next generation activity opens the next one). Per-generation token buckets are zipped
from the `result` message's `turnUsages` list when it lines up 1:1 with the
reconstructed generations; otherwise the messages carry content and timing only and
the collector's reconciliation entry carries the turn total — the
transcript-sums-to-total invariant holds either way.

**Tokens and cost.** `usage.input_tokens` from the host is the fresh (uncached) prompt
slice and maps onto `uncached_input_tokens`; cache reads/writes arrive separately. The
Delegate SDK exposes no pricing, so `total_cost_usd` is computed locally via
`calculate_cost` on the **hyphenated** model id the backend reports (normalized from
its underscored form) — the Delegate-routed rows in `src/coder_eval/pricing.py`
(`virtuoso-*`, `gemini-3-5-flash`, `gpt-5-6-*`, `kimi-k2-7-code`, …) are keyed that
way. A model your tenant serves that has no row reports no cost until one is added.

**`max_turns`** is forwarded to the host as `maxSteps`; `maxStepsReached` on the
result becomes `max_turns_exhausted` and the run finalizes cleanly.

**Environment info.** Every task records `delegate_env`, `delegate_model`, and — when
set — the *host* of `BACKEND_URL` / `INTEROP_URL` (never the full URL) so runs against
different environments stay distinguishable and no embedded credential leaks into the
run record.

## Failure handling

Some backend failures need a specific retry shape, so the agent classifies them before
the generic categorizer sees the message:

- **First-response stall** (`DELEGATE_STALL_TIMEOUT_S`, opt-in). A turn that receives
  *no* agent activity within this many seconds of the prompt is treated as a transient
  backend stall: the wedged host is force-killed, respawned, and the prompt resent
  once, inside the same turn. Only the wait for the first activity is guarded, so a
  long-running tool call never trips it. Unset (the default) disables it — callers
  pointing at a dedicated, healthy backend are unaffected; set it to a value *above*
  the host's own SSE watchdog stack (a shared backend typically wants ~240s).
- **Backend session conflict** (`A reply is already being generated for this
  conversation`). Resending into the same conversation can only re-conflict, so the
  host is killed and the orchestrator's `AGENT_CRASH` retry gets a **fresh host**.
- **Cloudflare WAF content block** (the generic "not available in your country" 403
  page). Deterministic per payload — shell-like text in a prompt or tool result tripped
  a managed rule — so the reason is rewritten to route the failure to the non-retryable
  `AGENT_INVALID_OUTPUT` category instead of burning identical retries.
- **SSE connect watchdog** (`sse connect timeout`). The backend front door never sent
  response headers; a transient availability window, so the reason is rewritten to the
  *retryable* `AGENT_API_ERROR` signature and the live host is kept for the resend.
- **Host exit / oversized frame.** A host that dies mid-turn (or emits a single line
  over the 64 MiB stream limit) crashes the turn with the last 20 stderr lines in the
  message; retries do not respawn a dead host except in the session-conflict case.

## Tracing the host

`DELEGATE_STDIO_VERBOSE=1` makes the host trace its stdio protocol (every frame,
every agent event, its auth/init/token-refresh steps) to stderr, which the agent
forwards into the run log. `1`/`true` forces it on, `0`/`false` forces it off, and
unset follows Coder Eval's own `--verbose`. Set it when a turn looks hung: without it
the host is silent for the whole backend round-trip, so a long turn is
indistinguishable from a wedged one. `DELEGATE_STDIO_LOG_MAX_CHARS` (host-side,
default 50 000) caps each traced value.

## Running in Docker

**Not supported out of the box — use the default `tempdir` driver**, or build an
overlay image. Two things are missing from the stock `coder-eval-agent` image, both
deliberate:

- **The host is not in the image.** Baking `@uipath/delegate-stdio` in means a pinned
  version that travels with the release tag (as `CLAUDE_CODE_VERSION` does) — a
  release-process decision. An overlay image can `npm install` it into a fixed
  directory and set `DELEGATE_STDIO_PATH` to the bundle (the resolver never probes a
  global npm prefix, so a fixed path is the reliable seam).
- **No UiPath auth reaches it.** The docker driver forwards host environment variables
  through an explicit allowlist (`SandboxConfig.env_passthrough`) with no Delegate
  block. Forward what the host needs with a layer-5 override, e.g.:

```bash
uv run coder-eval run tasks/my_task.yaml --driver docker -D agent.type=delegate-sdk \
  -D 'sandbox.docker.env_passthrough_extra=[AUTH_TOKEN, DELEGATE_AUTH_TOKEN_FILE, TENANT_ID, ORG_ID, USER_ID, ORG_LOGICAL_NAME, TENANT_NAME, DELEGATE_SDK_ENV, DELEGATE_STALL_TIMEOUT_S, DELEGATE_STDIO_VERBOSE]'
```

Do **not** forward `DELEGATE_STDIO_PATH` (the image has its own copy) or the `LLMGW_*`
secret (the agent's shell tools have no use for it).

Under any driver, running several tasks in parallel on one machine wants **one shared
interop** (`INTEROP_URL`): without it each task's SDK attach-or-spawns an interop via
`~/.delegate-sdk/interop.pid` and the task that spawned it kills it on finish while
its siblings are still attached, turning every later tool call into `ECONNREFUSED`.

## Known limitations

- **`allowed_tools` / `disallowed_tools` / `system_prompt` / `system_prompt_file` /
  `setting_sources` have no Delegate SDK equivalent.** They are ignored, with a
  one-time warning at `start()`. `permission_mode` is equally unsupported but ignored
  silently — it always carries a value, so warning on it would be noise on every run.
  The run marker `system_prompt_semantics` is therefore `"unknown"`.
- **One plugin only.** The first `plugins:` entry becomes `bundledSkillsPath`; the rest
  are dropped with a warning. A plugin's agents, hooks, commands and MCP servers have
  no Delegate equivalent.
- **No cooperative early stop.** The host drives its own inner loop with no
  between-message poll point, so `run_limits.stop_early` is rejected for this harness.
- **`max_turns` counts Delegate steps**, and the SDK stops a limit-hit tool call
  *before* emitting its `tool_call` event — under `max_turns: 1` a correct first-response
  skill engagement never reaches `TurnRecord.commands`, so `skill_triggered` routing rows
  are structurally unmeasurable here. See [Run-Limit Parity](HARNESS_PARITY.md).
- **Sandbox mock CLIs need a recent host.** `SandboxConfig.mock_path_dirs` is forwarded
  as the `shellPathPrepend` init option, which the SDK injects into every shell command
  it runs inside the interop; a host whose bundled SDK predates the option ignores it and
  mock shadowing is lost.
- **No sub-agent attribution.** The host's stream carries no nested-agent boundaries.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `AgentConfigError: delegate-stdio bundle not found …` | `npm install @uipath/delegate-stdio` (step 1), or set `DELEGATE_STDIO_NODE_MODULES` / `DELEGATE_STDIO_PATH` (step 2). |
| `node not on PATH` / spawn fails | Install Node.js and make sure `node` runs in your shell. |
| `Model '<id>' is not available` | The pinned/inherited model is not in *your* tenant + environment's list. Run `npx @uipath/delegate-cli models --env <env>` and pick one it serves. |
| `AgentConfigError: Delegate SDK authentication failed during init …` | Auth missing/expired (non-retryable, fails immediately). The message says whether the saved login is expired or absent — run `npx @uipath/delegate-cli login --env <env>`, or set `AUTH_TOKEN`/`TENANT_ID`/`ORG_ID`. Confirm the org/tenant has autopilot-everywhere provisioned on `DELEGATE_SDK_ENV`. |
| `401 Invalid token: Signature has expired` mid-run | The env `AUTH_TOKEN` outlived its TTL. Point `DELEGATE_AUTH_TOKEN_FILE` at a file you keep fresh, or let the adapter-side S2S refresher take over (see "Staying authenticated"). |
| Every tool call fails with `ECONNREFUSED` after one task finishes | Parallel tasks are sharing an auto-spawned interop. Start one interop yourself and export `INTEROP_URL`. |
| A turn looks hung | Re-run with `DELEGATE_STDIO_VERBOSE=1` (or `--verbose`) to see the host's own trace; consider `DELEGATE_STALL_TIMEOUT_S` for a shared backend. |

## Bundled example and tests

```bash
uv run coder-eval run tasks/delegate_sdk_smoke_test.yaml   # needs the host + auth
uv run pytest tests/test_delegate_sdk_agent.py            # offline: replays the stdio protocol in memory
uv run pytest -m live tests/test_delegate_sdk_agent_live.py   # drives the real host; skips without prerequisites
```

## References

- Host package: [`@uipath/delegate-stdio`](https://www.npmjs.com/package/@uipath/delegate-stdio) ·
  SDK: [`@uipath/delegate-sdk`](https://www.npmjs.com/package/@uipath/delegate-sdk) ·
  CLI: [`@uipath/delegate-cli`](https://www.npmjs.com/package/@uipath/delegate-cli)
- [Run-Limit Parity](HARNESS_PARITY.md) — what `run_limits` means on this harness
- [Extending Coder Eval](../EXTENDING.md) — the agent plugin SPI
- [Task Definition Guide](../TASK_DEFINITION_GUIDE.md)
