"""Tests for the Bedrock invoke helper used by judge-style criteria.

The helper issues a forced ``submit_verdict`` tool call and returns the parsed
response dict so the caller can walk ``content`` for ``tool_use`` blocks.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx2
import pytest

from coder_eval.errors import JudgeInfrastructureError
from coder_eval.evaluation import judge_bedrock
from coder_eval.evaluation.judge_bedrock import invoke_bedrock_judge_async
from coder_eval.evaluation.verdict_tool import SUBMIT_VERDICT_ANTHROPIC_TOOL
from coder_eval.models.routing import BedrockRoute


def _make_response(*, status_code: int = 200, json_data: Any = None, text: str = "") -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data if json_data is not None else {}
    response.text = text
    return response


@pytest.fixture(autouse=True)
def _bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """invoke_bedrock_judge_async reads the bearer token from settings, not the
    route, so every test needs one set regardless of whether it inspects it."""
    monkeypatch.setattr(judge_bedrock.settings, "aws_bearer_token_bedrock", "test-token")


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Capture backoff sleeps instead of actually sleeping."""
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr(judge_bedrock.asyncio, "sleep", fake_sleep)
    return sleeps


def _route() -> BedrockRoute:
    return BedrockRoute(region="eu-north-1")


def _tool_use_response(score: float = 0.5, rationale: str = "ok") -> dict[str, Any]:
    return {
        "content": [
            {"type": "tool_use", "name": "submit_verdict", "input": {"score": score, "rationale": rationale}},
        ]
    }


