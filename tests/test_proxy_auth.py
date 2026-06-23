"""Unit tests for TokenManager._acquire_token using httpx.MockTransport.

These exercise the real body of ``_acquire_token`` — URL construction, the
client_credentials grant payload, ``raise_for_status``, and the ``access_token``
parse — without any network. ``httpx.MockTransport`` is built into httpx (already
a proxy dependency), so no ``respx`` is needed. Runs in the default suite
(not marked ``live`` / ``requires_api_key``).
"""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from coder_eval.proxy.auth import TokenManager
from coder_eval.proxy.config import ProxyConfig


def _make_config() -> ProxyConfig:
    return ProxyConfig(
        llmgw_url="https://gw.example.com/",  # trailing slash exercised
        client_id="cid-1",
        client_secret="secret-1",
        org_id="org-1",
        tenant_id="tenant-1",
    )


def _install_mock_transport(monkeypatch, handler) -> None:
    """Make auth.py's ``httpx.AsyncClient(...)`` route through a MockTransport."""
    real_client = httpx.AsyncClient  # capture before patching to avoid self-recursion

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("coder_eval.proxy.auth.httpx.AsyncClient", factory)


async def test_acquire_token_happy_path(monkeypatch):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = parse_qs(request.content.decode())
        return httpx.Response(200, json={"access_token": "tok-123"})

    _install_mock_transport(monkeypatch, handler)

    token = await TokenManager(_make_config())._acquire_token()

    assert token == "tok-123"
    # URL is built off the rstrip'd base + the identity path.
    assert seen["url"] == "https://gw.example.com/identity_/connect/token"
    body = seen["body"]
    assert body["grant_type"] == ["client_credentials"]
    assert body["client_id"] == ["cid-1"]
    assert body["client_secret"] == ["secret-1"]


async def test_acquire_token_raises_on_non_2xx(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    _install_mock_transport(monkeypatch, handler)

    with pytest.raises(httpx.HTTPStatusError):
        await TokenManager(_make_config())._acquire_token()
