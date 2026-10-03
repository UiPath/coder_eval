---
description: >-
  Run UiPath Autopilot's Delegate agent as the agent under evaluation in Coder
  Eval — installing the public @uipath/delegate-stdio host, authentication, task
  configuration, and how Delegate telemetry maps to sandboxed, weighted scoring.
---

# Running the Delegate agent in Coder Eval

## Overview

Coder Eval can run UiPath Autopilot's **Delegate agent** as the agent under evaluation. Its reasoning runs in the UiPath backend, but its tools (shell, file, Office, PDF) execute locally through the SDK's bundled interop process — so file-based success criteria work as usual. `DelegateAgent` plugs into the same sandbox, scoring, and telemetry pipeline as every other agent: set `agent.type: delegate` in a task and the rest of the framework works unchanged.

Under the hood, this agent spawns the host that the public [`@uipath/delegate-stdio`](https://www.npmjs.com/package/@uipath/delegate-stdio) package ships (`dist/delegate_stdio.mjs`). The host runs the Delegate agent as a subprocess that speaks newline-delimited JSON over stdio. `@uipath/delegate-sdk` and your platform's `@uipath/delegate-runtime-*` interop binaries are dependencies of that package, so one install is the complete install.

## Setup

### 1. Install the Delegate host

With **Node.js** on your `PATH`, install the host once:

```bash
npm install -g @uipath/delegate-stdio
```

That is all. The package is public (no token, no custom registry). coder_eval finds the global install by itself, so you set no path. coder_eval needs version 1.203.0 or later, the first release that accepts the `auth` init option and writes a `usage` frame for each model call.

A local `npm install @uipath/delegate-stdio` also works: in your project, in a parent directory, or in `src/coder_eval/agents/delegate/` (it ships a `package.json`). A local install wins over a global one. On Windows, keep a local install in a short path: a path longer than 260 characters to the interop binary makes its spawn fail with `ENOENT`.

> **CI only.** To pin one exact build, for example a build from source or an install in a temporary directory, set `DELEGATE_STDIO_PATH` to its `dist/delegate_stdio.mjs`.

### 2. Choose a backend

| Variable | Purpose |
|---|---|
| `DELEGATE_ENV` | Cloud environment slug (`alpha` / `staging` / `production`), sent as the host's `env` init option. The host derives the backend URL from the org/tenant slugs. |
| `DELEGATE_BACKEND_URL` | Pin the full agent-service URL directly (e.g. `https://cloud.uipath.com/<org>/<tenant>/delegate_`, or `http://localhost:5002` for a local backend), sent as the host's `backendUrl` init option. Wins over `DELEGATE_ENV` when both are set. |
| `INTEROP_URL` | Connect to an interop instance that is already running, instead of letting the SDK spawn its own. The host reads it itself. |

### 3. Authenticate

Either:

- **Environment token** — `DELEGATE_AUTH_TOKEN`, `DELEGATE_TENANT_ID`, `DELEGATE_ORG_ID` env vars. When `DELEGATE_ENV` (not `DELEGATE_BACKEND_URL`) resolves the backend, also set `DELEGATE_ORG_SLUG` and `DELEGATE_TENANT_SLUG`. These are the human-readable org/tenant names, not the GUIDs. Without the slugs, init fails with `env="alpha" requires org/tenant slugs`. To skip the slugs, set `DELEGATE_BACKEND_URL` instead. Each variable also accepts the bare spelling (`AUTH_TOKEN`, `TENANT_ID`, `ORG_ID`, `ORG_SLUG`, `TENANT_SLUG`) as a fallback. Prefer the `DELEGATE_` spelling in a shared environment, because the bare names collide with what other tooling (npm, Vault, Terraform) commonly exports.
- **Saved login** — a prior `npx @uipath/delegate-cli login --env <env>` that wrote `~/.aria/sdk-auth.json`. The host reads and refreshes this file itself when no token is supplied. This agent does not parse that file in Python, so auth-freshness logic lives in exactly one place. The saved login already carries the slugs, so `DELEGATE_ORG_SLUG` / `DELEGATE_TENANT_SLUG` are not needed.

coder_eval sends these values to the host as its `auth` init option, on stdin. It also removes the host's own variable names (`AUTH_TOKEN`, `TENANT_ID`, `ORG_ID`, `ORG_LOGICAL_NAME`, `TENANT_NAME`, `BACKEND_URL`) and `DELEGATE_AUTH_TOKEN` from the host's environment. So the agent's shell commands do not get the token in their environment, and a variable that another tool exports does not change where the host connects.

This is defense in depth, not a security boundary. Code under test runs as your user, so it can still get to a credential through:

- the token file that `DELEGATE_AUTH_TOKEN_FILE` names, or the saved login in `~/.aria/sdk-auth.json`;
- the `LLMGW_*` client secret, when no token file is set (see below);
- the host's own `AUTH_TOKEN`, which the host writes into its environment when it refreshes the token;
- the environment of the coder_eval process, which still holds `DELEGATE_AUTH_TOKEN` (on Linux, through `/proc/<pid>/environ`).

The host also reads its own advanced variables directly, such as `DELEGATE_AUTH_TOKEN_FILE` (a token file that an external process keeps fresh) and `DELEGATE_STDIO_VERBOSE=1` (trace every frame to stderr). See the [package README](https://www.npmjs.com/package/@uipath/delegate-stdio).

For runs longer than the token's lifetime (about one hour), the host refreshes the token itself. It uses the token file when `DELEGATE_AUTH_TOKEN_FILE` (or the older `AUTH_TOKEN_FILE`) is set. If no token file is set, it uses the `LLMGW_CLIENT_ID` / `LLMGW_CLIENT_SECRET` / `LLMGW_URL` S2S pair. The agent's shell tools inherit the host's environment, so when a token file is set, coder_eval removes the `LLMGW_*` variables from that environment: the host does not need them, and the code under test cannot read the client secret. When no token file is set, the variables stay, because the host needs them to refresh the token.

## Usage

### Command Line

```bash
coder-eval run tasks/delegate/hello_date_delegate.yaml --type delegate --model virtuoso-1-5
```

### Task Definition (YAML)

```yaml
agent:
  type: delegate
  model: virtuoso-1-5
  sdk_options:
    effort: high      # low | medium | high | xhigh | max
  project_id: invoice-approval  # client-side wiki-routing key; None means session-scoped wiki state
  session_id: abc-123           # pins the SDK session id a turn omits; project_id takes precedence
  enable_computer_use: false   # default; screen tools off, file/shell/Office/PDF unaffected
  plugins:
    - type: local
      path: "$SKILLS_PLUGIN_PATH"

success_criteria:
  - type: file_exists
    path: "solution.py"
    description: "Solution file must exist"
```

`enable_computer_use` defaults to `false` — this is a headless eval harness: `true` requires macOS Accessibility/Screen-Recording grants a CI runner does not have, and the SDK throws unconditionally on Linux when it's enabled. File, shell, Office and PDF tools are unaffected either way.

`project_id` and `session_id` both route the SDK to a "wiki" — its per-project/per-session persistent scratch state. `project_id` is client-side only (never sent to the backend) and picks a stable directory; leaving it unset falls back to session-scoped state under a server-assigned session id instead. `session_id`, unlike `project_id`, IS a backend entity: pinning one skips session creation, so it must be an id the backend already accepts — do not guess one.

### Skills

A `plugins:` entry with `type: local` and a `path` is mounted as `<path>/skills` (the Delegate SDK's `bundledSkillsPath`, which expects one directory whose direct children are skill folders), and turns on `enableSkills`. With no plugin, skills stay off. If more than one plugin is configured, the first wins and a warning is logged.

The SDK loads a catalog skill with `LoadSkill {"name", "plugin"}`. The adapter records it as `Skill {"skill", "plugin"}`, the call Claude Code makes, so `skill_triggered` and other skill-loaded criteria see the load.

## Architecture

### Class Hierarchy

```
Agent (ABC)
└── DelegateAgent
    ├── Node host subprocess (@uipath/delegate-stdio's dist/delegate_stdio.mjs)
    │   └── @uipath/delegate-sdk's DelegateAgent class
    └── Streaming telemetry (commands, token usage, agent text)
```

### Key Methods

- **`start(working_directory)`** — resolve the host install, spawn the host, send the `init` command and wait for `init_ok`.
- **`communicate(user_input, timeout, stream_callback)`** — send one `"send"` command and drain the host's `event` frames until `result`, `error` or EOF.
- **`stop()`** — send `destroy`, wait bounded, then SIGKILL.
- **`kill()` / `kill_sync()`** — force-terminate the host subprocess (the latter safe to call from the orchestrator's watchdog thread).

### TurnRecord Format

Each turn returns a `TurnRecord` with:
- `agent_output` — the SDK's final response, falling back to accumulated streamed text.
- `commands` — one `CommandTelemetry` per `tool_call`/`tool_result` pair.
- `messages` — exactly **one** `AssistantMessage` per turn (see Known Limitations).
- `token_usage` — the `result` frame's `usage` (Anthropic convention: `input_tokens` excludes cache reads and writes). A turn cut before that frame keeps the sum of the `usage` frames that the host sends for each model call.
- `num_turns` — the length of the `result` frame's `turnUsages` (one entry per backend round-trip).
- `model_used` — the `result` frame's `model`, falling back to the pinned `agent.model`.

## Implementation Details

### Timeout Handling

A wall-clock deadline (`timeout`) is enforced both between reads (a top-of-loop check) and *during* a blocked read (an `asyncio.wait_for` around the queue read) — both arms raise `TurnTimeoutError` with a partial `TurnRecord` preserved in `pending_turn`, and both force-kill the host and drop the process handle so the next turn respawns a fresh one rather than reuse a host with a `"send"` still nominally in flight.

### Error Recovery

On any crash (host death, an `error` frame during a turn, or an unexpected exception), the agent:
1. Sets `pending_turn` to a `crashed=True` TurnRecord with captured telemetry.
2. Raises `AgentCrashError` (retryable) or `AgentConfigError` (non-retryable — missing Node/SDK install, or an init error that a retry cannot fix: missing or rejected auth, missing org/tenant slugs, or an unknown `DELEGATE_ENV`). Any other init error, and an init that does not respond within 60 s, is retryable, unless its message matches a category that is not retried (a timeout, or an auth, billing or content-filter error). An HTTP status code matches only as a whole word, so a port or a GUID that contains `401` stays retryable.
3. The orchestrator reads `pending_turn` and calls `discard_pending_turn()` to roll back state.

The crash reason never includes the host's stderr. The agent logs the last 20 stderr lines at WARNING instead, because the error categorizer matches words in the reason, and stderr contains the sandbox path, which contains the task id.

A dead host is always detected and its handle cleared, so a retried `communicate()` call always respawns a fresh host rather than hang or cross-wire a stale response.

### Skills Discovery

`_resolve_bundled_skills_path` maps the first `local` plugin's `path` to `<path>/skills` and forwards it as the SDK's `bundledSkillsPath` option. This is a different mapping from OpenCode/Pi's shared `agents/_skills.py` resolver: that resolver enumerates individual skill directories for a repeated `--skill <dir>`-style CLI argument, while the Delegate SDK wants exactly one parent directory whose children are skill folders.

### Non-JSON stdout tolerance

The host writes some non-JSON diagnostic lines to stdout (confirmed live), for example `[backendUrl] Module loaded ...` and `[DelegateAgent] Using model: ...`. The Python-side reader skips a line that fails to parse as JSON rather than treating it as a protocol violation, mirroring every other CLI-driven agent's tolerance for interleaved non-JSON notices.

## Differences from Claude Code Agent

| Feature | Claude Code | Delegate |
|---------|------------|----------|
| **SDK Type** | Subprocess (CLI via JSON generator) | Subprocess (the `@uipath/delegate-stdio` Node host) |
| **Reasoning location** | Local process | UiPath backend |
| **Tool execution** | Local | Local, through the SDK's bundled interop process |
| **System prompt** | `system_prompt` appended to the default prompt | No SDK equivalent — warned about, not enforced |
| **Session Resume** | `--resume {session_id}` | SDK `sessionId`, remembered across turns |
| **Permissions** | `permission_mode` + `allowed_tools` | No permission-prompt concept; both silently unsupported (`allowed_tools`/`disallowed_tools` warned, `permission_mode` silently ignored) |
| **Early stop** | Supported (cooperative `should_stop`, polled between messages) | Supported — polled after each forwarded event; the host is abandoned and a fresh one spawns for the next turn (no interrupt command exists) |
| **Transcript granularity** | One `AssistantMessage` per model round-trip | One `AssistantMessage` per whole turn (see Known Limitations) |

Run-limit semantics per harness: [Run-Limit Parity](HARNESS_PARITY.md).

## Known Limitations

1. **No multi-generation transcript splitting.** This agent builds one `AssistantMessage` per `communicate()` call, not one per backend round-trip. The host does send the signals a split needs (`isStepStart` on `message` events and per-round-trip `turnUsages` on `result`), but this agent does not use them yet.
2. **`max_turns` is enforced by coder_eval, not by the host.** The host's `maxSteps` option on `send` does not stop the turn (confirmed live: `maxSteps: 2` ran 7 steps and only reported `maxStepsReached: true`), so this agent does not send it. See [Run-Limit Parity](HARNESS_PARITY.md).
   A turn cut at `max_turns`, or by a cooperative early stop, keeps the token usage and cost of every model call that finished before the cut, as on the other agents. The host sends a `usage` frame for each call before that call's tool results. The call in progress at the cut has no usage. A host that does not send `usage` frames reports usage only on its final `result` frame, which a cut turn never gets: that turn has no usage, and the agent logs a warning.
3. **Only three backend failures get a specific diagnosis.** A Cloudflare WAF block page is reported as a content-filter failure and is not retried: the same prompt or tool result is blocked again. An SSE connect timeout is reported as a connection failure and is retried. A session conflict ("A reply is already being generated") is retried in a new conversation when no turn of that conversation has finished. After a finished turn, or with a `session_id` set in the config, the retry stays in the same conversation, so the agent never continues without its earlier turns: the retry succeeds once the backend releases the conversation (it stops the reply when the host disconnects), or the task ends as an error. There is no first-response stall detection: a stalled turn ends at its turn timeout. Every other crash ends the turn as an `AgentCrashError`, which is retried unless its message matches a category that is not retried (a timeout, or an auth, billing or content-filter error).
4. **`sdk_options` accepts only `effort`.** Reasoning effort uses the same `sdk_options.effort` key as Claude Code, so `-D agent.sdk_options.effort=high` works for both agents. Any other key, or an `effort` that is not a string, is a validation error. The host checks the tier itself: it logs a tier it does not recognize and uses the model's default. `project_id`, `session_id` and `enable_computer_use` are typed config fields.
5. **`enable_computer_use: true` requires local permissions and is unavailable on Linux.** macOS needs Accessibility + Screen Recording grants; the SDK throws unconditionally on Linux when this is enabled.

## Testing

Unit tests (no real Node process — a fake host replays the stdio protocol in memory):

```bash
uv run pytest tests/test_delegate_agent.py
```

Registration/pricing tests:

```bash
uv run pytest tests/test_delegate_agent_registration.py
```

Live integration tests (drive a real `@uipath/delegate-stdio` install against a real backend; skipped unless credentials are configured):

```bash
uv run pytest -m live tests/test_delegate_agent_live.py
```

CI runs the same kind of check as the `delegate-live-tests` job in
[`pr-checks.yml`](../../.github/workflows/pr-checks.yml): it mints a fresh access
token via the OAuth2 Resource Owner Password Credentials (ROPC) grant against a
dedicated bot user, then runs `tasks/delegate/fizzbuzz_delegate.yaml` end to end and
asserts `final_status` is a PASS. It skips cleanly (never fails a required check)
until the `DELEGATE_ROPC_*` / `DELEGATE_ORG_ID` / `DELEGATE_TENANT_ID` repo secrets
are provisioned.

## References

- Host package: [`@uipath/delegate-stdio`](https://www.npmjs.com/package/@uipath/delegate-stdio) — its README is the wire-protocol reference
- SDK package (installed by the host package): [`@uipath/delegate-sdk`](https://www.npmjs.com/package/@uipath/delegate-sdk)
- CLI package (not required by this agent, but shares the same auth file): [`@uipath/delegate-cli`](https://www.npmjs.com/package/@uipath/delegate-cli)
- Task examples: `tasks/delegate/*.yaml`
