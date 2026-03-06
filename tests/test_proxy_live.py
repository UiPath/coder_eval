"""Live integration tests for the LLM Gateway proxy.

These tests hit the real LLM Gateway with real credentials from .env.
They are skipped by default and only run with: pytest -m live

Requirements:
  - LLMGW_PROXY_ENABLED=true in .env
  - Valid LLMGW_* credentials in .env
"""

import base64
import contextlib
import json
import re

import httpx
import pytest

from coder_eval.config import settings
from coder_eval.proxy.config import ProxyConfig
from coder_eval.proxy.server import LLMGatewayProxy


_skip_reason = "Live LLMGW tests require LLMGW_PROXY_ENABLED=true and valid credentials"

_live = pytest.mark.live


def _can_run_live() -> bool:
    """Check if live tests can run based on settings."""
    return bool(
        settings.llmgw_proxy_enabled
        and settings.llmgw_url
        and settings.llmgw_client_id
        and settings.llmgw_client_secret
        and settings.llmgw_semantic_org_id
        and settings.llmgw_semantic_tenant_id
    )


def _make_live_config() -> ProxyConfig:
    """Build ProxyConfig from live .env settings."""
    assert settings.llmgw_url
    assert settings.llmgw_client_id
    assert settings.llmgw_client_secret
    assert settings.llmgw_semantic_org_id
    assert settings.llmgw_semantic_tenant_id
    return ProxyConfig(
        llmgw_url=settings.llmgw_url,
        client_id=settings.llmgw_client_id,
        client_secret=settings.llmgw_client_secret,
        org_id=settings.llmgw_semantic_org_id,
        tenant_id=settings.llmgw_semantic_tenant_id,
        requesting_product=settings.llmgw_requesting_product,
        requesting_feature=settings.llmgw_requesting_feature,
        user_id=settings.llmgw_semantic_user_id or "",
        timeout_seconds=settings.llmgw_timeout_seconds,
        vendor=settings.llmgw_proxy_vendor,
        api_flavor=settings.llmgw_proxy_api_flavor,
    )


@_live
@pytest.mark.skipif(not _can_run_live(), reason=_skip_reason)
class TestLiveTokenAcquisition:
    """Test S2S token acquisition against the real identity endpoint."""

    async def test_acquire_token(self):
        from coder_eval.proxy.auth import TokenManager

        config = _make_live_config()
        tm = TokenManager(config)
        token = await tm.get_token()
        assert isinstance(token, str)
        assert len(token) > 20  # JWTs are much longer

    async def test_token_is_cached(self):
        from coder_eval.proxy.auth import TokenManager

        config = _make_live_config()
        tm = TokenManager(config)
        t1 = await tm.get_token()
        t2 = await tm.get_token()
        assert t1 == t2

    async def test_refresh_returns_new_token(self):
        from coder_eval.proxy.auth import TokenManager

        config = _make_live_config()
        tm = TokenManager(config)
        await tm.get_token()
        t2 = await tm.refresh_token()
        # Tokens may be identical if TTL hasn't changed, but both should be valid
        assert isinstance(t2, str)
        assert len(t2) > 20


@_live
@pytest.mark.skipif(not _can_run_live(), reason=_skip_reason)
class TestLiveNonStreaming:
    """Test non-streaming requests through the proxy against real gateway."""

    async def test_simple_completion(self):
        """Send a simple non-streaming request and verify we get a valid response."""
        config = _make_live_config()
        proxy = LLMGatewayProxy(config)
        port = await proxy.start()

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"http://127.0.0.1:{port}/v1/messages",
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 50,
                        "messages": [{"role": "user", "content": "Reply with exactly: hello"}],
                    },
                )

            assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.text}"
            data = resp.json()

            # Verify response structure
            assert "content" in data
            assert len(data["content"]) > 0
            assert data["content"][0]["type"] == "text"
            assert len(data["content"][0]["text"]) > 0

            # Verify usage was tracked
            assert proxy.usage.requests == 1
            assert proxy.usage.input_tokens > 0
            assert proxy.usage.output_tokens > 0

            # Verify cost calculation works
            cost = proxy.get_total_cost()
            assert cost is not None
            assert cost > 0

        finally:
            await proxy.stop()

    async def test_body_fields_stripped(self):
        """Verify non-Bedrock fields are stripped but request still succeeds."""
        config = _make_live_config()
        proxy = LLMGatewayProxy(config)
        port = await proxy.start()

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"http://127.0.0.1:{port}/v1/messages",
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 10,
                        "messages": [{"role": "user", "content": "Say hi"}],
                        # These should be stripped without error
                        "stream": False,
                        "context_management": {"enabled": True},
                        "extra_unknown_field": "should be stripped",
                    },
                )

            assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.text}"

        finally:
            await proxy.stop()