def _make_async_client(post_side_effect) -> MagicMock:
    """Mock ``httpx2.AsyncClient`` — supports the ``async with`` + repeated ``.post(...)`` shape."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = AsyncMock(side_effect=post_side_effect)
    return client


async def _invoke(**overrides):
    defaults = {
        "route": _route(),
        "model": "anthropic.claude-sonnet-4-6",
        "system": "s",
        "user": "u",
        "temperature": 0.0,
        "max_tokens": 10,
        "tool_spec": SUBMIT_VERDICT_ANTHROPIC_TOOL,
    }
    defaults.update(overrides)
    return await invoke_bedrock_judge_async(**defaults)


async def test_invoke_bedrock_judge_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float) -> MagicMock:
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _make_response(status_code=200, json_data=_tool_use_response(score=0.5))

    monkeypatch.setattr(judge_bedrock.httpx2, "AsyncClient", lambda: _make_async_client(fake_post))
    result = await _invoke(max_tokens=42)

    assert result["content"][0]["type"] == "tool_use"
    assert captured["url"] == (
        "https://bedrock-runtime.eu-north-1.amazonaws.com/model/eu.anthropic.claude-sonnet-4-6/invoke"
    )
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    # Request body must include the forced tool_choice.
    assert captured["json"]["tools"] == [SUBMIT_VERDICT_ANTHROPIC_TOOL]
    assert captured["json"]["tool_choice"] == {"type": "tool", "name": "submit_verdict"}
    assert captured["json"]["max_tokens"] == 42


async def test_invoke_bedrock_judge_raises_on_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        judge_bedrock.httpx2,
        "AsyncClient",
        lambda: _make_async_client(lambda *a, **kw: _make_response(status_code=400, text='{"message":"bad model"}')),
    )
    with pytest.raises(JudgeInfrastructureError, match="Bedrock invoke failed: 400") as excinfo:
        await _invoke()
    assert "bad model" in str(excinfo.value)


async def test_invoke_bedrock_judge_raises_on_5xx(monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]) -> None:
    calls: list[int] = []

    def counting_post(*a: Any, **kw: Any) -> MagicMock:
        calls.append(1)
        return _make_response(status_code=500, text="upstream error")

    monkeypatch.setattr(judge_bedrock.httpx2, "AsyncClient", lambda: _make_async_client(counting_post))
    with pytest.raises(JudgeInfrastructureError, match="Bedrock invoke failed: 500"):
        await _invoke()
    # Exactly 1 initial call + max_retries retries — read from the constant, don't hardcode.
    assert len(calls) == judge_bedrock._JUDGE_RETRY.max_retries + 1
    assert len(no_sleep) == judge_bedrock._JUDGE_RETRY.max_retries


async def test_invoke_bedrock_judge_raises_on_non_dict_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        judge_bedrock.httpx2,
        "AsyncClient",
        lambda: _make_async_client(lambda *a, **kw: _make_response(json_data=["not a dict"])),
    )
    with pytest.raises(JudgeInfrastructureError, match="not a JSON object"):
        await _invoke()


async def test_invoke_bedrock_judge_raises_on_empty_model() -> None:
    with pytest.raises(ValueError):
        await invoke_bedrock_judge_async(
            route=_route(),
            model="",
            system="s",
            user="u",
            temperature=0.0,
            max_tokens=10,
            tool_spec=SUBMIT_VERDICT_ANTHROPIC_TOOL,
        )


async def test_invoke_bedrock_judge_wraps_transport_error(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    def raising_post(*a: Any, **kw: Any) -> MagicMock:
        raise httpx2.ConnectTimeout("connection timed out")

    monkeypatch.setattr(judge_bedrock.httpx2, "AsyncClient", lambda: _make_async_client(raising_post))
    with pytest.raises(JudgeInfrastructureError, match="Bedrock invoke transport error") as excinfo:
        await _invoke()
    assert "connection timed out" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, httpx2.ConnectTimeout)


async def test_invoke_bedrock_judge_strips_v1_suffix_in_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kw: Any) -> MagicMock:
        captured["url"] = url
        return _make_response(json_data=_tool_use_response())

    monkeypatch.setattr(judge_bedrock.httpx2, "AsyncClient", lambda: _make_async_client(fake_post))
    await _invoke(model="anthropic.claude-opus-4-6-v1")
    assert "/model/eu.anthropic.claude-opus-4-6/invoke" in captured["url"]


async def test_invoke_bedrock_judge_retries_429_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    responses = iter(
        [
            _make_response(status_code=429, text="throttled"),
            _make_response(status_code=429, text="throttled"),
            _make_response(status_code=200, json_data=_tool_use_response(score=0.9)),
        ]
    )
    calls: list[int] = []

    def sequenced_post(*a: Any, **kw: Any) -> MagicMock:
        calls.append(1)
        return next(responses)

    monkeypatch.setattr(judge_bedrock.httpx2, "AsyncClient", lambda: _make_async_client(sequenced_post))
    result = await _invoke()
    assert result["content"][0]["input"]["score"] == 0.9
    assert len(calls) == 3
    assert len(no_sleep) == 2


async def test_invoke_bedrock_judge_403_fails_immediately(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    calls: list[int] = []

    def counting_post(*a: Any, **kw: Any) -> MagicMock:
        calls.append(1)
        return _make_response(status_code=403, text="forbidden")

    monkeypatch.setattr(judge_bedrock.httpx2, "AsyncClient", lambda: _make_async_client(counting_post))
    with pytest.raises(JudgeInfrastructureError, match="Bedrock invoke failed: 403"):
        await _invoke()
    assert len(calls) == 1
    assert no_sleep == []


async def test_invoke_bedrock_judge_retries_connect_error_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    calls: list[int] = []

    def flaky_post(*a: Any, **kw: Any) -> MagicMock:
        calls.append(1)
        if len(calls) == 1:
            raise httpx2.ConnectError("connection refused")
        return _make_response(status_code=200, json_data=_tool_use_response())

    monkeypatch.setattr(judge_bedrock.httpx2, "AsyncClient", lambda: _make_async_client(flaky_post))
    result = await _invoke()
    assert result["content"][0]["type"] == "tool_use"
    assert len(calls) == 2
    assert len(no_sleep) == 1


async def test_invoke_bedrock_judge_malformed_json_body_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    import json as _json

    response = _make_response(status_code=200)
    response.json.side_effect = _json.JSONDecodeError("Expecting value", doc="", pos=0)
    monkeypatch.setattr(judge_bedrock.httpx2, "AsyncClient", lambda: _make_async_client(lambda *a, **kw: response))
    with pytest.raises(JudgeInfrastructureError, match="not valid JSON"):
        await _invoke()
