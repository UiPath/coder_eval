# Local Proxy for Routing Claude Code Agent SDK Traffic Through LLM Gateway

> **Note:** As of 2026-04-06, `LLMGW_PROXY_ENABLED` was replaced by `API_BACKEND=proxy`. See README.md for current configuration.

**Related PR:** #45
**Status**: Draft
**Date**: 2026-03-05

## Problem

The Claude Code Agent SDK spawns the Claude Code CLI as a subprocess. All Anthropic API traffic flows through that CLI process, which authenticates directly with `api.anthropic.com` using `ANTHROPIC_API_KEY`. We want to route this traffic through UiPath LLM Gateway instead, for centralized billing, access control, and audit.

## Architecture

```
┌──────────────┐     ANTHROPIC_BASE_URL=     ┌──────────────┐      OAuth Bearer +      ┌──────────────┐
│  Claude Code  │ ──── http://localhost:PORT ──▶│  Local Proxy  │ ──── LLMGW headers ────▶│ LLM Gateway  │
│  CLI (subprocess)│                          │  (Python)     │                          │  (UiPath)    │
└──────────────┘                              └──────────────┘                          └──────────────┘
       ▲                                            │
       │                                            │  POST /identity_/connect/token
  ClaudeAgentOptions(                               │  (S2S OAuth, cached)
    env={                                           ▼
      ANTHROPIC_BASE_URL,               ┌──────────────────────┐
      ANTHROPIC_API_KEY=dummy            │ LLM Gateway Identity │
    }                                    └──────────────────────┘
  )
```

## How It Works

### 1. CLI sends standard Anthropic API requests

The Claude Code CLI calls:
- `POST /v1/messages` — chat completions (streaming and non-streaming)
- `POST /v1/messages/count_tokens` — token counting

Requests include:
- `x-api-key: {ANTHROPIC_API_KEY}` header
- `anthropic-version` header
- Standard Anthropic Messages API JSON body (`model`, `messages`, `max_tokens`, `tools`, `system`, etc.)
- `"stream": true` for streaming requests

### 2. Proxy intercepts and transforms

The proxy:
1. Strips the `x-api-key` header
2. Adds `Authorization: Bearer {oauth_token}` (from cached S2S token)
3. Adds required LLM Gateway headers (see below)
4. Rewrites the URL path to the LLM Gateway passthrough endpoint
5. Forwards the request body unchanged
6. Streams the response back transparently

### 3. LLM Gateway passthrough endpoint

Target URL pattern:
```
{LLMGW_URL}/{org_id}/{tenant_id}/llmgateway_/api/raw/vendor/awsbedrock/model/{model}/completions
```

The model name is extracted from the request JSON body (`model` field) and placed into the URL path.

## Detailed Design

### Proxy Server

**Technology**: Python `aiohttp` (already available via `anyio` dependency tree, or use `httpx` with ASGI). Alternative: lightweight `http.server` + `httpx` async client.

**Recommended**: Use `aiohttp` for its mature streaming support on both server and client sides.

**Module**: `coder_eval/proxy/llmgw_proxy.py`

```
coder_eval/proxy/
├── __init__.py
├── server.py          # aiohttp server, request handler, lifecycle
├── auth.py            # S2S OAuth token management (acquire, cache, refresh)
└── config.py          # Proxy configuration (ports, LLMGW settings)
```

### Request Handler (pseudocode)

```python
async def handle_request(request):
    # 1. Read the incoming request
    body = await request.read()
    payload = json.loads(body)

    # 2. Extract model from body to build gateway URL
    model = payload.get("model", "")

    # 3. Determine target URL
    #    /v1/messages         -> passthrough completions endpoint
    #    /v1/messages/count_tokens -> needs special handling (see Open Questions)
    path = request.path
    if path == "/v1/messages":
        target_url = build_passthrough_url(model, api_type="completions")
    else:
        # Fallback: forward to direct Anthropic API (or return 404)
        ...

    # 4. Build headers
    headers = {
        "Authorization": f"Bearer {await get_token()}",
        "Content-Type": "application/json",
        "X-UiPath-LlmGateway-RequestingProduct": config.requesting_product,
        "X-UiPath-LlmGateway-RequestingFeature": config.requesting_feature,
        "X-UiPath-LlmGateway-UserId": config.user_id,
        "X-UiPath-LlmGateway-TimeoutSeconds": "300",
        "X-UiPath-LLMGateway-AllowFull4xxResponse": "true",
        "X-UiPath-LlmGateway-ApiFlavor": "invoke",  # for awsbedrock vendor
    }

    # 5. Set streaming header based on request body
    is_streaming = payload.get("stream", False)
    headers["X-UiPath-Streaming-Enabled"] = str(is_streaming).lower()

    # 6. Forward request
    if is_streaming:
        return await forward_streaming(target_url, headers, body)
    else:
        return await forward_sync(target_url, headers, body)
```

