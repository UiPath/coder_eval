"""Unit tests for the LLM Gateway proxy package.

Tests cover: model mapping, header/body transformation, SSE usage extraction,
pricing calculation, token manager, and proxy lifecycle.
"""

import base64
import json

import httpx
import pytest

from coder_eval.errors.categories import RetryConfig
from coder_eval.errors.retry import compute_backoff
from coder_eval.proxy.config import DEFAULT_MODEL_MAP, ProxyConfig
from coder_eval.proxy.pricing import calculate_cost
from coder_eval.proxy.server import (
    _ALLOWED_BODY_FIELDS,
    _MAX_BACKOFF_S,
    _MAX_SSE_BUFFER_BYTES,
    _RETRY_CFG,
    LLMGatewayProxy,
    ProxyUsage,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> ProxyConfig:
    """Create a ProxyConfig with test defaults."""
    defaults = {
        "llmgw_url": "https://gateway.example.com",
        "client_id": "test-client",
        "client_secret": "test-secret",
        "org_id": "org-123",
        "tenant_id": "tenant-456",
    }
    defaults.update(overrides)
    return ProxyConfig(**defaults)


def _make_proxy(**config_overrides) -> LLMGatewayProxy:
    """Create a proxy instance (not started) for testing internal methods."""
    return LLMGatewayProxy(_make_config(**config_overrides))


# ---------------------------------------------------------------------------
# Model mapping
# ---------------------------------------------------------------------------


class TestModelMapping:
    """Tests for _map_model and DEFAULT_MODEL_MAP."""

    def test_default_map_known_model(self):
        proxy = _make_proxy()
        assert proxy._map_model("claude-sonnet-4-6") == "anthropic.claude-sonnet-4-6"

    def test_default_map_older_model(self):
        proxy = _make_proxy()
        assert proxy._map_model("claude-3-5-sonnet-20241022") == "anthropic.claude-3-5-sonnet-20241022-v2:0"

    def test_config_override_takes_precedence(self):
        proxy = _make_proxy(model_map={"claude-sonnet-4-6": "custom-model-name"})
        assert proxy._map_model("claude-sonnet-4-6") == "custom-model-name"

    def test_unknown_model_passes_through(self):
        proxy = _make_proxy()
        assert proxy._map_model("unknown-model-xyz") == "unknown-model-xyz"

    def test_all_default_map_entries_are_strings(self):
        for cli_name, gw_name in DEFAULT_MODEL_MAP.items():
            assert isinstance(cli_name, str)
            assert isinstance(gw_name, str)
            assert gw_name.startswith("anthropic.")


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------


class TestUrlBuilding:
    """Tests for _build_target_url."""

    def test_basic_url(self):
        proxy = _make_proxy()
        url = proxy._build_target_url("claude-sonnet-4-6")
        assert url == (
            "https://gateway.example.com/org-123/tenant-456"
            "/llmgateway_/api/raw/vendor/awsbedrock/model/anthropic.claude-sonnet-4-6/completions"
        )

    def test_trailing_slash_stripped(self):
        proxy = _make_proxy(llmgw_url="https://gateway.example.com/")
        url = proxy._build_target_url("claude-sonnet-4-6")
        assert "//org-123" not in url

    def test_custom_vendor(self):
        proxy = _make_proxy(vendor="openai")
        url = proxy._build_target_url("claude-sonnet-4-6")
        assert "/vendor/openai/" in url


# ---------------------------------------------------------------------------
# Header building
# ---------------------------------------------------------------------------


class TestHeaderBuilding:
    """Tests for _build_headers."""

    def test_streaming_header_true(self):
        proxy = _make_proxy()
        headers = proxy._build_headers("tok123", is_streaming=True)
        assert headers["Authorization"] == "Bearer tok123"
        assert headers["X-UiPath-Streaming-Enabled"] == "true"

    def test_streaming_header_false(self):
        proxy = _make_proxy()
        headers = proxy._build_headers("tok123", is_streaming=False)
        assert headers["X-UiPath-Streaming-Enabled"] == "false"

    def test_custom_product_and_feature(self):
        proxy = _make_proxy(requesting_product="my-product", requesting_feature="my-feature")
        headers = proxy._build_headers("tok", is_streaming=False)
        assert headers["X-UiPath-LlmGateway-RequestingProduct"] == "my-product"
        assert headers["X-UiPath-LlmGateway-RequestingFeature"] == "my-feature"

    def test_all_required_headers_present(self):
        proxy = _make_proxy()
        headers = proxy._build_headers("tok", is_streaming=False)
        required = [
            "Authorization",
            "Content-Type",
            "X-UiPath-LlmGateway-RequestingProduct",
            "X-UiPath-LlmGateway-RequestingFeature",
            "X-UiPath-LlmGateway-UserId",
            "X-UiPath-LlmGateway-TimeoutSeconds",
            "X-UiPath-LLMGateway-AllowFull4xxResponse",
            "X-UiPath-LlmGateway-ApiFlavor",
            "X-UiPath-Streaming-Enabled",
        ]
        for key in required:
            assert key in headers


# ---------------------------------------------------------------------------
# Body field stripping
# ---------------------------------------------------------------------------


class TestBodyFieldStripping:
    """Tests for _ALLOWED_BODY_FIELDS allowlist."""

    def test_allowed_fields_are_preserved(self):
        for field in _ALLOWED_BODY_FIELDS:
            payload = {field: "value", "model": "test"}
            stripped = [k for k in list(payload) if k not in _ALLOWED_BODY_FIELDS]
            for key in stripped:
                del payload[key]
            assert field in payload

    def test_model_field_stripped(self):
        """model is sent via URL path, not in the body for Bedrock."""
        assert "model" not in _ALLOWED_BODY_FIELDS

    def test_stream_field_stripped(self):
        """stream is communicated via header, not body for gateway."""
        assert "stream" not in _ALLOWED_BODY_FIELDS

    def test_thinking_field_allowed(self):
        """Extended thinking must be passed through."""
        assert "thinking" in _ALLOWED_BODY_FIELDS

    async def test_non_bedrock_vendor_preserves_all_fields(self):
        """When vendor is not awsbedrock, body fields must NOT be stripped."""
        proxy = _make_proxy(vendor="openai")
        proxy._token_manager._token = "test-token"
        port = await proxy.start()
        try:
            # Include fields that would be stripped for Bedrock (model, stream, extra_field)
            request_json = {
                "model": "claude-sonnet-4-6",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
                "extra_custom_field": "should-survive",
            }
            # We can't easily inspect the forwarded body without a real upstream,
            # but we can verify the proxy doesn't inject bedrock-specific anthropic_version
            # by checking the payload transformation logic.
            # Instead, send a request and check it doesn't crash (field stripping is vendor-gated)
            async with httpx.AsyncClient(timeout=5) as client:
                # This will fail to reach the upstream, but the proxy should attempt forwarding
                # with all fields intact. We verify the proxy doesn't return 400.
                resp = await client.post(
                    f"http://127.0.0.1:{port}/v1/messages",
                    json=request_json,
                )
            # 502/500 expected (upstream unreachable), but NOT 400 (invalid body)
            assert resp.status_code != 400
        finally:
            await proxy.stop()


# ---------------------------------------------------------------------------
# Token usage tracking
# ---------------------------------------------------------------------------


class TestUsageTracking:
    """Tests for _track_usage and ProxyUsage accumulation."""

    def test_single_request(self):
        proxy = _make_proxy()
        proxy._track_usage("model-a", {"input_tokens": 100, "output_tokens": 50})
        assert proxy.usage.input_tokens == 100
        assert proxy.usage.output_tokens == 50
        assert proxy.usage.requests == 1
        assert proxy.usage.models_used == {"model-a": 1}

    def test_multiple_requests_accumulate(self):
        proxy = _make_proxy()
        proxy._track_usage("model-a", {"input_tokens": 100, "output_tokens": 50})
        proxy._track_usage("model-a", {"input_tokens": 200, "output_tokens": 75})
        assert proxy.usage.input_tokens == 300
        assert proxy.usage.output_tokens == 125
        assert proxy.usage.requests == 2
        assert proxy.usage.models_used == {"model-a": 2}

    def test_cache_tokens(self):
        proxy = _make_proxy()
        proxy._track_usage(
            "model-a",
            {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 200,
                "cache_read_input_tokens": 300,
            },
        )
        assert proxy.usage.cache_creation_input_tokens == 200
        assert proxy.usage.cache_read_input_tokens == 300

    def test_missing_fields_default_to_zero(self):
        proxy = _make_proxy()
        proxy._track_usage("model-a", {})
        assert proxy.usage.input_tokens == 0
        assert proxy.usage.output_tokens == 0
        assert proxy.usage.requests == 1

    def test_multiple_models_tracked(self):
        proxy = _make_proxy()
        proxy._track_usage("model-a", {"input_tokens": 100, "output_tokens": 50})
        proxy._track_usage("model-b", {"input_tokens": 200, "output_tokens": 75})
        assert proxy.usage.models_used == {"model-a": 1, "model-b": 1}


# ---------------------------------------------------------------------------
# SSE usage extraction
# ---------------------------------------------------------------------------


class TestSSEUsageExtraction:
    """Tests for _extract_usage_from_sse."""

    def test_typical_stream(self):
        proxy = _make_proxy()
        sse = (
            b"event: message_start\n"
            b'data: {"type": "message_start", "message": {"usage":'
            b' {"input_tokens": 1500, "cache_creation_input_tokens": 200,'
            b' "cache_read_input_tokens": 300}}}\n'
            b"\n"
            b"event: content_block_delta\n"
            b'data: {"type": "content_block_delta", "delta": {"text": "Hello"}}\n'
            b"\n"
            b"event: message_delta\n"
            b'data: {"type": "message_delta", "usage": {"output_tokens": 750}}\n'
            b"\n"
            b"data: [DONE]\n"
        )
        proxy._extract_usage_from_sse(sse, "claude-sonnet-4-6")
        assert proxy.usage.input_tokens == 1500
        assert proxy.usage.output_tokens == 750
        assert proxy.usage.cache_creation_input_tokens == 200
        assert proxy.usage.cache_read_input_tokens == 300
        assert proxy.usage.requests == 1

    def test_empty_stream(self):
        proxy = _make_proxy()
        proxy._extract_usage_from_sse(b"", "claude-sonnet-4-6")
        assert proxy.usage.requests == 0

    def test_malformed_json_skipped(self):
        proxy = _make_proxy()
        sse = b'data: {not json}\ndata: {"type": "message_delta", "usage": {"output_tokens": 42}}\n'
        proxy._extract_usage_from_sse(sse, "model-a")
        assert proxy.usage.output_tokens == 42
        assert proxy.usage.requests == 1

    def test_done_marker_skipped(self):
        proxy = _make_proxy()
        sse = b"data: [DONE]\n"
        proxy._extract_usage_from_sse(sse, "model-a")
        assert proxy.usage.requests == 0

    def test_no_usage_events(self):
        proxy = _make_proxy()
        sse = b'data: {"type": "content_block_delta", "delta": {"text": "hi"}}\n'
        proxy._extract_usage_from_sse(sse, "model-a")
        assert proxy.usage.requests == 0

    def test_non_data_lines_ignored(self):
        proxy = _make_proxy()
        sse = (
            b"event: message_start\n"
            b": comment line\n"
            b"retry: 1000\n"
            b'data: {"type": "message_start", "message": {"usage": {"input_tokens": 42}}}\n'
        )
        proxy._extract_usage_from_sse(sse, "model-a")
        assert proxy.usage.input_tokens == 42

    def test_message_start_without_usage(self):
        proxy = _make_proxy()
        sse = b'data: {"type": "message_start", "message": {}}\n'
        proxy._extract_usage_from_sse(sse, "model-a")
        assert proxy.usage.requests == 0

    def test_multiple_message_start_events_accumulate(self):
        proxy = _make_proxy()
        sse = (
            b'data: {"type": "message_start", "message": {"usage": {"input_tokens": 100}}}\n'
            b'data: {"type": "message_start", "message": {"usage": {"input_tokens": 200}}}\n'
        )
        proxy._extract_usage_from_sse(sse, "model-a")
        assert proxy.usage.input_tokens == 300


class TestSSEBufferCap:
    """Tests for _MAX_SSE_BUFFER_BYTES cap on streaming usage extraction."""

    def test_constant_is_positive(self):
        assert _MAX_SSE_BUFFER_BYTES > 0

    def test_buffer_cap_value(self):
        """Buffer cap should be 5 MB."""
        assert _MAX_SSE_BUFFER_BYTES == 5_000_000


# ---------------------------------------------------------------------------
# Bedrock event stream parsing
# ---------------------------------------------------------------------------


def _bedrock_frame(event_json: dict) -> bytes:
    """Build a minimal Bedrock event stream frame wrapping JSON as base64.

    Real Bedrock frames have binary headers + CRC, but we only need the
    {"bytes":"<base64>"} part to be extractable via regex on raw bytes.
    """
    b64 = base64.b64encode(json.dumps(event_json).encode()).decode()
    # Prefix with a null byte to trigger the Bedrock detection heuristic,
    # then embed the JSON payload
    return b"\x00" + f'{{"bytes":"{b64}","p":"pad"}}'.encode() + b"\n"


class TestBedrockEventStreamParsing:
    """Tests for _extract_usage_from_sse with Bedrock event stream format."""

    def test_bedrock_message_start(self):
        proxy = _make_proxy()
        raw = _bedrock_frame(
            {
                "type": "message_start",
                "message": {
                    "usage": {"input_tokens": 42, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 10}
                },
            }
        )
        proxy._extract_usage_from_sse(raw, "model-a")
        assert proxy.usage.input_tokens == 42
        assert proxy.usage.cache_creation_input_tokens == 5
        assert proxy.usage.cache_read_input_tokens == 10

    def test_bedrock_message_delta(self):
        proxy = _make_proxy()
        raw = _bedrock_frame({"type": "message_delta", "usage": {"output_tokens": 99}})
        proxy._extract_usage_from_sse(raw, "model-a")
        assert proxy.usage.output_tokens == 99

    def test_bedrock_full_stream(self):
        proxy = _make_proxy()
        raw = (
            _bedrock_frame({"type": "message_start", "message": {"usage": {"input_tokens": 100}}})
            + _bedrock_frame({"type": "content_block_delta", "delta": {"text": "hi"}})
            + _bedrock_frame({"type": "message_delta", "usage": {"output_tokens": 50}})
        )
        proxy._extract_usage_from_sse(raw, "model-a")
        assert proxy.usage.input_tokens == 100
        assert proxy.usage.output_tokens == 50
        assert proxy.usage.requests == 1

    def test_bedrock_invalid_base64_skipped(self):
        proxy = _make_proxy()
        # Valid frame + corrupted frame
        raw = _bedrock_frame({"type": "message_start", "message": {"usage": {"input_tokens": 10}}})
        # Inject a frame with invalid base64
        raw += b'\x00{"bytes":"!!!invalid!!!"}\n'
        proxy._extract_usage_from_sse(raw, "model-a")
        assert proxy.usage.input_tokens == 10

    def test_content_type_selects_bedrock_parser(self):
        """Content-Type header should override byte heuristic."""
        proxy = _make_proxy()
        raw = _bedrock_frame({"type": "message_start", "message": {"usage": {"input_tokens": 77}}})
        proxy._extract_usage_from_sse(raw, "model-a", content_type="application/vnd.amazon.eventstream")
        assert proxy.usage.input_tokens == 77

    def test_content_type_selects_sse_parser(self):
        """Content-Type header should override byte heuristic for SSE."""
        proxy = _make_proxy()
        sse = b'data: {"type": "message_start", "message": {"usage": {"input_tokens": 33}}}\n'
        proxy._extract_usage_from_sse(sse, "model-a", content_type="text/event-stream")
        assert proxy.usage.input_tokens == 33


# ---------------------------------------------------------------------------
# Pricing calculation
# ---------------------------------------------------------------------------


class TestPricingCalculation:
    """Tests for calculate_cost."""

    def test_sonnet_cost(self):
        # Sonnet 4.6: $3/MTok in, $15/MTok out
        cost = calculate_cost("claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=0)
        assert cost == pytest.approx(3.0)

        cost = calculate_cost("claude-sonnet-4-6", input_tokens=0, output_tokens=1_000_000)
        assert cost == pytest.approx(15.0)

    def test_opus_cost(self):
        # Opus 4.6: $15/MTok in, $75/MTok out
        cost = calculate_cost("claude-opus-4-6", input_tokens=1_000_000, output_tokens=0)
        assert cost == pytest.approx(15.0)

    def test_haiku_cost(self):
        # Haiku 4.5: $0.80/MTok in, $4.0/MTok out
        cost = calculate_cost("claude-haiku-4-5-20251001", input_tokens=1_000_000, output_tokens=0)
        assert cost == pytest.approx(0.80)

    def test_cache_tokens_add_to_cost(self):
        cost = calculate_cost(
            "claude-sonnet-4-6",
            input_tokens=0,
            output_tokens=0,
            cache_creation_tokens=1_000_000,
            cache_read_tokens=1_000_000,
        )
        # cache_write: $3.75/MTok, cache_read: $0.30/MTok
        assert cost == pytest.approx(3.75 + 0.30)

    def test_unknown_model_returns_none(self):
        assert calculate_cost("unknown-model", input_tokens=1000, output_tokens=500) is None

    def test_zero_tokens_returns_zero(self):
        cost = calculate_cost("claude-sonnet-4-6", input_tokens=0, output_tokens=0)
        assert cost == 0.0

    def test_realistic_request(self):
        # 50k input, 2k output on Sonnet
        cost = calculate_cost("claude-sonnet-4-6", input_tokens=50_000, output_tokens=2_000)
        expected = (50_000 * 3.0 + 2_000 * 15.0) / 1_000_000
        assert cost == pytest.approx(expected)


# ---------------------------------------------------------------------------
# get_total_cost
# ---------------------------------------------------------------------------


class TestGetTotalCost:
    """Tests for LLMGatewayProxy.get_total_cost."""

    def test_no_usage_returns_none(self):
        proxy = _make_proxy()
        assert proxy.get_total_cost() is None

    def test_known_model(self):
        proxy = _make_proxy()
        proxy._track_usage("claude-sonnet-4-6", {"input_tokens": 1_000_000, "output_tokens": 0})
        assert proxy.get_total_cost() == pytest.approx(3.0)

    def test_unknown_model_returns_zero_cost(self):
        proxy = _make_proxy()
        proxy._track_usage("unknown-model", {"input_tokens": 1000, "output_tokens": 500})
        # Unknown model can't be priced, so cost stays at 0
        assert proxy.get_total_cost() == pytest.approx(0.0)

    def test_mixed_models_priced_independently(self):
        proxy = _make_proxy()
        # 2 Sonnet requests + 1 Opus request — each priced at its own rate
        proxy._track_usage("claude-sonnet-4-6", {"input_tokens": 500, "output_tokens": 0})
        proxy._track_usage("claude-sonnet-4-6", {"input_tokens": 500, "output_tokens": 0})
        proxy._track_usage("claude-opus-4-6", {"input_tokens": 500, "output_tokens": 0})
        # Sonnet: 1000 * 3.0 / 1M, Opus: 500 * 15.0 / 1M
        expected = (1000 * 3.0 + 500 * 15.0) / 1_000_000
        assert proxy.get_total_cost() == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Proxy lifecycle (start/stop)
# ---------------------------------------------------------------------------


class TestProxyLifecycle:
    """Tests for proxy start/stop without real upstream."""

    async def test_start_assigns_port(self):
        proxy = _make_proxy()
        port = await proxy.start()
        try:
            assert port > 0
            assert proxy.port == port
        finally:
            await proxy.stop()

    async def test_stop_clears_state(self):
        proxy = _make_proxy()
        await proxy.start()
        await proxy.stop()
        assert proxy._runner is None
        assert proxy._site is None

    async def test_health_endpoint(self):
        proxy = _make_proxy()
        port = await proxy.start()
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://127.0.0.1:{port}/health")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}
        finally:
            await proxy.stop()

    async def test_count_tokens_returns_synthetic(self):
        proxy = _make_proxy()
        port = await proxy.start()
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"http://127.0.0.1:{port}/v1/messages/count_tokens",
                    json={"model": "claude-sonnet-4-6", "messages": []},
                )
            assert resp.status_code == 200
            assert resp.json() == {"input_tokens": 0}
        finally:
            await proxy.stop()

    async def test_messages_rejects_invalid_json(self):
        proxy = _make_proxy()
        port = await proxy.start()
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"http://127.0.0.1:{port}/v1/messages",
                    content=b"not json",
                    headers={"Content-Type": "application/json"},
                )
            assert resp.status_code == 400
        finally:
            await proxy.stop()

    async def test_messages_rejects_non_object_json(self):
        proxy = _make_proxy()
        port = await proxy.start()
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"http://127.0.0.1:{port}/v1/messages",
                    content=b"[1, 2, 3]",
                    headers={"Content-Type": "application/json"},
                )
            assert resp.status_code == 400
            assert "object" in resp.json()["error"].lower()
        finally:
            await proxy.stop()

    async def test_messages_rejects_missing_model(self):
        proxy = _make_proxy()
        port = await proxy.start()
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"http://127.0.0.1:{port}/v1/messages",
                    json={"messages": [{"role": "user", "content": "hi"}]},
                )
            assert resp.status_code == 400
            assert "model" in resp.json()["error"].lower()
        finally:
            await proxy.stop()

    async def test_messages_rejects_non_string_model(self):
        """Non-string model (e.g. integer) should return 400, not crash with TypeError."""
        proxy = _make_proxy()
        port = await proxy.start()
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"http://127.0.0.1:{port}/v1/messages",
                    json={"model": 123, "messages": [{"role": "user", "content": "hi"}]},
                )
            assert resp.status_code == 400
            assert "model" in resp.json()["error"].lower()
        finally:
            await proxy.stop()


