"""Tests for the Anthropic SDK invoke helper used by judge-style criteria."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from coder_eval.evaluation.judge_anthropic import invoke_anthropic_judge
from coder_eval.models.routing import DirectRoute, ProxyRoute


def _make_response(text_blocks: list[str]) -> MagicMock:
    response = MagicMock()
    response.content = [MagicMock(type="text", text=t) for t in text_blocks]
    return response


def _make_client(response: MagicMock | None = None) -> MagicMock:
    client = MagicMock()
    client.messages.create.return_value = response if response is not None else _make_response(["ok"])
    return client


def test_invoke_anthropic_judge_direct_uses_default_client() -> None:
    client = _make_client(_make_response(["verdict"]))
    with patch("coder_eval.evaluation.judge_anthropic.Anthropic", return_value=client) as ctor:
        result = invoke_anthropic_judge(
            route=DirectRoute(),
            model="anthropic.claude-sonnet-4-6",
            system="s",
            user="u",
            temperature=0.0,
            max_tokens=42,
        )
    assert result == "verdict"
    # DirectRoute path: no base_url, no api_key — env-driven.
    ctor.assert_called_once()
    kwargs = ctor.call_args.kwargs
    assert "base_url" not in kwargs
    assert "api_key" not in kwargs
    assert kwargs["timeout"] == 120.0
    create_kwargs = client.messages.create.call_args.kwargs
    assert create_kwargs["model"] == "claude-sonnet-4-6"


def test_invoke_anthropic_judge_proxy_uses_local_base_url() -> None:
    client = _make_client()
    with patch("coder_eval.evaluation.judge_anthropic.Anthropic", return_value=client) as ctor:
        invoke_anthropic_judge(
            route=ProxyRoute(port=12345),
            model="anthropic.claude-sonnet-4-6",
            system="s",
            user="u",
            temperature=0.0,
            max_tokens=10,
        )
    kwargs = ctor.call_args.kwargs
    assert kwargs["base_url"] == "http://127.0.0.1:12345"
    assert kwargs["api_key"] == "llmgw-proxy"


def test_invoke_anthropic_judge_strips_v1_suffix() -> None:
    client = _make_client()
    with patch("coder_eval.evaluation.judge_anthropic.Anthropic", return_value=client):
        invoke_anthropic_judge(
            route=DirectRoute(),
            model="anthropic.claude-opus-4-6-v1",
            system="s",
            user="u",
            temperature=0.0,
            max_tokens=10,
        )
    assert client.messages.create.call_args.kwargs["model"] == "claude-opus-4-6"


def test_invoke_anthropic_judge_returns_text() -> None:
    client = _make_client(_make_response(["hello"]))
    with patch("coder_eval.evaluation.judge_anthropic.Anthropic", return_value=client):
        result = invoke_anthropic_judge(
            route=DirectRoute(),
            model="anthropic.claude-sonnet-4-6",
            system="s",
            user="u",
            temperature=0.0,
            max_tokens=10,
        )
    assert result == "hello"


def test_invoke_anthropic_judge_concatenates_text_blocks() -> None:
    client = _make_client(_make_response(["alpha", "-beta"]))
    with patch("coder_eval.evaluation.judge_anthropic.Anthropic", return_value=client):
        result = invoke_anthropic_judge(
            route=DirectRoute(),
            model="anthropic.claude-sonnet-4-6",
            system="s",
            user="u",
            temperature=0.0,
            max_tokens=10,
        )
    assert result == "alpha-beta"


def test_invoke_anthropic_judge_raises_on_no_text_blocks() -> None:
    response = MagicMock()
    response.content = [MagicMock(type="tool_use", text="not used")]
    client = _make_client(response)
    with (
        patch("coder_eval.evaluation.judge_anthropic.Anthropic", return_value=client),
        pytest.raises(RuntimeError, match="Anthropic API returned no text content"),
    ):
        invoke_anthropic_judge(
            route=DirectRoute(),
            model="anthropic.claude-sonnet-4-6",
            system="s",
            user="u",
            temperature=0.0,
            max_tokens=10,
        )


def test_invoke_anthropic_judge_raises_on_empty_model() -> None:
    with pytest.raises(ValueError):
        invoke_anthropic_judge(
            route=DirectRoute(),
            model="",
            system="s",
            user="u",
            temperature=0.0,
            max_tokens=10,
        )


def test_invoke_anthropic_judge_passes_temperature_and_max_tokens() -> None:
    client = _make_client()
    with patch("coder_eval.evaluation.judge_anthropic.Anthropic", return_value=client):
        invoke_anthropic_judge(
            route=DirectRoute(),
            model="anthropic.claude-sonnet-4-6",
            system="sys",
            user="usr",
            temperature=0.7,
            max_tokens=321,
        )
    kwargs: dict[str, Any] = client.messages.create.call_args.kwargs
    assert kwargs["temperature"] == 0.7
    assert kwargs["max_tokens"] == 321
    assert kwargs["system"] == "sys"
    assert kwargs["messages"] == [{"role": "user", "content": "usr"}]
