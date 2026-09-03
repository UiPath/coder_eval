"""Tests for :mod:`coder_eval.agents._delegate_s2s_token_file`.

Mint calls are monkeypatched — no real IdP round-trips.
"""

from __future__ import annotations

import asyncio
import base64
import http.client
import json
import logging
import ssl
import time
from pathlib import Path
from typing import Any

import pytest

from coder_eval.agents import _delegate_s2s_token_file
from coder_eval.agents._delegate_s2s_token_file import (
    S2sTokenFileRefresher,
    _read_creds,
    _S2sCreds,
    decode_jwt_claims,
)


_LOG = logging.LoggerAdapter(logging.getLogger("test"), {})


def _fake_jwt(claims: dict[str, Any]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{payload}.sig"


def _refresher_env(**overrides: str) -> dict[str, str]:
    env = {
        "LLMGW_CLIENT_ID": "eval-client",
        "LLMGW_CLIENT_SECRET": "s3cret",
        "LLMGW_URL": "https://alpha.uipath.com",
        "AUTH_TOKEN": _fake_jwt({"client_id": "eval-client", "exp": int(time.time()) + 3600}),
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v}


def _maybe_create(env: dict[str, str] | None = None) -> S2sTokenFileRefresher | None:
    """``maybe_create`` against the activating env by default."""
    return S2sTokenFileRefresher.maybe_create(_refresher_env() if env is None else env, _LOG)


# ---- decode_jwt_claims ------------------------------------------------------


class TestDecodeJwtClaims:
    def test_decodes_payload_claims(self) -> None:
        token = _fake_jwt({"client_id": "abc", "exp": 123})

        assert decode_jwt_claims(token) == {"client_id": "abc", "exp": 123}

    @pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b", "a.!!!.c", "a." + "b" * 10 + ".c"])
    def test_malformed_tokens_return_none(self, token: str) -> None:
        assert decode_jwt_claims(token) is None


# ---- _read_creds ------------------------------------------------------------


class TestReadCreds:
    def test_resolves_token_url_at_origin(self) -> None:
        creds = _read_creds(_refresher_env())

        assert creds is not None
        assert creds.token_url == "https://alpha.uipath.com/identity_/connect/token"

    def test_gateway_path_suffix_is_discarded(self) -> None:
        creds = _read_creds(_refresher_env(LLMGW_URL="https://alpha.uipath.com/llmgw"))

        assert creds is not None
        assert creds.token_url == "https://alpha.uipath.com/identity_/connect/token"

    @pytest.mark.parametrize("missing", ["LLMGW_CLIENT_ID", "LLMGW_CLIENT_SECRET", "LLMGW_URL"])
    def test_incomplete_triple_returns_none(self, missing: str) -> None:
        assert _read_creds(_refresher_env(**{missing: ""})) is None

    def test_invalid_url_returns_none(self) -> None:
        assert _read_creds(_refresher_env(LLMGW_URL="not a url")) is None

    @pytest.mark.parametrize("url", ["http://alpha.uipath.com", "file:///etc/passwd", "ftp://alpha.uipath.com"])
    def test_non_https_scheme_returns_none(self, url: str) -> None:
        """The mint body carries LLMGW_CLIENT_SECRET — never over a non-TLS scheme."""
        assert _read_creds(_refresher_env(LLMGW_URL=url)) is None


# ---- _mint_s2s_token ---------------------------------------------------------


class TestMintS2sToken:
    @staticmethod
    def _creds() -> _S2sCreds:
        creds = _read_creds(_refresher_env())
        assert creds is not None
        return creds

    def test_request_carries_a_real_user_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """UiPath's Cloudflare WAF 403s urllib's default UA (error code 1010)."""
        seen: dict[str, Any] = {}

        class _FakeResponse:
            def read(self) -> bytes:
                return json.dumps({"access_token": "tok"}).encode()

            def __enter__(self) -> _FakeResponse:
                return self

            def __exit__(self, *args: Any) -> None:
                return None

        def _fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
            seen["user_agent"] = request.get_header("User-agent")
            return _FakeResponse()

        monkeypatch.setattr(_delegate_s2s_token_file.urllib.request, "urlopen", _fake_urlopen)

        assert _delegate_s2s_token_file._mint_s2s_token(self._creds()) == "tok"
        assert seen["user_agent"]
        assert "python-urllib" not in seen["user_agent"].lower()

    def test_http_error_surfaces_response_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import io
        import urllib.error

        def _fake_urlopen(request: Any, timeout: float) -> Any:
            raise urllib.error.HTTPError(
                request.full_url, 403, "Forbidden", hdrs=None, fp=io.BytesIO(b"error code: 1010\n")
            )

        monkeypatch.setattr(_delegate_s2s_token_file.urllib.request, "urlopen", _fake_urlopen)

        with pytest.raises(RuntimeError, match=r"HTTP Error 403.*error code: 1010"):
            _delegate_s2s_token_file._mint_s2s_token(self._creds())

    @pytest.mark.parametrize(
        "raised",
        [
            pytest.param(http.client.RemoteDisconnected("closed early"), id="remote-disconnected"),
            pytest.param(http.client.IncompleteRead(b"half"), id="incomplete-read"),
            pytest.param(ssl.SSLError("handshake"), id="ssl-error"),
        ],
    )
    def test_transport_failures_are_normalized_to_runtimeerror(
        self, monkeypatch: pytest.MonkeyPatch, raised: Exception
    ) -> None:
        """These escape urllib un-wrapped (not URLError), so the contract must still hold."""

        def _fake_urlopen(request: Any, timeout: float) -> Any:
            raise raised

        monkeypatch.setattr(_delegate_s2s_token_file.urllib.request, "urlopen", _fake_urlopen)

        with pytest.raises(RuntimeError, match="rejected client_credentials"):
            _delegate_s2s_token_file._mint_s2s_token(self._creds())

    def test_missing_access_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _FakeResponse:
            def read(self) -> bytes:
                return json.dumps({"token_type": "Bearer"}).encode()

            def __enter__(self) -> _FakeResponse:
                return self

            def __exit__(self, *args: Any) -> None:
                return None

        monkeypatch.setattr(
            _delegate_s2s_token_file.urllib.request, "urlopen", lambda request, timeout: _FakeResponse()
        )

        with pytest.raises(RuntimeError, match="no access_token"):
            _delegate_s2s_token_file._mint_s2s_token(self._creds())


# ---- S2sTokenFileRefresher.maybe_create -------------------------------------


class TestMaybeCreate:
    def test_creates_when_auth_token_minted_by_same_client(self) -> None:
        assert _maybe_create() is not None

    def test_none_without_gateway_creds(self) -> None:
        assert _maybe_create(_refresher_env(LLMGW_CLIENT_SECRET="")) is None

    @pytest.mark.parametrize("name", ["DELEGATE_AUTH_TOKEN_FILE", "AUTH_TOKEN_FILE"])
    def test_none_when_external_token_file_configured(self, name: str) -> None:
        env = _refresher_env(**{name: "/some/token/file"})

        assert _maybe_create(env) is None

    def test_none_without_auth_token(self) -> None:
        assert _maybe_create(_refresher_env(AUTH_TOKEN="")) is None

    def test_none_when_auth_token_from_other_client(self) -> None:
        env = _refresher_env(AUTH_TOKEN=_fake_jwt({"client_id": "someone-else"}))

        assert _maybe_create(env) is None

    def test_none_when_auth_token_is_opaque(self) -> None:
        env = _refresher_env(AUTH_TOKEN="opaque-token-with-no-claims")

        assert _maybe_create(env) is None


# ---- start / stop lifecycle --------------------------------------------------


class TestStartStop:
    @pytest.mark.asyncio
    async def test_start_writes_minted_token_and_stop_removes_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        minted = _fake_jwt({"client_id": "eval-client", "exp": int(time.time()) + 3600})
        monkeypatch.setattr(_delegate_s2s_token_file, "_mint_s2s_token", lambda creds: minted)
        refresher = _maybe_create()
        assert refresher is not None

        path = await refresher.start()

        try:
            assert await asyncio.to_thread(Path(path).read_text, encoding="utf-8") == minted
        finally:
            await refresher.stop()
        assert not Path(path).exists()

    @pytest.mark.asyncio
    async def test_initial_mint_failure_seeds_inherited_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(creds: Any) -> str:
            raise RuntimeError("IdP down")

        monkeypatch.setattr(_delegate_s2s_token_file, "_mint_s2s_token", _boom)
        env = _refresher_env()
        refresher = _maybe_create(env)
        assert refresher is not None

        path = await refresher.start()

        try:
            assert await asyncio.to_thread(Path(path).read_text, encoding="utf-8") == env["AUTH_TOKEN"]
        finally:
            await refresher.stop()

    @pytest.mark.asyncio
    async def test_initial_mint_transport_failure_does_not_propagate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """start() is best-effort for non-RuntimeError failures too."""

        def _boom(creds: Any) -> str:
            raise http.client.RemoteDisconnected("closed early")

        monkeypatch.setattr(_delegate_s2s_token_file, "_mint_s2s_token", _boom)
        env = _refresher_env()
        refresher = _maybe_create(env)
        assert refresher is not None

        path = await refresher.start()

        try:
            assert await asyncio.to_thread(Path(path).read_text, encoding="utf-8") == env["AUTH_TOKEN"]
        finally:
            await refresher.stop()

    @pytest.mark.asyncio
    async def test_refresh_loop_rewrites_file_with_new_mint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An already-inside-the-lead-window exp forces the loop's first sleep to
        # the 60s floor; shrinking the floor makes the rewrite observable fast.
        first = _fake_jwt({"client_id": "eval-client", "exp": int(time.time())})
        second = _fake_jwt({"client_id": "eval-client", "exp": int(time.time()) + 7200})
        mints = iter([first, second])
        monkeypatch.setattr(_delegate_s2s_token_file, "_mint_s2s_token", lambda creds: next(mints))
        monkeypatch.setattr(_delegate_s2s_token_file, "_MIN_DELAY_SECONDS", 0.01)
        refresher = _maybe_create()
        assert refresher is not None

        path = await refresher.start()

        try:
            await _await_token(path, second)
        finally:
            await refresher.stop()

    def test_token_file_before_start_raises(self) -> None:
        refresher = _maybe_create()
        assert refresher is not None

        with pytest.raises(RuntimeError, match="start"):
            _ = refresher.token_file

    def test_write_token_before_start_raises(self) -> None:
        refresher = _maybe_create()
        assert refresher is not None

        with pytest.raises(RuntimeError, match="start"):
            refresher._write_token("tok")

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_delegate_s2s_token_file, "_mint_s2s_token", lambda creds: "tok")
        refresher = _maybe_create()
        assert refresher is not None
        path = await refresher.start()

        await refresher.stop()
        await refresher.stop()

        assert not Path(path).exists()