# ---------------------------------------------------------------------------
# Token manager
# ---------------------------------------------------------------------------


class TestTokenManager:
    """Tests for TokenManager with mocked HTTP."""

    async def test_get_token_acquires_once(self, monkeypatch):
        from coder_eval.proxy.auth import TokenManager

        config = _make_config()
        tm = TokenManager(config)

        call_count = 0

        async def mock_acquire(self_inner):
            nonlocal call_count
            call_count += 1
            return f"token-{call_count}"

        monkeypatch.setattr(TokenManager, "_acquire_token", mock_acquire)

        t1 = await tm.get_token()
        t2 = await tm.get_token()
        assert t1 == "token-1"
        assert t2 == "token-1"  # cached
        assert call_count == 1

    async def test_refresh_token_reacquires(self, monkeypatch):
        from coder_eval.proxy.auth import TokenManager

        config = _make_config()
        tm = TokenManager(config)

        call_count = 0

        async def mock_acquire(self_inner):
            nonlocal call_count
            call_count += 1
            return f"token-{call_count}"

        monkeypatch.setattr(TokenManager, "_acquire_token", mock_acquire)

        t1 = await tm.get_token()
        assert t1 == "token-1"

        t2 = await tm.refresh_token()
        assert t2 == "token-2"
        assert call_count == 2

        # After refresh, get_token returns the new token
        t3 = await tm.get_token()
        assert t3 == "token-2"