### Streaming Passthrough

For streaming requests (`"stream": true`), the proxy must:
1. Open a streaming connection to LLM Gateway
2. Set `Transfer-Encoding: chunked` on the response to the CLI
3. Forward each SSE chunk as it arrives, without buffering
4. Preserve the `event: ...` and `data: ...` SSE format exactly

```python
async def forward_streaming(target_url, headers, body):
    async with httpx.AsyncClient() as client:
        async with client.stream("POST", target_url, headers=headers, content=body) as upstream:
            response = web.StreamResponse(
                status=upstream.status_code,
                headers={"Content-Type": "text/event-stream"},
            )
            await response.prepare(request)
            async for chunk in upstream.aiter_bytes():
                await response.write(chunk)
            return response
```

### OAuth Token Management

Reuse the same S2S flow as `BearerAuthWithRetry`:

```python
class TokenManager:
    def __init__(self, config):
        self._token: str | None = None
        self._lock = asyncio.Lock()
        self._config = config

    async def get_token(self) -> str:
        if self._token is None:
            async with self._lock:
                if self._token is None:
                    self._token = await self._acquire_token()
        return self._token

    async def refresh_token(self):
        """Called on 401 from gateway."""
        async with self._lock:
            self._token = await self._acquire_token()

    async def _acquire_token(self) -> str:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._config.llmgw_url}/identity_/connect/token",
                data={
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                    "grant_type": "client_credentials",
                },
            )
            response.raise_for_status()
            return response.json()["access_token"]
```

On 401 responses from the gateway, the proxy should:
1. Refresh the token
2. Retry the request once
3. If still 401, return the error to the CLI

### Integration with ClaudeCodeAgent

In `claude_code_agent.py`, inject env vars when creating `ClaudeAgentOptions`:

```python
options = ClaudeAgentOptions(
    ...,
    env={
        "ANTHROPIC_BASE_URL": f"http://localhost:{proxy_port}",
        "ANTHROPIC_API_KEY": "llmgw-proxy",  # dummy, required by CLI
    },
)
```

The proxy port is either:
- Configured in `.env` / `Settings`
- Dynamically assigned (bind to port 0, read back actual port)

### Proxy Lifecycle

The proxy must start before the agent and stop after:

```python
# In Orchestrator or a new wrapper
proxy = LLMGatewayProxy(config)
proxy_port = await proxy.start()  # returns bound port

try:
    # Pass proxy_port to agent configuration
    agent_config.proxy_port = proxy_port
    await orchestrator.run(...)
finally:
    await proxy.stop()
```

**Option A — Per-run proxy**: Start/stop with each evaluation run. Simple, clean lifecycle.

**Option B — Long-lived proxy**: Start once, reuse across runs. More efficient for batch evaluations. Managed by the CLI entry point.

Recommend **Option A** for v1 simplicity. The startup cost (one S2S token request + bind a socket) is negligible compared to evaluation time.

### Configuration

Add to `Settings` in `config.py`:

```python
# LLM Gateway Proxy settings
llmgw_proxy_enabled: bool = False
llmgw_proxy_vendor: str = "awsbedrock"   # vendor for passthrough endpoint
llmgw_proxy_api_flavor: str = "invoke"   # API flavor for the vendor
```

All other LLMGW settings (`LLMGW_URL`, `LLMGW_CLIENT_ID`, etc.) are already in `Settings`.

### Model Name Mapping

The Claude Code CLI sends model names like `claude-sonnet-4-20250514`. The LLM Gateway passthrough URL needs the model name in its path. Two options:

**Option 1 — Pass-through as-is**: Use the CLI model name directly in the gateway URL path. This works if the gateway recognizes standard Anthropic model IDs.

