"""Tests for the Bedrock invoke helper used by judge-style criteria.

The helper issues a forced ``submit_verdict`` tool call and returns the parsed
response dict so the caller can walk ``content`` for ``tool_use`` blocks.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from coder_eval.errors import JudgeInfrastructureError
from coder_eval.evaluation import judge_bedrock
from coder_eval.evaluation.judge_bedrock import invoke_bedrock_judge
from coder_eval.evaluation.verdict_tool import SUBMIT_VERDICT_ANTHROPIC_TOOL
from coder_eval.models.routing import BedrockRoute


def _make_response(*, status_code: int = 200, json_data: Any = None, text: str = "") -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data if json_data is not None else {}
    response.text = text
    return response


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Capture backoff sleeps instead of actually sleeping."""
    sleeps: list[float] = []
    monkeypatch.setattr(judge_bedrock.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


def _route() -> BedrockRoute:
    return BedrockRoute(bearer_token="test-token", region="eu-north-1")


def _tool_use_response(score: float = 0.5, rationale: str = "ok") -> dict[str, Any]:
    return {
        "content": [
            {"type": "tool_use", "name": "submit_verdict", "input": {"score": score, "rationale": rationale}},
        ]
    }


def _invoke(monkeypatch_post, **overrides):  # convenience wrapper
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
    return invoke_bedrock_judge(**defaults)


def test_invoke_bedrock_judge_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float) -> MagicMock:
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _make_response(status_code=200, json_data=_tool_use_response(score=0.5))

    monkeypatch.setattr(judge_bedrock.httpx, "post", fake_post)
    result = _invoke(monkeypatch, max_tokens=42)

    assert result["content"][0]["type"] == "tool_use"
    assert captured["url"] == (
        "https://bedrock-runtime.eu-north-1.amazonaws.com/model/eu.anthropic.claude-sonnet-4-6/invoke"
    )
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    # Request body must include the forced tool_choice.
    assert captured["json"]["tools"] == [SUBMIT_VERDICT_ANTHROPIC_TOOL]
    assert captured["json"]["tool_choice"] == {"type": "tool", "name": "submit_verdict"}
    assert captured["json"]["max_tokens"] == 42


def test_invoke_bedrock_judge_raises_on_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        judge_bedrock.httpx,
        "post",
        lambda *a, **kw: _make_response(status_code=400, text='{"message":"bad model"}'),
    )
    with pytest.raises(JudgeInfrastructureError, match="Bedrock invoke failed: 400") as excinfo:
        _invoke(monkeypatch)
    assert "bad model" in str(excinfo.value)


def test_invoke_bedrock_judge_raises_on_5xx(monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]) -> None:
    calls: list[int] = []

    def counting_post(*a: Any, **kw: Any) -> MagicMock:
        calls.append(1)
        return _make_response(status_code=500, text="upstream error")

    monkeypatch.setattr(judge_bedrock.httpx, "post", counting_post)
    with pytest.raises(JudgeInfrastructureError, match="Bedrock invoke failed: 500"):
        _invoke(monkeypatch)
    # Exactly 1 initial call + max_retries retries — read from the constant, don't hardcode.
    assert len(calls) == judge_bedrock._JUDGE_RETRY.max_retries + 1
    assert len(no_sleep) == judge_bedrock._JUDGE_RETRY.max_retries


def test_invoke_bedrock_judge_raises_on_non_dict_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(judge_bedrock.httpx, "post", lambda *a, **kw: _make_response(json_data=["not a dict"]))
    with pytest.raises(JudgeInfrastructureError, match="not a JSON object"):
        _invoke(monkeypatch)


def test_invoke_bedrock_judge_raises_on_empty_model() -> None:
    with pytest.raises(ValueError):
        invoke_bedrock_judge(
            route=_route(),
            model="",
            system="s",
            user="u",
            temperature=0.0,
            max_tokens=10,
            tool_spec=SUBMIT_VERDICT_ANTHROPIC_TOOL,
        )


def test_invoke_bedrock_judge_wraps_transport_error(monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]) -> None:
    import httpx as _httpx

    def raising_post(*a: Any, **kw: Any) -> MagicMock:
        raise _httpx.ConnectTimeout("connection timed out")

    monkeypatch.setattr(judge_bedrock.httpx, "post", raising_post)
    with pytest.raises(JudgeInfrastructureError, match="Bedrock invoke transport error") as excinfo:
        _invoke(monkeypatch)
    assert "connection timed out" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, _httpx.ConnectTimeout)


def test_invoke_bedrock_judge_strips_v1_suffix_in_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kw: Any) -> MagicMock:
        captured["url"] = url
        return _make_response(json_data=_tool_use_response())

    monkeypatch.setattr(judge_bedrock.httpx, "post", fake_post)
    _invoke(monkeypatch, model="anthropic.claude-opus-4-6-v1")
    assert "/model/eu.anthropic.claude-opus-4-6/invoke" in captured["url"]


def test_invoke_bedrock_judge_retries_429_then_succeeds(monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]) -> None:
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

    monkeypatch.setattr(judge_bedrock.httpx, "post", sequenced_post)
    result = _invoke(monkeypatch)
    assert result["content"][0]["input"]["score"] == 0.9
    assert len(calls) == 3
    assert len(no_sleep) == 2


def test_invoke_bedrock_judge_403_fails_immediately(monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]) -> None:
    calls: list[int] = []

    def counting_post(*a: Any, **kw: Any) -> MagicMock:
        calls.append(1)
        return _make_response(status_code=403, text="forbidden")

    monkeypatch.setattr(judge_bedrock.httpx, "post", counting_post)
    with pytest.raises(JudgeInfrastructureError, match="Bedrock invoke failed: 403"):
        _invoke(monkeypatch)
    assert len(calls) == 1
    assert no_sleep == []


def test_invoke_bedrock_judge_retries_connect_error_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    import httpx as _httpx

    calls: list[int] = []

    def flaky_post(*a: Any, **kw: Any) -> MagicMock:
        calls.append(1)
        if len(calls) == 1:
            raise _httpx.ConnectError("connection refused")
        return _make_response(status_code=200, json_data=_tool_use_response())

    monkeypatch.setattr(judge_bedrock.httpx, "post", flaky_post)
    result = _invoke(monkeypatch)
    assert result["content"][0]["type"] == "tool_use"
    assert len(calls) == 2
    assert len(no_sleep) == 1


def test_invoke_bedrock_judge_malformed_json_body_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    import json as _json

    response = _make_response(status_code=200)
    response.json.side_effect = _json.JSONDecodeError("Expecting value", doc="", pos=0)
    monkeypatch.setattr(judge_bedrock.httpx, "post", lambda *a, **kw: response)
    with pytest.raises(JudgeInfrastructureError, match="not valid JSON"):
        _invoke(monkeypatch)