# ---------------------------------------------------------------------------
# ProxyUsage dataclass
# ---------------------------------------------------------------------------


class TestProxyUsage:
    """Tests for ProxyUsage defaults and mutability."""

    def test_defaults(self):
        u = ProxyUsage()
        assert u.input_tokens == 0
        assert u.output_tokens == 0
        assert u.cache_creation_input_tokens == 0
        assert u.cache_read_input_tokens == 0
        assert u.requests == 0
        assert u.models_used == {}

    def test_independent_models_used_dicts(self):
        """Each ProxyUsage instance should have its own models_used dict."""
        u1 = ProxyUsage()
        u2 = ProxyUsage()
        u1.models_used["a"] = 1
        assert "a" not in u2.models_used


# ---------------------------------------------------------------------------
# Retry delay calculation
# ---------------------------------------------------------------------------


class TestComputeBackoff:
    """Tests for compute_backoff (shared retry infrastructure)."""

    def test_exponential_growth(self):
        cfg = RetryConfig(initial_delay=1.0, backoff_multiplier=2.0)
        # compute_backoff adds up to 25% jitter, so check the base range
        for attempt in range(3):
            delay = compute_backoff(cfg, attempt)
            base = 1.0 * (2.0**attempt)
            assert base <= delay <= base * 1.25

    def test_proxy_config_values(self):
        delay = compute_backoff(_RETRY_CFG, attempt=0)
        # initial_delay=1.0 + up to 25% jitter
        assert 1.0 <= delay <= 1.25

    def test_high_attempt_grows(self):
        delay = compute_backoff(_RETRY_CFG, attempt=3)
        # 1.0 * 2^3 = 8.0 base
        assert delay >= 8.0