@_live
@pytest.mark.skipif(not _can_run_live(), reason=_skip_reason)
class TestLiveStreaming:
    """Test streaming (SSE) requests through the proxy against real gateway."""

    async def test_streaming_completion(self):
        """Send a streaming request and verify SSE events and usage tracking."""
        config = _make_live_config()
        proxy = LLMGatewayProxy(config)
        port = await proxy.start()

        try:
            # Use httpx streaming to consume the SSE response properly
            collected_bytes = bytearray()
            async with (
                httpx.AsyncClient(timeout=60) as client,
                client.stream(
                    "POST",
                    f"http://127.0.0.1:{port}/v1/messages",
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 50,
                        "stream": True,
                        "messages": [{"role": "user", "content": "Reply with exactly: hello"}],
                    },
                ) as resp,
            ):
                assert resp.status_code == 200, f"Unexpected status {resp.status_code}"
                async for chunk in resp.aiter_bytes():
                    collected_bytes.extend(chunk)

            # Parse events from collected response (may be SSE or Bedrock event stream)
            text = collected_bytes.decode("utf-8", errors="replace")
            events = []
            # Try SSE format first
            for line in text.split("\n"):
                line = line.strip()
                if line.startswith("data: ") and line[6:] != "[DONE]":
                    with contextlib.suppress(json.JSONDecodeError):
                        events.append(json.loads(line[6:]))
            # If no SSE events, try Bedrock event stream format (base64-encoded)
            if not events:
                for match in re.finditer(r'\{"bytes":"([A-Za-z0-9+/=]+)"', text):
                    try:
                        decoded = base64.b64decode(match.group(1))
                        events.append(json.loads(decoded))
                    except (ValueError, json.JSONDecodeError):
                        pass

            # Should have at least message_start and message_delta
            event_types = [e.get("type", "") for e in events]
            assert "message_start" in event_types, f"No message_start in: {event_types}"
            assert "message_delta" in event_types, f"No message_delta in: {event_types}"

            # Verify usage was extracted from SSE
            assert proxy.usage.requests == 1
            assert proxy.usage.input_tokens > 0
            assert proxy.usage.output_tokens > 0

            # Verify cost
            cost = proxy.get_total_cost()
            assert cost is not None
            assert cost > 0

        finally:
            await proxy.stop()


@_live
@pytest.mark.skipif(not _can_run_live(), reason=_skip_reason)
class TestLiveHealthCheck:
    """Test health endpoint with real proxy."""

    async def test_health_returns_ok(self):
        config = _make_live_config()
        proxy = LLMGatewayProxy(config)
        port = await proxy.start()

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://127.0.0.1:{port}/health")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}
        finally:
            await proxy.stop()


@_live
@pytest.mark.skipif(not _can_run_live(), reason=_skip_reason)
class TestLiveMultipleRequests:
    """Test usage accumulation across multiple live requests."""

    async def test_usage_accumulates(self):
        """Two requests should accumulate usage correctly."""
        config = _make_live_config()
        proxy = LLMGatewayProxy(config)
        port = await proxy.start()

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                for _ in range(2):
                    resp = await client.post(
                        f"http://127.0.0.1:{port}/v1/messages",
                        json={
                            "model": "claude-haiku-4-5-20251001",
                            "max_tokens": 10,
                            "messages": [{"role": "user", "content": "Say ok"}],
                        },
                    )
                    assert resp.status_code == 200

            assert proxy.usage.requests == 2
            assert proxy.usage.input_tokens > 0
            assert proxy.usage.output_tokens > 0

            cost = proxy.get_total_cost()
            assert cost is not None
            assert cost > 0

        finally:
            await proxy.stop()
