"""Tests for the Bedrock invoke helper used by judge-style criteria."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from coder_eval.evaluation import judge_bedrock
from coder_eval.evaluation.judge_bedrock import invoke_bedrock_judge
from coder_eval.models.routing import BedrockRoute


def _make_response(*, status_code: int = 200, json_data: Any = None, text: str = "") -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data if json_data is not None else {}
    response.text = text
    return response


def _route() -> BedrockRoute:
    return BedrockRoute(bearer_token="test-token", region="eu-north-1")


def test_invoke_bedrock_judge_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float) -> MagicMock:
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _make_response(status_code=200, json_data={"content": [{"type": "text", "text": "verdict"}]})

    monkeypatch.setattr(judge_bedrock.httpx, "post", fake_post)

    result = invoke_bedrock_judge(
        route=_route(),
        model="anthropic.claude-sonnet-4-6",
        system="s",
        user="u",
        temperature=0.0,
        max_tokens=42,
    )

    assert result == "verdict"
    assert captured["url"] == (
        "https://bedrock-runtime.eu-north-1.amazonaws.com/model/eu.anthropic.claude-sonnet-4-6/invoke"
    )
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["json"] == {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 42,
        "temperature": 0.0,
        "system": "s",
        "messages": [{"role": "user", "content": "u"}],
    }


def test_invoke_bedrock_judge_concatenates_multiple_text_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        judge_bedrock.httpx,
        "post",
        lambda *a, **kw: _make_response(
            json_data={
                "content": [
                    {"type": "text", "text": "alpha"},
                    {"type": "text", "text": "-beta"},
                ]
            }
        ),
    )
    assert (
        invoke_bedrock_judge(
            route=_route(), model="anthropic.claude-sonnet-4-6", system="s", user="u", temperature=0.0, max_tokens=10
        )
        == "alpha-beta"
    )


def test_invoke_bedrock_judge_raises_on_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        judge_bedrock.httpx,
        "post",
        lambda *a, **kw: _make_response(status_code=400, text='{"message":"bad model"}'),
    )
    with pytest.raises(RuntimeError, match="Bedrock invoke failed: 400") as excinfo:
        invoke_bedrock_judge(
            route=_route(), model="anthropic.claude-sonnet-4-6", system="s", user="u", temperature=0.0, max_tokens=10
        )
    assert "bad model" in str(excinfo.value)


def test_invoke_bedrock_judge_raises_on_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        judge_bedrock.httpx,
        "post",
        lambda *a, **kw: _make_response(status_code=500, text="upstream error"),
    )
    with pytest.raises(RuntimeError, match="Bedrock invoke failed: 500"):
        invoke_bedrock_judge(
            route=_route(), model="anthropic.claude-sonnet-4-6", system="s", user="u", temperature=0.0, max_tokens=10
        )


def test_invoke_bedrock_judge_raises_on_no_text_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        judge_bedrock.httpx,
        "post",
        lambda *a, **kw: _make_response(
            json_data={"content": [{"type": "tool_use", "id": "x", "name": "foo", "input": {}}]}
        ),
    )
    with pytest.raises(RuntimeError, match="Bedrock returned no text content"):
        invoke_bedrock_judge(
            route=_route(), model="anthropic.claude-sonnet-4-6", system="s", user="u", temperature=0.0, max_tokens=10
        )


def test_invoke_bedrock_judge_raises_on_missing_content_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(judge_bedrock.httpx, "post", lambda *a, **kw: _make_response(json_data={}))
    with pytest.raises(RuntimeError, match="missing 'content' list"):
        invoke_bedrock_judge(
            route=_route(), model="anthropic.claude-sonnet-4-6", system="s", user="u", temperature=0.0, max_tokens=10
        )


def test_invoke_bedrock_judge_raises_on_empty_model() -> None:
    with pytest.raises(ValueError):
        invoke_bedrock_judge(route=_route(), model="", system="s", user="u", temperature=0.0, max_tokens=10)


def test_invoke_bedrock_judge_wraps_transport_error_in_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx as _httpx

    def raising_post(*a: Any, **kw: Any) -> MagicMock:
        raise _httpx.ConnectTimeout("connection timed out")

    monkeypatch.setattr(judge_bedrock.httpx, "post", raising_post)
    with pytest.raises(RuntimeError, match="Bedrock invoke transport error") as excinfo:
        invoke_bedrock_judge(
            route=_route(), model="anthropic.claude-sonnet-4-6", system="s", user="u", temperature=0.0, max_tokens=10
        )
    assert "connection timed out" in str(excinfo.value)


def test_invoke_bedrock_judge_strips_v1_suffix_in_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kw: Any) -> MagicMock:
        captured["url"] = url
        return _make_response(json_data={"content": [{"type": "text", "text": "ok"}]})

    monkeypatch.setattr(judge_bedrock.httpx, "post", fake_post)
    invoke_bedrock_judge(
        route=_route(),
        model="anthropic.claude-opus-4-6-v1",
        system="s",
        user="u",
        temperature=0.0,
        max_tokens=10,
    )
    assert "/model/eu.anthropic.claude-opus-4-6/invoke" in captured["url"]