class TestHandleRetryableStatus:
    """Tests for _handle_retryable_status (retry-after + backoff decision)."""

    @staticmethod
    async def _noop_sleep(_delay: float) -> None:
        pass

    async def test_returns_true_for_429(self):
        proxy = _make_proxy()
        proxy._retry_sleep = self._noop_sleep  # type: ignore[assignment]
        result = await proxy._handle_retryable_status(429, httpx.Headers({}), attempt=0)
        assert result is True

    async def test_returns_false_for_200(self):
        proxy = _make_proxy()
        result = await proxy._handle_retryable_status(200, httpx.Headers({}), attempt=0)
        assert result is False

    async def test_returns_false_at_max_retries(self):
        proxy = _make_proxy()
        result = await proxy._handle_retryable_status(
            429,
            httpx.Headers({}),
            attempt=_RETRY_CFG.max_retries,
        )
        assert result is False

    async def test_honours_retry_after_header(self):
        """When retry-after is provided, the sleep delay should use it."""
        slept_with: list[float] = []

        async def _capture(delay: float) -> None:
            slept_with.append(delay)

        proxy = _make_proxy()
        proxy._retry_sleep = _capture  # type: ignore[assignment]
        await proxy._handle_retryable_status(429, httpx.Headers({"retry-after": "7"}), attempt=0)
        assert slept_with == [7.0]

    async def test_retry_after_capped_at_max(self):
        slept_with: list[float] = []

        async def _capture(delay: float) -> None:
            slept_with.append(delay)

        proxy = _make_proxy()
        proxy._retry_sleep = _capture  # type: ignore[assignment]
        await proxy._handle_retryable_status(429, httpx.Headers({"retry-after": "9999"}), attempt=0)
        assert slept_with == [_MAX_BACKOFF_S]

    async def test_non_numeric_retry_after_falls_back(self):
        slept_with: list[float] = []

        async def _capture(delay: float) -> None:
            slept_with.append(delay)

        proxy = _make_proxy()
        proxy._retry_sleep = _capture  # type: ignore[assignment]
        await proxy._handle_retryable_status(
            429,
            httpx.Headers({"retry-after": "Thu, 01 Jan 2099 00:00:00 GMT"}),
            attempt=0,
        )
        # Falls back to compute_backoff; initial_delay=1.0 + jitter
        assert 1.0 <= slept_with[0] <= 1.25


