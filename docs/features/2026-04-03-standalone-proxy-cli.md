# Standalone `coder-eval proxy` CLI Command

**Related PR:** feat/standalone-proxy-cli branch
**Status:** Implemented
**Date:** 2026-04-03

## Problem

The LLM Gateway proxy already exists as an internal component of `coder-eval run --proxy`, but there's no way to use it standalone. Users who want to run Claude Code CLI interactively (not through the eval framework) still need an `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`. If the organization routes all LLM traffic through the UiPath LLM Gateway, there's no way to use `claude` without a direct Anthropic key.

## Solution

Expose the existing `LLMGatewayProxy` as a standalone CLI command: `coder-eval proxy`. This starts a local HTTP server that translates standard Anthropic API calls into UiPath LLM Gateway requests, handling OAuth2 S2S authentication transparently.

## Architecture

```
┌──────────────┐   ANTHROPIC_BASE_URL=        ┌──────────────┐    OAuth Bearer +     ┌──────────────┐
│  Claude Code  │── http://127.0.0.1:PORT ───▶│ coder-eval   │─── LLMGW headers ───▶│  UiPath LLM  │
│  CLI          │                              │ proxy        │                       │  Gateway     │
└──────────────┘                               └──────────────┘                       └──────────────┘
       ▲                                              │
       │                                              │  POST /identity_/connect/token
  ANTHROPIC_API_KEY=                                  │  (S2S OAuth, cached)
  llmgw-proxy (dummy)                                 ▼
                                            ┌──────────────────────┐
                                            │ LLM Gateway Identity │
                                            └──────────────────────┘
```

## Usage

### Interactive (two terminals)

```bash
# Terminal 1: start proxy
coder-eval proxy --port 8080

# Terminal 2: use claude
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
export ANTHROPIC_API_KEY=llmgw-proxy
claude
```

### Scripted / CI (one-liner)

```bash
eval "$(coder-eval proxy --port 8080 -q &)"
sleep 2
claude -p "hello"
```

The `--quiet` (`-q`) flag prints only the `export` commands to stdout, making it compatible with `eval "$()"`. All human-readable output goes to stderr.

## CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | `0` (auto) | Port to bind to. `0` picks a free port automatically. |
| `--env-file` | `.env` | Path to `.env` file with LLM Gateway credentials. |
| `--vendor` | `awsbedrock` | Gateway vendor (`awsbedrock`, `anthropic`). |
| `--api-flavor` | `invoke` | Gateway API flavor. |
| `--quiet, -q` | `false` | Only print `export` commands to stdout (for `eval` usage). |

## Required Environment Variables

Set these in your `.env` file or as environment variables:

| Variable | Description |
|----------|-------------|
| `LLMGW_URL` | UiPath LLM Gateway base URL |
| `LLMGW_CLIENT_ID` | OAuth2 client ID for S2S authentication |
| `LLMGW_CLIENT_SECRET` | OAuth2 client secret |
| `LLMGW_SEMANTIC_ORG_ID` | Organization ID for gateway routing |
| `LLMGW_SEMANTIC_TENANT_ID` | Tenant ID for gateway routing |

Optional:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLMGW_SEMANTIC_USER_ID` | `""` | User ID for audit/tracking |
| `LLMGW_REQUESTING_PRODUCT` | `coder-eval` | Product name header |
| `LLMGW_REQUESTING_FEATURE` | `claude-code-agent` | Feature name header |
| `LLMGW_TIMEOUT_SECONDS` | `300` | Gateway request timeout |

## How It Works

1. Loads credentials from `.env` file (or environment variables)
2. Validates required settings, exits with a clear error if any are missing
3. Starts an `aiohttp` server on `127.0.0.1:<port>`
4. Handles `POST /v1/messages` (streaming + non-streaming) and `POST /v1/messages/count_tokens`
5. For each request:
   - Maps model names (e.g., `claude-sonnet-4-6` → `anthropic.claude-sonnet-4-6`)
   - Acquires/refreshes OAuth2 bearer token via S2S flow
   - Strips non-allowlisted body fields (e.g. `model`, `stream`) for Bedrock
   - Forwards to the LLM Gateway passthrough endpoint
   - Streams the response back transparently
6. On shutdown (Ctrl+C), prints usage summary (requests, tokens, cost)

## Bedrock Compatibility

When `--vendor=awsbedrock` (default), the proxy:

- Strips body fields not in the Bedrock allowlist (e.g., `stream`)
- Sets `anthropic_version` to `bedrock-2023-05-31`
- Forwards `cache_control` blocks (`system`, `messages`, `tools`) intact — Bedrock supports Anthropic prompt caching with the same syntax as the direct API

## Relationship to `--proxy` Flag

| Feature | `coder-eval run --proxy` | `coder-eval proxy` |
|---------|--------------------------|---------------------|
| Proxy lifecycle | Managed by orchestrator (start/stop per run) | User-managed (runs until Ctrl+C) |
| Client | Claude Code Agent SDK (subprocess) | Any tool that speaks Anthropic API |
| Use case | Automated evaluations | Interactive `claude` CLI, scripts, CI |
| Configuration | Via `Settings` / `.env` | Via CLI flags + `.env` |

Both use the same underlying `LLMGatewayProxy` class — no code duplication.

## Implementation Details

### Files Changed

- **`cli/proxy_command.py`** (new) — CLI command: env loading, validation, proxy lifecycle, signal handling, Rich output
- **`proxy/server.py`** (modified) — Vendor-gated body-field allowlist + Bedrock `anthropic_version` injection
- **`cli/__init__.py`** (modified) — Registered the `proxy` command

### Design Decisions

1. **stderr for human output** — All Rich-formatted messages go to `Console(stderr=True)` so that `eval "$(coder-eval proxy -q)"` works correctly.
2. **Deferred imports** — Heavy imports (`dotenv`, `ProxyConfig`, `LLMGatewayProxy`) are inside the async function to keep CLI startup fast.
3. **`contextlib.suppress` for clean exit** — Wraps `asyncio.run()` to suppress `KeyboardInterrupt`/`SystemExit` tracebacks from Ctrl+C.
4. **asyncio signal handling** — Uses `loop.add_signal_handler()` (the correct asyncio pattern, not `signal.signal()`).
