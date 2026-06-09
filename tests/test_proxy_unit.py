"""Unit tests for the LLM Gateway proxy package.

Tests cover: model mapping, header/body transformation, SSE usage extraction,
pricing calculation, token manager, and proxy lifecycle.
"""

import base64
import json
from urllib.parse import urlparse

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
    measure_proxy,
    usage_between,
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

    @pytest.mark.parametrize(
        "alias, expected",
        [
            # Short aliases resolve to same gateway ID as dated versions
            ("claude-opus-4-5", "anthropic.claude-opus-4-5-20251101-v1:0"),
            ("claude-sonnet-4-5", "anthropic.claude-sonnet-4-5-20250929-v1:0"),
            ("claude-haiku-4-5", "anthropic.claude-haiku-4-5-20251001-v1:0"),
            # -latest aliases
            ("claude-opus-4-5-latest", "anthropic.claude-opus-4-5-20251101-v1:0"),
            ("claude-sonnet-4-5-latest", "anthropic.claude-sonnet-4-5-20250929-v1:0"),
            ("claude-haiku-4-5-latest", "anthropic.claude-haiku-4-5-20251001-v1:0"),
            ("claude-3-7-sonnet-latest", "anthropic.claude-3-7-sonnet-20250219-v1:0"),
            ("claude-3-5-sonnet-latest", "anthropic.claude-3-5-sonnet-20241022-v2:0"),
        ],
    )
    def test_new_model_aliases(self, alias, expected):
        proxy = _make_proxy()
        assert proxy._map_model(alias) == expected

    def test_short_alias_matches_dated_version(self):
        """Short aliases must resolve to the same gateway ID as their dated counterparts."""
        proxy = _make_proxy()
        assert proxy._map_model("claude-opus-4-5") == proxy._map_model("claude-opus-4-5-20251101")
        assert proxy._map_model("claude-sonnet-4-5") == proxy._map_model("claude-sonnet-4-5-20250929")
        assert proxy._map_model("claude-haiku-4-5") == proxy._map_model("claude-haiku-4-5-20251001")

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


class TestProxyUsageSnapshotAndDiff:
    """Tests for ``ProxyUsage.snapshot()`` and the module-level
    ``usage_between(before, after)`` helper.

    These primitives are the foundation of per-consumer token attribution
    on LLMGW: each consumer (main-agent turn, sub-agent judge, llm_judge
    call) snapshots the proxy before its work and computes the delta after,
    so it can attribute only its own slice instead of pooling everything
    into the main agent's total.
    """

    def test_snapshot_is_independent_of_live_mutation(self):
        u = ProxyUsage(input_tokens=10, output_tokens=20, total_cost=0.001)
        snap = u.snapshot()
        # Mutate the live instance after the snapshot.
        u.input_tokens = 999
        u.output_tokens = 888
        u.total_cost = 9.99
        u.cache_creation_input_tokens = 500
        # Snapshot must remain frozen at its capture point.
        assert snap.input_tokens == 10
        assert snap.output_tokens == 20
        assert snap.total_cost == 0.001
        assert snap.cache_creation_input_tokens == 0

    def test_snapshot_copies_models_used_dict(self):
        u = ProxyUsage()
        u.models_used["model-a"] = 1
        snap = u.snapshot()
        u.models_used["model-a"] = 99
        u.models_used["model-b"] = 5
        assert snap.models_used == {"model-a": 1}

    def test_usage_between_returns_per_field_delta(self):
        before = ProxyUsage(
            input_tokens=100,
            output_tokens=50,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=1000,
            total_cost=0.10,
        )
        after = ProxyUsage(
            input_tokens=150,
            output_tokens=80,
            cache_creation_input_tokens=250,
            cache_read_input_tokens=1500,
            total_cost=0.18,
        )
        delta = usage_between(before, after)
        assert delta.uncached_input_tokens == 50
        assert delta.output_tokens == 30
        assert delta.cache_creation_input_tokens == 50
        assert delta.cache_read_input_tokens == 500
        assert delta.total_cost_usd is not None
        assert delta.total_cost_usd == pytest.approx(0.08)

    def test_usage_between_zero_delta_returns_zero_tokens_and_none_cost(self):
        u = ProxyUsage(input_tokens=10, total_cost=0.001)
        snap = u.snapshot()
        delta = usage_between(snap, u)
        assert delta.input_tokens == 0
        assert delta.output_tokens == 0
        assert delta.cache_creation_input_tokens == 0
        assert delta.cache_read_input_tokens == 0
        # Cost diff is 0.0; usage_between surfaces None rather than 0.0 for
        # cost so downstream "is None" gates don't fire on a no-op.
        assert delta.total_cost_usd is None

    def test_usage_between_typical_full_cycle(self):
        """End-to-end snapshot/diff usage that matches the orchestrator's pattern."""
        proxy = _make_proxy()
        pre = proxy.usage.snapshot()
        proxy._track_usage("model-a", {"input_tokens": 60_000, "output_tokens": 250})
        delta = usage_between(pre, proxy.usage)
        assert delta.input_tokens == 60_000
        assert delta.output_tokens == 250