**Option 2 — Configurable mapping**: Add a `model_map` dict in config for cases where the gateway expects different names (e.g., `anthropic.claude-3-5-sonnet-20240620-v1:0`):

```python
llmgw_proxy_model_map: dict[str, str] = {}
# Example: {"claude-sonnet-4-20250514": "anthropic.claude-sonnet-4-20250514-v1:0"}
```

Recommend starting with **Option 1** and adding mapping only if needed.

## Implementation Plan

### Phase 1: Core Proxy (MVP)

1. - [x] **`coder_eval/proxy/auth.py`** — `TokenManager` class
   - S2S token acquisition via `identity_/connect/token`
   - Async lock for thread safety
   - 401 retry with token refresh

2. - [x] **`coder_eval/proxy/server.py`** — Proxy server
   - `aiohttp` web server on localhost
   - Handler for `POST /v1/messages` (streaming + non-streaming)
   - Header transformation (strip `x-api-key`, add Bearer + LLMGW headers)
   - URL rewrite to passthrough endpoint
   - Transparent SSE streaming passthrough

3. - [x] **`coder_eval/proxy/config.py`** — Configuration
   - Read from existing `Settings` (LLMGW_URL, credentials, etc.)
   - Add `llmgw_proxy_enabled` flag

4. - [x] **Integration in `claude_code_agent.py`**
   - When proxy is enabled, inject `ANTHROPIC_BASE_URL` and dummy `ANTHROPIC_API_KEY` into `ClaudeAgentOptions.env`

5. - [x] **Proxy lifecycle in `orchestrator.py`**
   - Start proxy before evaluation, stop after
   - Pass port to agent config

### Phase 2: Robustness

6. **`/v1/messages/count_tokens` handling** — either proxy to gateway or fall back to direct API
7. **Error mapping** — translate gateway errors to Anthropic-compatible error format so the CLI handles them gracefully
8. **Logging** — request/response logging at DEBUG level for troubleshooting
9. **Health check endpoint** — `GET /health` for proxy readiness verification
10. **Timeout configuration** — configurable client-side and server-side timeouts

### Phase 3: Polish

11. **Model name mapping** — optional config for gateway-specific model names
12. - [x] **Metrics** — token usage tracking, request counts, latency, cost calculation
13. **Tests** — unit tests for header transformation, token management; integration test with mock gateway
14. **Documentation** — update task definition guide and .env.example

## Open Questions

1. **`/v1/messages/count_tokens`**: Does the LLM Gateway passthrough support this endpoint? If not, should we fall back to the direct Anthropic API, skip it, or return a synthetic response?

2. **Gateway model name format**: Do we need the `anthropic.` prefix or version suffix (e.g., `-v1:0`) in the passthrough URL path? Need to test with the actual gateway.

3. **Vendor selection**: The current plan assumes `awsbedrock` as the vendor. Should this be configurable per-task or globally?

4. **Error response format**: Does the LLM Gateway return errors in the same format as the Anthropic API? The CLI may fail hard on unexpected error formats.

5. **Extended thinking / betas**: The CLI sends `anthropic-beta` headers for features like extended thinking. Does the gateway passthrough forward these correctly?

6. **Concurrent requests**: The CLI may make concurrent API calls (e.g., for subagents). The proxy must handle concurrent connections. `aiohttp` handles this natively.

7. **Dependency choice**: Should we add `aiohttp` as a dependency, or use `httpx` for both client and server (via an ASGI adapter)? `httpx` is already a transitive dependency via `uipath_llmgw_client`. Using `httpx` for the client side and a minimal `asyncio` HTTP server could avoid a new dependency.

## Risks

- **API compatibility**: The LLM Gateway passthrough may not support all Anthropic API features (extended thinking, tool use streaming, etc.). The proxy approach makes debugging easier since we can log full request/response pairs.
- **Latency**: Adds one network hop (localhost). Negligible for localhost, but token acquisition adds ~200-500ms on first request.
- **Token expiry**: S2S tokens have a TTL. Long-running evaluations (hours) may hit expiry. The 401-retry logic handles this, but the retry adds latency to that one request.
- **Binary protocol changes**: If Claude Code CLI changes its API calling behavior in future versions, the proxy may need updates. The thin proxy design minimizes surface area.
