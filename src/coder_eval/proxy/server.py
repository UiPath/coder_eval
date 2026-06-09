"""Local proxy server that routes Anthropic API requests through LLM Gateway."""

import asyncio
import base64
import contextlib
import json
import logging
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError

from coder_eval.errors.categories import RetryConfig
from coder_eval.errors.retry import compute_backoff
from coder_eval.models import TokenUsage

from .auth import TokenManager
from .config import DEFAULT_MODEL_MAP, ProxyConfig
from .pricing import calculate_cost


logger = logging.getLogger(__name__)

_UPSTREAM_TIMEOUT = httpx.Timeout(timeout=300.0, connect=30.0)

# Retry settings for rate-limit (429) and overload (529) responses.
_RETRYABLE_STATUS_CODES = {429, 529}
_MAX_BACKOFF_S = 60.0
_MAX_SSE_BUFFER_BYTES = 5_000_000  # 5 MB cap to prevent unbounded memory growth
_RETRY_CFG = RetryConfig(max_retries=4, backoff_multiplier=2.0, initial_delay=1.0)

# Body fields accepted by the AWS Bedrock Anthropic Messages API (via gateway passthrough).
# Fields not in this set are stripped before forwarding.
# Reference: https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-anthropic-claude-messages.html
_ALLOWED_BODY_FIELDS = {
    "anthropic_version",
    "messages",
    "max_tokens",
    "system",
    "temperature",
    "top_p",
    "top_k",
    "stop_sequences",
    "tools",
    "tool_choice",
    "thinking",
    "metadata",
}