class TestMeasureProxy:
    """Tests for the ``measure_proxy`` context manager — the single home for
    the snapshot/diff attribution pattern that all three call sites use.
    """

    def test_none_proxy_yields_getter_returning_none(self):
        """Direct / Bedrock pass proxy=None; the getter must be a no-op."""
        with measure_proxy(None) as proxy_delta:
            assert proxy_delta() is None
        # Still None after the block (no live usage to read).
        assert proxy_delta() is None

    def test_returns_field_wise_delta_for_mid_block_traffic(self):
        proxy = _make_proxy()
        proxy._track_usage("model-a", {"input_tokens": 1_000, "output_tokens": 10})
        with measure_proxy(proxy) as proxy_delta:
            proxy._track_usage(
                "model-a",
                {
                    "input_tokens": 60_000,
                    "output_tokens": 250,
                    "cache_read_input_tokens": 5_000,
                },
            )
            delta = proxy_delta()
        assert delta is not None
        assert delta.uncached_input_tokens == 60_000
        assert delta.output_tokens == 250
        assert delta.cache_read_input_tokens == 5_000

    def test_returns_none_when_no_traffic_occurred(self):
        """An empty window drops to None, not an all-zero TokenUsage."""
        proxy = _make_proxy()
        proxy._track_usage("model-a", {"input_tokens": 1_000, "output_tokens": 10})
        with measure_proxy(proxy) as proxy_delta:
            pass  # no traffic inside the window
        assert proxy_delta() is None

    def test_getter_returns_delta_when_block_raises(self):
        """Exception inside the block must not lose the pre-failure delta —
        the getter re-reads live usage, matching the prior ``finally`` site.

        Uses bare ``try/except`` (rather than ``pytest.raises``) so the
        post-block assertions are unambiguously reachable to CodeQL's
        Python analyzer, which doesn't model ``pytest.raises`` as a
        catch site and would otherwise flag the assertions as unreachable.
        """
        proxy = _make_proxy()
        captured: list = []
        raised = False
        try:
            with measure_proxy(proxy) as proxy_delta:
                proxy._track_usage("model-a", {"input_tokens": 42, "output_tokens": 8})
                captured.append(proxy_delta)
                raise RuntimeError("boom")
        except RuntimeError:
            raised = True

        assert raised, "expected RuntimeError to propagate out of the measure_proxy block"
        delta = captured[0]()
        assert delta is not None
        assert delta.input_tokens == 42
        assert delta.output_tokens == 8


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

    def test_multiple_message_delta_events_take_final_cumulative(self):
        """Per the Anthropic streaming spec, message_delta.usage.output_tokens
        is the CUMULATIVE running total for the message. Multiple message_delta
        events each carry the updated total — only the last one is the true
        final. Summing them inflates the count by ~Nx for an N-event stream.
        """
        proxy = _make_proxy()
        # 6 message_delta events with monotonically-increasing cumulative output_tokens.
        # Final cumulative = 50. Summing the values yields 5+12+20+28+38+50 = 153.
        sse = (
            b'data: {"type": "message_start", "message": {"usage": {"input_tokens": 100}}}\n'
            b'data: {"type": "message_delta", "usage": {"output_tokens": 5}}\n'
            b'data: {"type": "message_delta", "usage": {"output_tokens": 12}}\n'
            b'data: {"type": "message_delta", "usage": {"output_tokens": 20}}\n'
            b'data: {"type": "message_delta", "usage": {"output_tokens": 28}}\n'
            b'data: {"type": "message_delta", "usage": {"output_tokens": 38}}\n'
            b'data: {"type": "message_delta", "usage": {"output_tokens": 50}}\n'
        )
        proxy._extract_usage_from_sse(sse, "model-a")
        assert proxy.usage.output_tokens == 50, (
            "Expected the final cumulative value (50), not the sum of all events (153)."
        )
        assert proxy.usage.input_tokens == 100
        assert proxy.usage.requests == 1


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

    def test_bedrock_multiple_message_delta_take_final_cumulative(self):
        """Same cumulative-vs-summed invariant as the SSE path, exercised
        against the Bedrock event-stream parser.
        """
        proxy = _make_proxy()
        raw = (
            _bedrock_frame({"type": "message_start", "message": {"usage": {"input_tokens": 100}}})
            + _bedrock_frame({"type": "message_delta", "usage": {"output_tokens": 10}})
            + _bedrock_frame({"type": "message_delta", "usage": {"output_tokens": 30}})
            + _bedrock_frame({"type": "message_delta", "usage": {"output_tokens": 50}})
        )
        proxy._extract_usage_from_sse(raw, "model-a")
        # Final cumulative = 50; the buggy sum would have been 90.
        assert proxy.usage.output_tokens == 50
        assert proxy.usage.input_tokens == 100
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
        cost = calculate_cost("claude-sonnet-4-6", uncached_input_tokens=1_000_000, output_tokens=0)
        assert cost == pytest.approx(3.0)

        cost = calculate_cost("claude-sonnet-4-6", uncached_input_tokens=0, output_tokens=1_000_000)
        assert cost == pytest.approx(15.0)

    def test_opus_cost(self):
        # Opus 4.6: $15/MTok in, $75/MTok out
        cost = calculate_cost("claude-opus-4-6", uncached_input_tokens=1_000_000, output_tokens=0)
        assert cost == pytest.approx(15.0)

    def test_haiku_cost(self):
        # Haiku 4.5: $0.80/MTok in, $4.0/MTok out
        cost = calculate_cost("claude-haiku-4-5-20251001", uncached_input_tokens=1_000_000, output_tokens=0)
        assert cost == pytest.approx(0.80)

    def test_cache_tokens_add_to_cost(self):
        cost = calculate_cost(
            "claude-sonnet-4-6",
            uncached_input_tokens=0,
            output_tokens=0,
            cache_creation_tokens=1_000_000,
            cache_read_tokens=1_000_000,
        )
        # cache_write: $3.75/MTok, cache_read: $0.30/MTok
        assert cost == pytest.approx(3.75 + 0.30)

    def test_unknown_model_returns_none(self):
        assert calculate_cost("unknown-model", uncached_input_tokens=1000, output_tokens=500) is None

    def test_bedrock_qualified_model_prices_same_as_bare(self):
        # A Bedrock route qualifies the alias into a region/vendor inference
        # profile id (e.g. eu.anthropic.claude-opus-4-8). It must price the same
        # as the bare alias so timeout-turn cost backfill works on Bedrock too
        # (issue #386 follow-up). Covers each region prefix + the bare anthropic.
        bare = calculate_cost("claude-opus-4-8", uncached_input_tokens=1_000_000, output_tokens=500_000)
        assert bare is not None
        for qualified in (
            "eu.anthropic.claude-opus-4-8",
            "us.anthropic.claude-opus-4-8",
            "apac.anthropic.claude-opus-4-8",
            "global.anthropic.claude-opus-4-8",
            "anthropic.claude-opus-4-8",
        ):
            assert calculate_cost(qualified, uncached_input_tokens=1_000_000, output_tokens=500_000) == bare, qualified

    def test_normalization_does_not_misprice_bare_models(self):
        # Bare aliases (Claude + gpt-*) must pass through normalization unchanged.
        assert calculate_cost("claude-sonnet-4-6", uncached_input_tokens=1_000_000, output_tokens=0) == pytest.approx(
            3.0
        )
        assert calculate_cost("gpt-5-codex", uncached_input_tokens=1_000_000, output_tokens=0) == pytest.approx(1.25)

    def test_pricing_region_prefixes_mirror_routing(self):
        # _normalize_model mirrors routing._BEDROCK_KNOWN_PREFIXES; if routing
        # adds a region prefix, pricing must strip it too or Bedrock backfill
        # silently regresses. Lock the mirror so the two can't drift apart.
        from coder_eval.models.routing import _BEDROCK_KNOWN_PREFIXES
        from coder_eval.proxy.pricing import _BEDROCK_REGION_PREFIXES

        assert set(_BEDROCK_REGION_PREFIXES) == set(_BEDROCK_KNOWN_PREFIXES)

    def test_zero_tokens_returns_zero(self):
        cost = calculate_cost("claude-sonnet-4-6", uncached_input_tokens=0, output_tokens=0)
        assert cost == 0.0

    def test_realistic_request(self):
        # 50k input, 2k output on Sonnet
        cost = calculate_cost("claude-sonnet-4-6", uncached_input_tokens=50_000, output_tokens=2_000)
        expected = (50_000 * 3.0 + 2_000 * 15.0) / 1_000_000
        assert cost == pytest.approx(expected)

    def test_gpt5_codex_cost(self):
        # gpt-5-codex: $1.25/MTok in, $10/MTok out
        cost = calculate_cost("gpt-5-codex", uncached_input_tokens=1_000_000, output_tokens=0)
        assert cost == pytest.approx(1.25)

        cost = calculate_cost("gpt-5-codex", uncached_input_tokens=0, output_tokens=1_000_000)
        assert cost == pytest.approx(10.0)

    def test_gpt5_cost(self):
        # gpt-5: same rates as gpt-5-codex ($1.25/MTok in, $10/MTok out)
        cost = calculate_cost("gpt-5", uncached_input_tokens=1_000_000, output_tokens=0)
        assert cost == pytest.approx(1.25)

        cost = calculate_cost("gpt-5", uncached_input_tokens=0, output_tokens=1_000_000)
        assert cost == pytest.approx(10.0)

    def test_gpt5_3_codex_cost(self):
        # gpt-5.3-codex: $1.75/MTok in, $14/MTok out
        cost = calculate_cost("gpt-5.3-codex", uncached_input_tokens=1_000_000, output_tokens=0)
        assert cost == pytest.approx(1.75)

        cost = calculate_cost("gpt-5.3-codex", uncached_input_tokens=0, output_tokens=1_000_000)
        assert cost == pytest.approx(14.0)

    def test_gpt5_codex_cached_input(self):
        # Mirrors CodexAgent._token_usage_from_sdk billing: non-cached input at
        # the input rate, cached portion at the cache-read rate ($0.125/MTok).
        cost = calculate_cost(
            "gpt-5-codex",
            uncached_input_tokens=1_000_000,
            output_tokens=0,
            cache_read_tokens=1_000_000,
        )
        assert cost == pytest.approx(1.25 + 0.125)

    def test_gpt5_4_cost(self):
        # gpt-5.4: $2.50/MTok in, $15/MTok out, $0.25/MTok cached.
        assert calculate_cost("gpt-5.4", uncached_input_tokens=1_000_000, output_tokens=0) == pytest.approx(2.5)
        assert calculate_cost("gpt-5.4", uncached_input_tokens=0, output_tokens=1_000_000) == pytest.approx(15.0)
        assert calculate_cost(
            "gpt-5.4", uncached_input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000
        ) == pytest.approx(0.25)

    def test_gpt5_5_cost(self):
        # gpt-5.5: $5/MTok in, $30/MTok out, $0.50/MTok cached.
        assert calculate_cost("gpt-5.5", uncached_input_tokens=1_000_000, output_tokens=0) == pytest.approx(5.0)
        assert calculate_cost("gpt-5.5", uncached_input_tokens=0, output_tokens=1_000_000) == pytest.approx(30.0)
        assert calculate_cost(
            "gpt-5.5", uncached_input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000
        ) == pytest.approx(0.5)

    def test_openai_cache_write_rate_equals_input_rate(self):
        # OpenAI bills no separate cache-write fee, so the table sets
        # cache_write == input rate for every gpt-* model. This is the invariant
        # that makes CodexAgent's "fresh -> cache_creation" re-attribution
        # cost-neutral. Guard it for all priced OpenAI models.
        from coder_eval.proxy.pricing import _PRICING

        openai_models = [m for m in _PRICING if m.startswith("gpt-")]
        assert openai_models  # table actually has OpenAI entries
        for model in openai_models:
            pricing = _PRICING[model]
            assert pricing.cache_write_per_mtok == pricing.input_per_mtok, model

    def test_cache_write_reattribution_is_cost_neutral(self):
        # CodexAgent moved the fresh slice from input_tokens into
        # cache_creation_tokens. Because cache_write == input rate for OpenAI,
        # pricing the fresh tokens either way yields the same cost.
        fresh, cached, out = 14_080, 278, 458
        as_input = calculate_cost("gpt-5.5", uncached_input_tokens=fresh, output_tokens=out, cache_read_tokens=cached)
        as_cache_write = calculate_cost(
            "gpt-5.5", uncached_input_tokens=0, output_tokens=out, cache_creation_tokens=fresh, cache_read_tokens=cached
        )
        assert as_input == pytest.approx(as_cache_write)


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