# ---- refresh-loop resilience -------------------------------------------------


class TestRefreshLoopResilience:
    """A loop that dies silently resurrects the very 401 this module prevents."""

    @staticmethod
    def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_delegate_s2s_token_file, "_MIN_DELAY_SECONDS", 0.01)
        monkeypatch.setattr(_delegate_s2s_token_file, "_RETRY_SECONDS", 0.01)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(RuntimeError("IdP down"), id="idp-refusal"),
            pytest.param(http.client.RemoteDisconnected("closed early"), id="non-runtimeerror-transport"),
        ],
    )
    async def test_loop_survives_a_mint_failure_and_lands_the_next_token(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        # exp already inside the refresh lead window ⇒ the loop's first sleep is
        # the (shrunk) floor, so both the failure and the recovery are observable.
        first = _fake_jwt({"client_id": "eval-client", "exp": int(time.time())})
        third = _fake_jwt({"client_id": "eval-client", "exp": int(time.time()) + 7200})
        outcomes: list[Any] = [first, error, third]

        def _mint(creds: Any) -> str:
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        self._fast(monkeypatch)
        monkeypatch.setattr(_delegate_s2s_token_file, "_mint_s2s_token", _mint)
        refresher = _maybe_create()
        assert refresher is not None

        path = await refresher.start()

        try:
            # The failed mint must leave the previous (stale but still valid)
            # token in place rather than clobbering the file.
            assert await asyncio.to_thread(Path(path).read_text, encoding="utf-8") == first
            await _await_token(path, third)
            assert refresher._task is not None and not refresher._task.done()
        finally:
            await refresher.stop()

    @pytest.mark.asyncio
    async def test_loop_survives_a_write_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        first = _fake_jwt({"client_id": "eval-client", "exp": int(time.time())})
        second = _fake_jwt({"client_id": "eval-client", "exp": int(time.time()) + 7200})
        mints = iter([first, second, second])
        self._fast(monkeypatch)
        monkeypatch.setattr(_delegate_s2s_token_file, "_mint_s2s_token", lambda creds: next(mints))
        refresher = _maybe_create()
        assert refresher is not None
        path = await refresher.start()
        real_write = refresher._write_token
        writes = {"n": 0}

        def _flaky_write(token: str) -> None:
            writes["n"] += 1
            if writes["n"] == 2:  # the loop's first write, after start()'s
                raise OSError("no space left on device")
            real_write(token)

        monkeypatch.setattr(refresher, "_write_token", _flaky_write)

        try:
            await _await_token(path, second)
            assert refresher._task is not None and not refresher._task.done()
        finally:
            await refresher.stop()

    @pytest.mark.asyncio
    async def test_stop_logs_a_loop_that_died(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(_delegate_s2s_token_file, "_mint_s2s_token", lambda creds: "tok")
        refresher = _maybe_create()
        assert refresher is not None
        await refresher.start()

        async def _die() -> None:
            raise ValueError("loop blew up")

        assert refresher._task is not None
        refresher._task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await refresher._task
        refresher._task = asyncio.create_task(_die())
        await asyncio.sleep(0)

        with caplog.at_level(logging.ERROR, logger="test"):
            await refresher.stop()

        assert "refresh loop had died" in caplog.text


# ---- _delay_until_refresh ----------------------------------------------------


class TestDelayUntilRefresh:
    def test_schedules_ahead_of_expiry(self) -> None:
        refresher = _maybe_create()
        assert refresher is not None
        token = _fake_jwt({"exp": int(time.time()) + 3600})

        delay = refresher._delay_until_refresh(token)

        assert (
            _delegate_s2s_token_file._MIN_DELAY_SECONDS
            <= delay
            <= 3600 - _delegate_s2s_token_file._REFRESH_LEAD_SECONDS
        )

    def test_floors_the_delay_for_an_already_expiring_token(self) -> None:
        refresher = _maybe_create()
        assert refresher is not None

        assert refresher._delay_until_refresh(_fake_jwt({"exp": 1})) == _delegate_s2s_token_file._MIN_DELAY_SECONDS

    @pytest.mark.parametrize("token", ["opaque-token", _fake_jwt({"client_id": "x"})])
    def test_falls_back_to_a_fixed_interval_without_exp(self, token: str) -> None:
        refresher = _maybe_create()
        assert refresher is not None

        assert refresher._delay_until_refresh(token) == _delegate_s2s_token_file._FALLBACK_INTERVAL_SECONDS


async def _await_token(path: str, expected: str, timeout: float = 5.0) -> None:
    """Poll the token file until it holds ``expected``, or fail the test."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await asyncio.to_thread(Path(path).read_text, encoding="utf-8") == expected:
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"token file never reached the expected value within {timeout}s")
