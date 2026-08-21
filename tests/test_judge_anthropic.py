"""Tests for the Anthropic SDK invoke helper used by judge-style criteria.

The helper issues a forced ``submit_verdict`` tool call and returns the parsed
response dict so the caller can walk ``content`` for ``tool_use`` blocks.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from anthropic.resources.messages import AsyncMessages

from coder_eval.evaluation.judge_anthropic import invoke_anthropic_judge_async
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
    # spec-bound to the real ``AsyncMessages.create`` signature so a kwarg the
    # installed SDK no longer accepts (e.g. a removed ``temperature``) fails
    # here instead of silently passing against an unconstrained MagicMock.
    client.messages = MagicMock(spec=AsyncMessages)
    client.messages.create = AsyncMock(
        wraps=lambda **kwargs: _bind_and_return(response if response is not None else _make_response(), **kwargs)
    )
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def _bind_and_return(response: MagicMock, **kwargs: Any) -> MagicMock:
    import inspect

    inspect.signature(AsyncMessages.create).bind(MagicMock(), **kwargs)
    return response


async def _invoke(**overrides):
    defaults = {
        "model": "anthropic.claude-sonnet-4-6",
        "system": "s",
        "user": "u",
        "temperature": 0.0,
        "max_tokens": 10,
        "tool_spec": SUBMIT_VERDICT_ANTHROPIC_TOOL,
    }
    defaults.update(overrides)
    return await invoke_anthropic_judge_async(**defaults)


async def test_invoke_anthropic_judge_direct_uses_default_client() -> None:
    client = _make_client(_make_response(score=0.42))
    with patch("coder_eval.evaluation.judge_anthropic.AsyncAnthropic", return_value=client) as ctor:
        result = await _invoke()
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


async def test_invoke_anthropic_judge_strips_v1_suffix() -> None:
    client = _make_client()
    with patch("coder_eval.evaluation.judge_anthropic.AsyncAnthropic", return_value=client):
        await _invoke(model="anthropic.claude-opus-4-6-v1")
    assert client.messages.create.call_args.kwargs["model"] == "claude-opus-4-6"


async def test_invoke_anthropic_judge_raises_on_empty_model() -> None:
    with pytest.raises(ValueError):
        await _invoke(model="")


async def test_invoke_anthropic_judge_passes_temperature_and_max_tokens() -> None:
    """``temperature`` travels via ``extra_body`` — anthropic 1.0.0 dropped it as a
    top-level ``messages.create`` kwarg, but the Messages API still accepts it in
    the raw JSON body (the same body shape the Bedrock path sends it in)."""
    client = _make_client()
    with patch("coder_eval.evaluation.judge_anthropic.AsyncAnthropic", return_value=client):
        await _invoke(temperature=0.7, max_tokens=321, system="sys", user="usr")
    kwargs: dict[str, Any] = client.messages.create.call_args.kwargs
    assert "temperature" not in kwargs
    assert kwargs["extra_body"] == {"temperature": 0.7}
    assert kwargs["max_tokens"] == 321
    assert kwargs["system"] == "sys"
    assert kwargs["messages"] == [{"role": "user", "content": "usr"}]


async def test_invoke_anthropic_judge_escalates_on_signature_break() -> None:
    """A kwarg the installed SDK no longer accepts must escalate as infra, not
    silently score the row 0.0 (see judge_bedrock.py's parallel retry/escalation
    contract and CLAUDE.md's CE039 rationale)."""
    from coder_eval.errors import JudgeInfrastructureError

    client = _make_client()
    client.messages.create.side_effect = TypeError("create() got an unexpected keyword argument 'temperature'")
    with (
        patch("coder_eval.evaluation.judge_anthropic.AsyncAnthropic", return_value=client),
        pytest.raises(JudgeInfrastructureError, match="Anthropic judge call failed"),
    ):
        await _invoke()


async def test_invoke_anthropic_judge_wraps_api_error() -> None:
    import httpx2
    from anthropic import APIConnectionError

    from coder_eval.errors import JudgeInfrastructureError

    client = _make_client()
    sdk_error = APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))
    client.messages.create.side_effect = sdk_error
    with (
        patch("coder_eval.evaluation.judge_anthropic.AsyncAnthropic", return_value=client),
        pytest.raises(JudgeInfrastructureError, match="Anthropic judge API error") as excinfo,
    ):
        await _invoke()
    assert excinfo.value.__cause__ is sdk_error