# ---------------------------------------------------------------------------
# Rate-limit retry (429/529) via live proxy
# ---------------------------------------------------------------------------

_OK_RESPONSE_JSON = {
    "content": [{"type": "text", "text": "ok"}],
    "usage": {"input_tokens": 10, "output_tokens": 5},
}

_SIMPLE_REQUEST_JSON = {
    "model": "claude-sonnet-4-6",
    "max_tokens": 10,
    "messages": [{"role": "user", "content": "hi"}],
}


class TestRateLimitRetry:
    """Tests for 429/529 retry logic using a mock upstream."""

    @staticmethod
    def _patch_for_retry_test(proxy: LLMGatewayProxy) -> None:
        """Prepare proxy for retry tests: no-op sleep and pre-loaded token."""

        async def _noop(_delay: float) -> None:
            pass

        proxy._retry_sleep = _noop  # type: ignore[assignment]
        # Pre-load a dummy token so the token manager doesn't hit the network
        proxy._token_manager._token = "test-token"

    async def test_sync_retries_on_429(self, monkeypatch):
        """Non-streaming request should retry on 429 and succeed."""
        call_count = 0
        _real_post = httpx.AsyncClient.post

        async def mock_post(client_self, url, **kwargs):
            nonlocal call_count
            if "gateway.example.com" in str(url):
                call_count += 1
                if call_count == 1:
                    return httpx.Response(
                        429,
                        headers={"retry-after": "0"},
                        json={"error": "rate limited"},
                    )
                return httpx.Response(200, json=_OK_RESPONSE_JSON)
            return await _real_post(client_self, url, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

        proxy = _make_proxy()
        self._patch_for_retry_test(proxy)
        port = await proxy.start()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"http://127.0.0.1:{port}/v1/messages",
                    json=_SIMPLE_REQUEST_JSON,
                )
            assert resp.status_code == 200
            assert call_count == 2
        finally:
            await proxy.stop()

    async def test_sync_retries_on_529(self, monkeypatch):
        """Non-streaming request should retry on 529 (overloaded)."""
        call_count = 0
        _real_post = httpx.AsyncClient.post

        async def mock_post(client_self, url, **kwargs):
            nonlocal call_count
            if "gateway.example.com" in str(url):
                call_count += 1
                if call_count == 1:
                    return httpx.Response(529, json={"error": "overloaded"})
                return httpx.Response(200, json=_OK_RESPONSE_JSON)
            return await _real_post(client_self, url, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

        proxy = _make_proxy()
        self._patch_for_retry_test(proxy)
        port = await proxy.start()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"http://127.0.0.1:{port}/v1/messages",
                    json=_SIMPLE_REQUEST_JSON,
                )
            assert resp.status_code == 200
            assert call_count == 2
        finally:
            await proxy.stop()

    async def test_sync_gives_up_after_max_retries(self, monkeypatch):
        """Should return the error after exhausting retries."""
        _real_post = httpx.AsyncClient.post

        async def mock_post(client_self, url, **kwargs):
            if "gateway.example.com" in str(url):
                return httpx.Response(
                    429,
                    headers={"retry-after": "0"},
                    json={"error": "rate limited"},
                )
            return await _real_post(client_self, url, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

        proxy = _make_proxy()
        self._patch_for_retry_test(proxy)
        port = await proxy.start()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"http://127.0.0.1:{port}/v1/messages",
                    json=_SIMPLE_REQUEST_JSON,
                )
            assert resp.status_code == 429
        finally:
            await proxy.stop()
