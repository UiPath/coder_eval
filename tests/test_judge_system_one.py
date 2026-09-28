"""Hermetic tests for ``invoke_system_one_async``.

This is the gate that decides ERROR versus a scored row. Every test of the
checker patches the invoker out, and the live test only covers the happy path,
so without these the missing-key, retry, fail-fast and malformed-body branches
never run in ``make test``. Mirrors tests/test_judge_bedrock.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx2
import pytest

from coder_eval.errors import JudgeInfrastructureError
from coder_eval.evaluation import judge_system_one
from coder_eval.evaluation.judge_system_one import invoke_system_one_async


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Collapse backoff so the retry ladder does not really wait ~14s."""
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(judge_system_one.asyncio, "sleep", fake_sleep)
    return delays


def _make_async_client(post_side_effect) -> MagicMock:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    # A mock response is itself callable, so passing one straight through as
    # `side_effect` would CALL it and hand back a fresh MagicMock. Serve it.
    if isinstance(post_side_effect, MagicMock):
        response = post_side_effect
        client.post = AsyncMock(side_effect=lambda *_a, **_k: response)
    else:
        client.post = AsyncMock(side_effect=post_side_effect)
    return client


def _response(status_code: int = 200, json_body: Any = None, text: str = "") -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.text = text
    if isinstance(json_body, ValueError):
        response.json = MagicMock(side_effect=json_body)
    else:
        response.json = MagicMock(return_value=json_body)
    return response


def _install(monkeypatch: pytest.MonkeyPatch, side_effect) -> None:
    monkeypatch.setattr(judge_system_one.httpx2, "AsyncClient", lambda: _make_async_client(side_effect))


async def _invoke(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "base_url": "https://api.typesafe.ai/v1",
        "api_key": "k",
        "model": "jev-latest",
        "state": {"files": {}},
        "questions": {"q": {"type": "noul", "instructions": "i"}},
    }
    defaults.update(overrides)
    return await invoke_system_one_async(**defaults)


async def test_happy_path_posts_once_and_returns_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    client_box: dict[str, MagicMock] = {}

    def capture() -> MagicMock:
        client = _make_async_client(_response(json_body={"answers": {"q": {"noul": 1.0}}}))
        client_box["client"] = client
        return client

    monkeypatch.setattr(judge_system_one.httpx2, "AsyncClient", capture)
    assert await _invoke() == {"answers": {"q": {"noul": 1.0}}}

    call = client_box["client"].post.await_args
    assert call.args[0] == "https://api.typesafe.ai/v1/systemone"
    assert call.kwargs["headers"]["Authorization"] == "Bearer k"
    assert call.kwargs["json"]["model"] == "jev-latest"


async def test_trailing_slash_does_not_double_up_the_path(monkeypatch: pytest.MonkeyPatch) -> None:
    client_box: dict[str, MagicMock] = {}

    def capture() -> MagicMock:
        client = _make_async_client(_response(json_body={"answers": {}}))
        client_box["client"] = client
        return client

    monkeypatch.setattr(judge_system_one.httpx2, "AsyncClient", capture)
    await _invoke(base_url="https://gateway.internal/v1/")
    assert client_box["client"].post.await_args.args[0] == "https://gateway.internal/v1/systemone"


async def test_missing_api_key_escalates_rather_than_scoring_zero() -> None:
    with pytest.raises(JudgeInfrastructureError, match="requires an API key"):
        await _invoke(api_key="")


@pytest.mark.parametrize(("field", "message"), [("model", "model must not be empty"), ("questions", "questions")])
async def test_empty_required_inputs_raise_value_error(field: str, message: str) -> None:
    empty: Any = {} if field == "questions" else ""
    with pytest.raises(ValueError, match=message):
        await _invoke(**{field: empty})


@pytest.mark.parametrize("status", [400, 401, 403, 422])
async def test_client_errors_fail_fast_without_retrying(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float], status: int
) -> None:
    """401/422 are the author's to fix — burning the retry ladder only delays the message."""
    calls = 0

    def counting(*_args: Any, **_kwargs: Any) -> MagicMock:
        nonlocal calls
        calls += 1
        return _response(status_code=status, text="nope")

    _install(monkeypatch, counting)
    with pytest.raises(JudgeInfrastructureError, match=str(status)):
        await _invoke()
    assert calls == 1
    assert no_sleep == []


@pytest.mark.parametrize("status", [429, 500, 503, 529])
async def test_throttles_and_server_errors_retry_then_escalate(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float], status: int
) -> None:
    calls = 0

    def counting(*_args: Any, **_kwargs: Any) -> MagicMock:
        nonlocal calls
        calls += 1
        return _response(status_code=status, text="busy")

    _install(monkeypatch, counting)
    with pytest.raises(JudgeInfrastructureError, match="after 4 attempts"):
        await _invoke()
    assert calls == 4
    assert len(no_sleep) == 3


async def test_a_retry_that_succeeds_returns_the_body(monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]) -> None:
    responses = [_response(status_code=503), _response(json_body={"answers": {"q": {"noul": 0.5}}})]
    _install(monkeypatch, responses)
    assert await _invoke() == {"answers": {"q": {"noul": 0.5}}}


async def test_transport_errors_are_retried_then_wrapped(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    _install(monkeypatch, httpx2.ConnectError("refused"))
    with pytest.raises(JudgeInfrastructureError, match="transport error"):
        await _invoke()
    assert len(no_sleep) == 3


async def test_malformed_url_escalates_instead_of_leaking_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx2.InvalidURL is NOT an HTTPError, so an uncaught one is downgraded to a scored 0.0."""
    _install(monkeypatch, httpx2.InvalidURL("bad"))
    with pytest.raises(JudgeInfrastructureError):
        await _invoke(base_url="http://[bad")


async def test_non_json_body_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _response(json_body=ValueError("not json"), text="<html>proxy</html>"))
    with pytest.raises(JudgeInfrastructureError, match="not valid JSON"):
        await _invoke()


async def test_non_object_body_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _response(json_body=["a", "list"]))
    with pytest.raises(JudgeInfrastructureError, match="not a JSON object"):
        await _invoke()