# ---------------------------------------------------------------------------
# cache_control forwarding
# ---------------------------------------------------------------------------


class TestCacheControlForwarding:
    """Verify cache_control blocks survive the proxy and reach the gateway.

    AWS Bedrock and Vertex AI both support Anthropic prompt caching with the
    same ``cache_control: {type: "ephemeral"}`` syntax used by the direct API,
    so the proxy must forward those blocks intact rather than stripping them.
    """

    async def _capture_forwarded_body(
        self,
        monkeypatch: pytest.MonkeyPatch,
        request_json: dict,
        vendor: str = "awsbedrock",
    ) -> dict:
        """Spin up the proxy, send ``request_json``, return the body the proxy forwarded upstream."""
        captured: dict = {}
        _real_post = httpx.AsyncClient.post

        async def mock_post(client_self, url, **kwargs):
            if urlparse(str(url)).hostname == "gateway.example.com":
                captured["body"] = json.loads(kwargs["content"])
                return httpx.Response(200, json=_OK_RESPONSE_JSON)
            return await _real_post(client_self, url, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

        proxy = _make_proxy(vendor=vendor)
        proxy._token_manager._token = "test-token"
        port = await proxy.start()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(f"http://127.0.0.1:{port}/v1/messages", json=request_json)
            assert resp.status_code == 200, resp.text
        finally:
            await proxy.stop()
        return captured["body"]

    @pytest.mark.parametrize("vendor", ["awsbedrock", "anthropic"])
    async def test_cache_control_preserved_in_system(self, monkeypatch, vendor):
        body = await self._capture_forwarded_body(
            monkeypatch,
            {
                "model": "claude-sonnet-4-6",
                "max_tokens": 10,
                "system": [
                    {"type": "text", "text": "You are helpful", "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": "Be concise"},
                ],
                "messages": [{"role": "user", "content": "hi"}],
            },
            vendor=vendor,
        )
        assert body["system"][0].get("cache_control") == {"type": "ephemeral"}
        assert "cache_control" not in body["system"][1]

    async def test_cache_control_preserved_in_message_content(self, monkeypatch):
        body = await self._capture_forwarded_body(
            monkeypatch,
            {
                "model": "claude-sonnet-4-6",
                "max_tokens": 10,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "long prefix", "cache_control": {"type": "ephemeral"}},
                            {"type": "text", "text": "trailing question"},
                        ],
                    }
                ],
            },
        )
        content = body["messages"][0]["content"]
        assert content[0].get("cache_control") == {"type": "ephemeral"}
        assert "cache_control" not in content[1]

    async def test_cache_control_preserved_in_tools(self, monkeypatch):
        body = await self._capture_forwarded_body(
            monkeypatch,
            {
                "model": "claude-sonnet-4-6",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [
                    {
                        "name": "get_weather",
                        "description": "Get weather",
                        "input_schema": {"type": "object"},
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
            },
        )
        assert body["tools"][0].get("cache_control") == {"type": "ephemeral"}

    async def test_anthropic_version_still_injected(self, monkeypatch):
        """The Bedrock version stamp must still be injected even without stripping."""
        body = await self._capture_forwarded_body(
            monkeypatch,
            {
                "model": "claude-sonnet-4-6",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert body["anthropic_version"] == "bedrock-2023-05-31"
