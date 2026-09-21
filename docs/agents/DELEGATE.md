---
description: >-
  Run UiPath Autopilot's Delegate agent as the agent under evaluation in Coder
  Eval — installing the public @uipath/delegate-sdk, authentication, task
  configuration, and how Delegate telemetry maps to sandboxed, weighted scoring.
---

# Running the Delegate agent in Coder Eval

## Overview

Coder Eval can run UiPath Autopilot's **Delegate agent** as the agent under evaluation. Its reasoning runs in the UiPath backend, but its tools (shell, file, Office, PDF) execute locally through the SDK's bundled interop process — so file-based success criteria work as usual. `DelegateAgent` plugs into the same sandbox, scoring, and telemetry pipeline as every other agent: set `agent.type: delegate` in a task and the rest of the framework works unchanged.

Under the hood, this agent spawns a small first-party Node host, `agents/delegate/delegate_host.mjs`, that wraps the public `@uipath/delegate-sdk` package's `DelegateAgent` class in a newline-JSON stdio protocol. That host exists because `@uipath/delegate-stdio` — a ready-made protocol host UiPath's internal-only tooling drives — is not a public package; only `@uipath/delegate-sdk` (a programmatic library) and `@uipath/delegate-cli` (a terminal wrapper around it) are.

## Setup

### 1. Install Node and the Delegate SDK

1. **Node.js** on your `PATH`.
2. **`@uipath/delegate-sdk`**, a genuinely public npm package (verified: no token, no custom registry) — install it plain:

```bash
npm install @uipath/delegate-sdk
```

**You usually don't need to set anything about where.** When neither `DELEGATE_SDK_NODE_MODULES` nor `DELEGATE_SDK_PATH` is set, coder_eval auto-locates the install by walking up from the current directory through its ancestors (and home) — the same way Node resolves modules. Running the install inside `src/coder_eval/agents/delegate/` (this agent's own directory, which ships a `package.json` naming the dependency) is a convenient default location.

To override the auto-search, set **one** of:

| Variable | Purpose |
|---|---|
| `DELEGATE_SDK_NODE_MODULES` | Install root that holds `node_modules/@uipath/...`. |
| `DELEGATE_SDK_PATH` | Absolute path straight to `dist/index.mjs`. |

### 2. Choose a backend

| Variable | Purpose |
|---|---|
| `DELEGATE_ENV` | Cloud environment slug (`alpha` / `staging` / `production` / `localhost`). Derives the backend URL from the auth record's org/tenant slugs. |
| `DELEGATE_BACKEND_URL` | Pin the full agent-service URL directly (e.g. `https://cloud.uipath.com/<org>/<tenant>/delegate_`, or `http://localhost:5002` for a local backend). Wins over `DELEGATE_ENV` when both are set. |

### 3. Authenticate

Either:

- **Environment token** — `AUTH_TOKEN`, `TENANT_ID`, `ORG_ID` env vars. Confirmed live: when `DELEGATE_ENV` (rather than `DELEGATE_BACKEND_URL`) resolves the backend, also set `ORG_SLUG` and `TENANT_SLUG` (the human-readable org/tenant names, not the GUIDs above) — the SDK's `environment` resolution reads `organizationName`/`tenantName` off the `auth` object passed to `initialize()`, not off `process.env` directly (its own error message's "set ORG_SLUG and TENANT_SLUG env vars" advice describes the separate `delegate-cli` wrapper's behavior, not this SDK class), so this agent forwards them into `auth.organizationName`/`auth.tenantName` itself. Without them, init fails with `--env alpha needs org/tenant slugs`. Skip both by setting `DELEGATE_BACKEND_URL` directly instead.
- **Saved login** — a prior `delegate-sdk` `runLoginFlow` / `delegate-cli login` that wrote `~/.aria/sdk-auth.json`. The Node host reads this itself (via the SDK's own `loadAndRefreshAuth()`) when no `AUTH_TOKEN` is supplied — this agent does not parse that file in Python, so auth-freshness logic lives in exactly one place. This path already carries the slugs, so `ORG_SLUG`/`TENANT_SLUG` aren't needed.

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
  effort: high        # low | medium | high | xhigh
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

### Skills

A `plugins:` entry with `type: local` and a `path` is mounted as `<path>/skills` (the Delegate SDK's `bundledSkillsPath`, which expects one directory whose direct children are skill folders). If more than one plugin is configured, the first wins and a warning is logged.

## Architecture

### Class Hierarchy

```
Agent (ABC)
└── DelegateAgent
    ├── Node host subprocess (agents/delegate/delegate_host.mjs)
    │   └── @uipath/delegate-sdk's DelegateAgent class
    └── Streaming telemetry (commands, token usage, agent text)
```

### Key Methods

- **`start(working_directory)`** — resolve the Node/SDK install, spawn the host, send the `init` command.
- **`communicate(user_input, timeout, stream_callback)`** — send one `"send"` command and drain the host's forwarded events until `send_ok`/`send_error`/`fatal` or EOF.
- **`stop()`** — send `destroy`, wait bounded, then SIGKILL.
- **`kill()` / `kill_sync()`** — force-terminate the host subprocess (the latter safe to call from the orchestrator's watchdog thread).

### TurnRecord Format

Each turn returns a `TurnRecord` with:
- `agent_output` — the SDK's final response, falling back to accumulated streamed text.
- `commands` — one `CommandTelemetry` per `tool_call`/`tool_result` pair.
- `messages` — exactly **one** `AssistantMessage` per turn (see Known Limitations).
- `token_usage` — best-effort parse of the SDK's `usage` payload (see Known Limitations).
- `model_used` — the SDK-reported model, falling back to the pinned `agent.model`.

## Implementation Details

### Timeout Handling

A wall-clock deadline (`timeout`) is enforced both between reads (a top-of-loop check) and *during* a blocked read (an `asyncio.wait_for` around the queue read) — both arms raise `TurnTimeoutError` with a partial `TurnRecord` preserved in `pending_turn`, and both force-kill the host and drop the process handle so the next turn respawns a fresh one rather than reuse a host with a `"send"` still nominally in flight.

### Error Recovery

On any crash (host death, a `send_error`/`fatal` protocol message, or an unexpected exception), the agent:
1. Sets `pending_turn` to a `crashed=True` TurnRecord with captured telemetry.
2. Raises `AgentCrashError` (retryable) or `AgentConfigError` (non-retryable — missing Node/SDK install, or an SDK init rejection such as a missing `backendUrl`).
3. The orchestrator reads `pending_turn` and calls `discard_pending_turn()` to roll back state.

A dead host is always detected and its handle cleared, so a retried `communicate()` call always respawns a fresh host rather than hang or cross-wire a stale response.

### Skills Discovery

`_resolve_bundled_skills_path` maps the first `local` plugin's `path` to `<path>/skills` and forwards it as the SDK's `bundledSkillsPath` option. This is a different mapping from OpenCode/Pi's shared `agents/_skills.py` resolver: that resolver enumerates individual skill directories for a repeated `--skill <dir>`-style CLI argument, while the Delegate SDK wants exactly one parent directory whose children are skill folders.

### Non-JSON stdout tolerance

Importing `@uipath/delegate-sdk` can itself write a non-JSON diagnostic line to stdout (confirmed live) before this agent's own host script produces any output. The Python-side reader skips a line that fails to parse as JSON rather than treating it as a protocol violation, mirroring every other CLI-driven agent's tolerance for interleaved non-JSON notices.

## Differences from Claude Code Agent

| Feature | Claude Code | Delegate |
|---------|------------|----------|
| **SDK Type** | Subprocess (CLI via JSON generator) | Subprocess (a first-party Node host wrapping a programmatic SDK class) |
| **Reasoning location** | Local process | UiPath backend |
| **Tool execution** | Local | Local, through the SDK's bundled interop process |
| **System prompt** | `system_prompt` appended to the default prompt | No SDK equivalent — warned about, not enforced |
| **Session Resume** | `--resume {session_id}` | SDK `sessionId`, remembered across turns |
| **Permissions** | `permission_mode` + `allowed_tools` | No permission-prompt concept; both silently unsupported (`allowed_tools`/`disallowed_tools` warned, `permission_mode` silently ignored) |
| **Early stop** | Supported (cooperative `should_stop`, polled between messages) | Supported — polled after each forwarded event; the host is abandoned and a fresh one spawns for the next turn (no interrupt command exists) |
| **Transcript granularity** | One `AssistantMessage` per model round-trip | One `AssistantMessage` per whole turn (see Known Limitations) |

Run-limit semantics per harness: [Run-Limit Parity](HARNESS_PARITY.md).

## Known Limitations

1. **No multi-generation transcript splitting.** The SDK's event stream carries no round-trip boundary signal (no `isStepStart`/`turnUsages` equivalent), so this agent builds one `AssistantMessage` per `communicate()` call rather than one per backend round-trip.
2. **Token-bucket field names are best-effort.** The SDK confirms an event-level `usage` field exists, but not its exact internal bucket names; several plausible spellings are tried and unrecognized shapes fall back to zero rather than raising.
3. **No WAF-block-page rewrite, SSE-connect-timeout rewrite, session-conflict fresh-host recovery, or first-response stall-timeout+resend.** These are hard-won failure-signature-specific recoveries UiPath's internal tooling has needed against its own CI; they are not ported here until the same failures are observed against the public SDK/backend from this agent. A crash still ends the turn correctly as a retryable `AgentCrashError` — it is just not specially diagnosed.
4. **No `sdk_options` passthrough.** Unlike Claude Code, there is no allowlisted escape hatch for arbitrary SDK fields — only `effort`, `project_id`, `session_id`, and `enable_computer_use` are exposed as typed config fields.
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

Live integration tests (drive a real `@uipath/delegate-sdk` install against a real backend; skipped unless credentials are configured):

```bash
uv run pytest -m live tests/test_delegate_agent_live.py
```

CI runs the same kind of check as a manually-dispatched `delegate-live-tests` job in
[`pr-checks.yml`](../../.github/workflows/pr-checks.yml): it mints a fresh access
token via the OAuth2 Resource Owner Password Credentials (ROPC) grant against a
dedicated bot user, then runs `tasks/delegate/fizzbuzz_delegate.yaml` end to end and
asserts `final_status` is a PASS. It skips cleanly (never fails a required check)
until the `DELEGATE_ROPC_*` / `DELEGATE_ORG_ID` / `DELEGATE_TENANT_ID` repo secrets
are provisioned.

## References

- SDK package: [`@uipath/delegate-sdk`](https://www.npmjs.com/package/@uipath/delegate-sdk)
- CLI package (not required by this agent, but shares the same auth file): [`@uipath/delegate-cli`](https://www.npmjs.com/package/@uipath/delegate-cli)
- Task examples: `tasks/delegate/*.yaml`