@dataclass
class ProxyUsage:
    """Accumulated token usage tracked by the proxy."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    requests: int = 0
    models_used: dict[str, int] = field(default_factory=dict)  # model -> request count
    total_cost: float = 0.0

    def snapshot(self) -> "ProxyUsage":
        """Return an immutable point-in-time copy of the current usage.

        The proxy mutates this object in place from inside aiohttp request
        handlers, so callers that want a stable "before" marker for a
        snapshot/diff comparison MUST snapshot — never hold a reference to
        the live instance.

        Used by :func:`usage_between` to attribute a slice of total proxy
        traffic to a single consumer (main-agent turn, sub-agent judge,
        llm_judge call, simulator utterance, ...).
        """
        return ProxyUsage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens,
            requests=self.requests,
            models_used=dict(self.models_used),
            total_cost=self.total_cost,
        )


def usage_between(before: ProxyUsage, after: ProxyUsage) -> TokenUsage:
    """Return the proxy traffic that landed between two snapshots as a TokenUsage.

    Use to attribute a slice of total proxy usage to a single consumer.
    Typical pattern::

        pre = proxy.usage.snapshot()
        ... do work that may send requests through the proxy ...
        delta = usage_between(pre, proxy.usage)

    Cost diff is surfaced only when positive — a negative delta would
    indicate a bug or an out-of-order snapshot pair and should not silently
    appear as a credit on the consumer's bill (we'd rather see the issue
    via a follow-up audit than misattribute a refund). The token deltas
    are returned verbatim; with monotonic accumulation they're already
    non-negative.
    """
    cost_diff = after.total_cost - before.total_cost
    return TokenUsage(
        uncached_input_tokens=after.input_tokens - before.input_tokens,
        output_tokens=after.output_tokens - before.output_tokens,
        cache_creation_input_tokens=(after.cache_creation_input_tokens - before.cache_creation_input_tokens),
        cache_read_input_tokens=(after.cache_read_input_tokens - before.cache_read_input_tokens),
        total_cost_usd=cost_diff if cost_diff > 0 else None,
    )


@contextmanager
def measure_proxy(proxy: "LLMGatewayProxy | None") -> Iterator[Callable[[], TokenUsage | None]]:
    """Attribute the proxy traffic inside this block to one consumer.

    Yields a getter returning the attributed TokenUsage (None when the proxy is
    absent or the window carried no traffic). CORRECT ONLY because work on a
    given proxy is serialized — one agent turn or one judge call at a time;
    cross-task isolation is guaranteed by each Orchestrator owning its own
    per-task proxy (orchestrator.py:743,783). This docstring is the single home
    for that non-overlapping-window invariant.

    The getter re-reads live usage on each call and applies the
    ``is_empty() -> None`` drop, so no caller re-implements it; it returns the
    delta even when the measured block raised (the ``finally``-equivalent
    semantics of the prior hand-rolled snapshot/diff sites).
    """
    if proxy is None:
        yield lambda: None
        return
    before = proxy.usage.snapshot()

    def delta() -> TokenUsage | None:
        d = usage_between(before, proxy.usage)
        return None if d.is_empty() else d

    yield delta


class LLMGatewayProxy:
    """HTTP proxy that intercepts Anthropic API calls and routes them through LLM Gateway.

    Handles both streaming (SSE) and non-streaming requests.
    Manages its own lifecycle (start/stop) and OAuth token refresh on 401.
    Tracks token usage from gateway responses for cost calculation.
    """

    def __init__(self, config: ProxyConfig):
        self._config = config
        self._token_manager = TokenManager(config)
        if config.task_id:
            self._logger: logging.Logger | logging.LoggerAdapter[logging.Logger] = logging.LoggerAdapter(
                logger, extra={"task_id": config.task_id}
            )
        else:
            self._logger = logger
        self._app = web.Application()
        self._app.router.add_post("/v1/messages", self._handle_messages)
        self._app.router.add_post("/v1/messages/count_tokens", self._handle_count_tokens)
        self._app.router.add_get("/health", self._handle_health)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._port: int | None = None
        self._usage = ProxyUsage()

    @property
    def port(self) -> int | None:
        """The port the proxy is listening on, or None if not started."""
        return self._port

    @property
    def usage(self) -> ProxyUsage:
        """Accumulated token usage from all proxied requests."""
        return self._usage

    async def start(self, port: int = 0) -> int:
        """Start the proxy server.

        Args:
            port: Port to bind to. Use 0 for dynamic assignment.

        Returns:
            The actual port the server is listening on.
        """
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", port)
        await self._site.start()

        # Extract the actual bound port
        assert self._site._server is not None  # pyright: ignore[reportAttributeAccessIssue]
        sockets = self._site._server.sockets  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue]
        assert sockets
        port_num: int = sockets[0].getsockname()[1]
        self._port = port_num

        self._logger.info("LLM Gateway proxy started on http://127.0.0.1:%d", port_num)
        return port_num

    async def stop(self) -> None:
        """Stop the proxy server and clean up resources."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            u = self._usage
            self._logger.info(
                "LLM Gateway proxy stopped (total: %d requests, %d input + %d output tokens)",
                u.requests,
                u.input_tokens,
                u.output_tokens,
            )

    def _map_model(self, model: str) -> str:
        """Map a CLI model name to the LLM Gateway model name.

        Checks config overrides first, then the default map, then passes through as-is.
        """
        if model in self._config.model_map:
            return self._config.model_map[model]
        if model in DEFAULT_MODEL_MAP:
            return DEFAULT_MODEL_MAP[model]
        # Pass through as-is (may already be a gateway model name)
        self._logger.warning("No model mapping for '%s', passing through as-is", model)
        return model

    def _build_target_url(self, model: str) -> str:
        """Build the LLM Gateway passthrough URL for the given model."""
        base_url = self._config.llmgw_url.rstrip("/")
        gateway_model = self._map_model(model)
        return (
            f"{base_url}/{self._config.org_id}/{self._config.tenant_id}"
            f"/llmgateway_/api/raw/vendor/{self._config.vendor}/model/{gateway_model}/completions"
        )

    def _build_headers(self, token: str, is_streaming: bool) -> dict[str, str]:
        """Build headers for the upstream LLM Gateway request."""
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-UiPath-LlmGateway-RequestingProduct": self._config.requesting_product,
            "X-UiPath-LlmGateway-RequestingFeature": self._config.requesting_feature,
            "X-UiPath-LlmGateway-UserId": self._config.user_id,
            "X-UiPath-LlmGateway-TimeoutSeconds": str(self._config.timeout_seconds),
            "X-UiPath-LLMGateway-AllowFull4xxResponse": "true",
            "X-UiPath-LlmGateway-ApiFlavor": self._config.api_flavor,
            "X-UiPath-Streaming-Enabled": str(is_streaming).lower(),
        }

    async def _handle_retryable_status(
        self,
        status_code: int,
        response_headers: httpx.Headers,
        attempt: int,
    ) -> bool:
        """Check if a response status is retryable and, if so, sleep before the next attempt.

        Uses the ``retry-after`` header when the server provides one, otherwise
        delegates to ``errors.retry.get_retry_delay`` for exponential backoff
        with jitter (reusing the project-wide retry infrastructure).

        Returns True if the caller should retry, False if retries are exhausted.
        """
        if status_code not in _RETRYABLE_STATUS_CODES or attempt >= _RETRY_CFG.max_retries:
            return False

        # Prefer server-supplied delay; fall back to project-wide backoff
        retry_after = response_headers.get("retry-after")
        if retry_after is not None:
            try:
                delay = min(float(retry_after), _MAX_BACKOFF_S)
            except ValueError:
                delay = None  # Non-numeric (HTTP-date) — use default
        else:
            delay = None

        if delay is None:
            delay = min(compute_backoff(_RETRY_CFG, attempt), _MAX_BACKOFF_S)

        self._logger.warning(
            "Got %d from gateway, retrying in %.1fs (attempt %d/%d)",
            status_code,
            delay,
            attempt + 1,
            _RETRY_CFG.max_retries,
        )
        await self._retry_sleep(delay)
        return True

    @staticmethod
    async def _retry_sleep(delay: float) -> None:
        """Sleep before a retry. Extracted for testability."""
        await asyncio.sleep(delay)

    def _track_usage(self, model: str, usage: dict[str, Any]) -> None:
        """Accumulate token usage from a response."""
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        cache_write = usage.get("cache_creation_input_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)

        self._usage.input_tokens += in_tok
        self._usage.output_tokens += out_tok
        self._usage.cache_creation_input_tokens += cache_write
        self._usage.cache_read_input_tokens += cache_read
        self._usage.requests += 1
        self._usage.models_used[model] = self._usage.models_used.get(model, 0) + 1

        cost = calculate_cost(model, in_tok, out_tok, cache_write, cache_read)
        if cost is not None:
            self._usage.total_cost += cost

    def _extract_usage_from_sse(self, raw_bytes: bytes, model: str, *, content_type: str | None = None) -> None:
        """Parse streaming response bytes to extract usage from message_start and message_delta events.

        Handles two formats:
        - Standard SSE (text/event-stream): lines like `data: {"type": "message_start", ...}`
        - AWS Bedrock event stream (application/vnd.amazon.eventstream): binary frames
          containing `{"bytes": "<base64>"}` where the base64 decodes to the event JSON
        """
        events = self._parse_stream_events(raw_bytes, content_type=content_type)
        usage: dict[str, int] = {}

        for data in events:
            event_type = data.get("type", "")

            # message_start contains input_tokens in message.usage
            if event_type == "message_start":
                msg_usage = data.get("message", {}).get("usage", {})
                if msg_usage:
                    usage["input_tokens"] = usage.get("input_tokens", 0) + msg_usage.get("input_tokens", 0)
                    usage["cache_creation_input_tokens"] = usage.get("cache_creation_input_tokens", 0) + msg_usage.get(
                        "cache_creation_input_tokens", 0
                    )
                    usage["cache_read_input_tokens"] = usage.get("cache_read_input_tokens", 0) + msg_usage.get(
                        "cache_read_input_tokens", 0
                    )

            # message_delta carries CUMULATIVE output_tokens — each subsequent
            # event reports the updated running total for the message, not a
            # delta. See https://docs.anthropic.com/en/api/messages-streaming
            # ("message_delta ... Usage values are cumulative.").
            # Take the max instead of summing, which would over-count by ~Nx
            # for an N-event stream and inflate Bedrock-proxied usage reports.
            elif event_type == "message_delta":
                delta_usage = data.get("usage", {})
                if delta_usage and "output_tokens" in delta_usage:
                    usage["output_tokens"] = max(
                        usage.get("output_tokens", 0),
                        delta_usage["output_tokens"],
                    )

        if usage:
            self._track_usage(model, usage)

    @staticmethod
    def _parse_stream_events(raw_bytes: bytes, *, content_type: str | None = None) -> list[dict[str, Any]]:
        """Extract JSON event objects from either SSE or Bedrock event stream format.

        Uses Content-Type header when available, falls back to a byte heuristic.
        Returns a list of parsed JSON dicts (one per event).
        """
        # Prefer Content-Type for format detection
        if content_type:
            ct_lower = content_type.lower()
            if "application/vnd.amazon.eventstream" in ct_lower:
                return LLMGatewayProxy._parse_bedrock_event_stream(raw_bytes)
            if "text/event-stream" in ct_lower:
                return LLMGatewayProxy._parse_sse_events(raw_bytes.decode("utf-8", errors="replace"))

        # Fallback heuristic: binary data starts with null byte
        if raw_bytes and raw_bytes[0] == 0:
            return LLMGatewayProxy._parse_bedrock_event_stream(raw_bytes)
        return LLMGatewayProxy._parse_sse_events(raw_bytes.decode("utf-8", errors="replace"))

    @staticmethod
    def _parse_sse_events(text: str) -> list[dict[str, Any]]:
        """Parse standard SSE text/event-stream format."""
        events: list[dict[str, Any]] = []
        for line in text.split("\n"):
            line = line.strip()
            if not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str == "[DONE]":
                continue
            try:
                events.append(json.loads(data_str))
            except json.JSONDecodeError:
                continue
        return events

    @staticmethod
    def _parse_bedrock_event_stream(raw_bytes: bytes) -> list[dict[str, Any]]:
        """Parse AWS Bedrock event stream format.

        Bedrock wraps each event as `{"bytes": "<base64-encoded-json>"}`.
        We extract the base64 values directly from raw bytes to avoid
        corruption from UTF-8 decoding of binary headers.
        """
        events: list[dict[str, Any]] = []
        for match in re.finditer(rb'\{"bytes":"([A-Za-z0-9+/=]+)"', raw_bytes):
            b64_bytes = match.group(1)
            try:
                decoded = base64.b64decode(b64_bytes)
                events.append(json.loads(decoded))
            except (ValueError, json.JSONDecodeError):
                continue
        return events

    @staticmethod
    def _upstream_error_response(exc: httpx.RequestError, status: int) -> web.Response:
        """Build a JSON error response for upstream transport failures."""
        error_type = "timeout_error" if isinstance(exc, httpx.TimeoutException) else "connection_error"
        return web.json_response(
            {"type": "error", "error": {"type": error_type, "message": f"Upstream request failed: {exc}"}},
            status=status,
        )

    async def _handle_messages(self, request: web.Request) -> web.StreamResponse:
        """Handle POST /v1/messages — main completions endpoint."""
        body = await request.read()

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        if not isinstance(payload, dict):
            return web.json_response({"error": "JSON body must be an object"}, status=400)

        model = payload.get("model", "")
        if not isinstance(model, str) or not model:
            return web.json_response({"error": "Missing 'model' in request body"}, status=400)
        if not re.match(r"^[a-zA-Z0-9._:-]+$", model):
            return web.json_response({"error": "Invalid 'model' format"}, status=400)

        is_streaming = payload.get("stream", False)
        gateway_model = self._map_model(model)
        target_url = self._build_target_url(model)
        mode = "streaming" if is_streaming else "non-streaming"
        self._logger.debug("Proxying %s request for model=%s (gateway: %s)", mode, model, gateway_model)

        # Strip fields and inject version only for AWS Bedrock vendor
        if self._config.vendor == "awsbedrock":
            stripped = [k for k in list(payload) if k not in _ALLOWED_BODY_FIELDS]
            for key in stripped:
                del payload[key]
            if stripped:
                self._logger.debug("Stripped non-Bedrock fields from request body: %s", stripped)

            # Bedrock requires this specific anthropic_version
            payload["anthropic_version"] = "bedrock-2023-05-31"

        body = json.dumps(payload).encode()

        token = await self._token_manager.get_token()
        headers = self._build_headers(token, is_streaming)

        if is_streaming:
            return await self._forward_streaming(request, target_url, headers, body, model=model)
        else:
            return await self._forward_sync(target_url, headers, body, model=model)

    async def _handle_count_tokens(self, request: web.Request) -> web.Response:
        """Handle POST /v1/messages/count_tokens — token counting endpoint.

        For v1, returns a synthetic response since the gateway may not support this.
        """
        body = await request.read()
        try:
            json.loads(body)
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        self._logger.debug("count_tokens request received — returning synthetic response")
        return web.json_response({"input_tokens": 0})

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response({"status": "ok"})

    async def _forward_streaming(
        self,
        request: web.Request,
        target_url: str,
        headers: dict[str, str],
        body: bytes,
        *,
        model: str = "",
    ) -> web.StreamResponse:
        """Forward a streaming request and pass SSE chunks back transparently."""
        token_refreshed = False
        last_status = 0
        response: web.StreamResponse | None = None

        try:
            async with httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT) as client:
                for attempt in range(_RETRY_CFG.max_retries + 1):
                    async with client.stream("POST", target_url, headers=headers, content=body) as upstream:
                        last_status = upstream.status_code

                        # 401 — refresh token once then retry
                        if upstream.status_code == 401 and not token_refreshed:
                            self._logger.info("Got 401 from gateway, refreshing token and retrying")
                            await upstream.aread()
                            token = await self._token_manager.refresh_token()
                            headers = self._build_headers(token, is_streaming=True)
                            token_refreshed = True
                            continue

                        if upstream.status_code == 401:
                            await upstream.aread()
                            return web.json_response(
                                {"error": "Authentication failed after token refresh"},
                                status=401,
                            )

                        # 429 / 529 — backoff and retry
                        if await self._handle_retryable_status(upstream.status_code, upstream.headers, attempt):
                            await upstream.aread()
                            continue

                        response = web.StreamResponse(
                            status=upstream.status_code,
                            headers={
                                "Content-Type": upstream.headers.get("content-type", "text/event-stream"),
                                "Cache-Control": "no-cache",
                            },
                        )
                        try:
                            await response.prepare(request)

                            # Buffer SSE bytes for usage extraction while forwarding.
                            # Cap at _MAX_SSE_BUFFER_BYTES to avoid unbounded memory growth.
                            sse_buffer = bytearray()
                            track_usage = True
                            async for chunk in upstream.aiter_bytes():
                                await response.write(chunk)
                                if track_usage:
                                    if len(sse_buffer) + len(chunk) > _MAX_SSE_BUFFER_BYTES:
                                        track_usage = False
                                        sse_buffer.clear()
                                        self._logger.warning(
                                            "SSE response exceeded %d bytes; usage tracking disabled for this response",
                                            _MAX_SSE_BUFFER_BYTES,
                                        )
                                    else:
                                        sse_buffer.extend(chunk)

                            # Extract usage from buffered SSE events
                            if track_usage and sse_buffer and upstream.status_code == 200:
                                self._extract_usage_from_sse(
                                    bytes(sse_buffer), model, content_type=upstream.headers.get("content-type")
                                )

                            await response.write_eof()
                        except (ClientConnectionResetError, ConnectionResetError):
                            self._logger.warning("Client disconnected during streaming — usage tracking skipped")

                        return response

            # Should not reach here, but satisfy type checker
            return web.json_response(
                {"error": f"Retry limit exceeded (last status: {last_status})"}, status=last_status or 500
            )
        except httpx.RequestError as exc:
            status = 504 if isinstance(exc, httpx.TimeoutException) else 502
            self._logger.warning("Upstream streaming request failed (%d): %s", status, exc)
            if response is not None and response.prepared:
                # Headers already sent — cannot send a new response; close the stream
                with contextlib.suppress(ConnectionError, OSError):
                    await response.write_eof()
                return response
            return self._upstream_error_response(exc, status)

    async def _forward_sync(
        self,
        target_url: str,
        headers: dict[str, str],
        body: bytes,
        *,
        model: str = "",
    ) -> web.Response:
        """Forward a non-streaming request and return the full response."""
        token_refreshed = False
        upstream: httpx.Response | None = None

        try:
            async with httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT) as client:
                for attempt in range(_RETRY_CFG.max_retries + 1):
                    upstream = await client.post(target_url, headers=headers, content=body)

                    # 401 — refresh token once then retry
                    if upstream.status_code == 401 and not token_refreshed:
                        self._logger.info("Got 401 from gateway, refreshing token and retrying")
                        token = await self._token_manager.refresh_token()
                        headers = self._build_headers(token, is_streaming=False)
                        token_refreshed = True
                        continue

                    # 429 / 529 — backoff and retry
                    if await self._handle_retryable_status(upstream.status_code, upstream.headers, attempt):
                        continue

                    break  # Success or non-retryable error

                if upstream is None:
                    self._logger.warning("No upstream response after retries (loop body never executed)")
                    return web.Response(status=502, text="No upstream response after retries")

                # Extract usage from non-streaming response
                if upstream.status_code == 200:
                    try:
                        resp_data = upstream.json()
                        usage = resp_data.get("usage", {})
                        if usage:
                            self._track_usage(model, usage)
                    except (json.JSONDecodeError, ValueError):
                        self._logger.debug("Failed to parse usage from non-streaming response")

                return web.Response(
                    status=upstream.status_code,
                    body=upstream.content,
                    content_type=upstream.headers.get("content-type", "application/json"),
                )
        except httpx.RequestError as exc:
            status = 504 if isinstance(exc, httpx.TimeoutException) else 502
            self._logger.warning("Upstream request failed (%d): %s", status, exc)
            return self._upstream_error_response(exc, status)

    def usage_total(self) -> TokenUsage:
        """Live accumulator as a TokenUsage (ground truth for reconciliation).

        Unlike :func:`usage_between` (a delta between two snapshots), this is
        the proxy's cumulative total across every request it has handled —
        the independent counter the orchestrator reconciles attributed
        per-consumer usage against (see ``_reconcile_proxy_usage``).
        """
        u = self._usage
        return TokenUsage(
            uncached_input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_creation_input_tokens=u.cache_creation_input_tokens,
            cache_read_input_tokens=u.cache_read_input_tokens,
            total_cost_usd=u.total_cost,
        )

    def get_total_cost(self) -> float | None:
        """Calculate total cost from accumulated usage using official Anthropic pricing.

        Returns None if no requests have been tracked.
        """
        if not self._usage.models_used:
            return None
        return self._usage.total_cost
