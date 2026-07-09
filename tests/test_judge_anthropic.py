"""Tests for the Anthropic SDK invoke helper used by judge-style criteria.

The helper issues a forced ``submit_verdict`` tool call and returns the parsed
response dict so the caller can walk ``content`` for ``tool_use`` blocks.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from coder_eval.evaluation.judge_anthropic import invoke_anthropic_judge
from coder_eval.evaluation.verdict_tool import SUBMIT_VERDICT_ANTHROPIC_TOOL


def _make_response(*, score: float = 0.5, rationale: str = "ok") -> MagicMock:
    """Mimic the Anthropic SDK ``Message`` Pydantic model: ``model_dump()`` returns the
    Anthropic-native content-block dict."""
    response = MagicMock()
    response.model_dump.return_value = {
        "content": [
            {"type": "tool_use", "name": "submit_verdict", "input": {"score": score, "rationale": rationale}},
        ]
    }
    return response


def _make_client(response: MagicMock | None = None) -> MagicMock:
    client = MagicMock()
    client.messages.create.return_value = response if response is not None else _make_response()
    return client


def _invoke(**overrides):
    defaults = {
        "model": "anthropic.claude-sonnet-4-6",
        "system": "s",
        "user": "u",
        "temperature": 0.0,
        "max_tokens": 10,
        "tool_spec": SUBMIT_VERDICT_ANTHROPIC_TOOL,
    }
    defaults.update(overrides)
    return invoke_anthropic_judge(**defaults)


def test_invoke_anthropic_judge_direct_uses_default_client() -> None:
    client = _make_client(_make_response(score=0.42))
    with patch("coder_eval.evaluation.judge_anthropic.Anthropic", return_value=client) as ctor:
        result = _invoke()
    # DirectRoute path: no base_url, no api_key — env-driven.
    ctor.assert_called_once()
    kwargs = ctor.call_args.kwargs
    assert "base_url" not in kwargs
    assert "api_key" not in kwargs
    assert kwargs["timeout"] == 120.0
    create_kwargs = client.messages.create.call_args.kwargs
    assert create_kwargs["model"] == "claude-sonnet-4-6"
    assert create_kwargs["tools"] == [SUBMIT_VERDICT_ANTHROPIC_TOOL]
    assert create_kwargs["tool_choice"] == {"type": "tool", "name": "submit_verdict"}
    # Return shape is dict-like via Message.model_dump()
    assert result["content"][0]["type"] == "tool_use"


def test_invoke_anthropic_judge_strips_v1_suffix() -> None:
    client = _make_client()
    with patch("coder_eval.evaluation.judge_anthropic.Anthropic", return_value=client):
        _invoke(model="anthropic.claude-opus-4-6-v1")
    assert client.messages.create.call_args.kwargs["model"] == "claude-opus-4-6"


def test_invoke_anthropic_judge_raises_on_empty_model() -> None:
    with pytest.raises(ValueError):
        _invoke(model="")


def test_invoke_anthropic_judge_passes_temperature_and_max_tokens() -> None:
    client = _make_client()
    with patch("coder_eval.evaluation.judge_anthropic.Anthropic", return_value=client):
        _invoke(temperature=0.7, max_tokens=321, system="sys", user="usr")
    kwargs: dict[str, Any] = client.messages.create.call_args.kwargs
    assert kwargs["temperature"] == 0.7
    assert kwargs["max_tokens"] == 321
    assert kwargs["system"] == "sys"
    assert kwargs["messages"] == [{"role": "user", "content": "usr"}]


def test_invoke_anthropic_judge_wraps_api_error() -> None:
    import httpx
    from anthropic import APIConnectionError

    from coder_eval.errors import JudgeInfrastructureError

    client = _make_client()
    sdk_error = APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    client.messages.create.side_effect = sdk_error
    with (
        patch("coder_eval.evaluation.judge_anthropic.Anthropic", return_value=client),
        pytest.raises(JudgeInfrastructureError, match="Anthropic judge API error") as excinfo,
    ):
        _invoke()
    assert excinfo.value.__cause__ is sdk_error
