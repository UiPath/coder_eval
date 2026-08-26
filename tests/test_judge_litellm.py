"""Tests for the LiteLLM judge invoker, which calls through the ``litellm``
library (``litellm.acompletion``) rather than a hand-rolled HTTP client —
``litellm`` normalizes provider-specific request/response shapes so the judge
transport doesn't have to. Unlike the agent's own LiteLLM backend, this path
reads NOTHING from ``coder_eval.config.settings`` — everything comes from
``route.params``/``route.env_params``.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coder_eval.errors import JudgeInfrastructureError
from coder_eval.evaluation.judge_litellm import invoke_litellm_judge_async
from coder_eval.evaluation.verdict_tool import SUBMIT_VERDICT_ANTHROPIC_TOOL
from coder_eval.models.routing import LiteLLMRoute


def _make_response(*, score: float = 0.5, rationale: str = "ok") -> MagicMock:
    """Mimic litellm's OpenAI-shaped ``ModelResponse``: ``model_dump()`` returns
    an OpenAI Chat-Completions-native tool_calls dict, regardless of the
    underlying provider litellm actually routed to.

    ``spec=ModelResponse`` so ``invoke_litellm_judge_async``'s defensive
    ``isinstance(response, ModelResponse)`` guard (against the
    ``ModelResponse | CustomStreamWrapper`` union ``acompletion`` is typed to
    return) passes against the mock the same as it would the real object."""
    import json

    from litellm.types.utils import ModelResponse

    response = MagicMock(spec=ModelResponse)
    response.model_dump.return_value = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "submit_verdict",
                                "arguments": json.dumps({"score": score, "rationale": rationale}),
                            }
                        }
                    ]
                }
            }
        ]
    }
    return response


def _route(
    *,
    params: dict[str, Any] | None = None,
    env_params: dict[str, str] | None = None,
) -> LiteLLMRoute:
    return LiteLLMRoute(model="gpt-5.6-luna", params=params, env_params=env_params)


async def _invoke(**overrides):
    defaults = {
        "route": _route(params={"api_base": "http://gateway:4000", "api_key": "sk-master"}),
        "model": "azure_ai/gpt-5.6-luna",
        "system": "s",
        "user": "u",
        "max_tokens": 10,
        "tool_spec": SUBMIT_VERDICT_ANTHROPIC_TOOL,
    }
    defaults.update(overrides)
    return await invoke_litellm_judge_async(**defaults)


async def test_invoke_litellm_judge_calls_acompletion() -> None:
    acompletion = AsyncMock(return_value=_make_response(score=0.42))
    with patch("litellm.acompletion", new=acompletion):
        result = await _invoke()
    acompletion.assert_called_once()
    kwargs = acompletion.call_args.kwargs
    # Model id travels verbatim, provider prefix and all -- litellm routes on it.
    assert kwargs["model"] == "azure_ai/gpt-5.6-luna"
    assert kwargs["api_base"] == "http://gateway:4000"
    assert kwargs["api_key"] == "sk-master"
    assert kwargs["drop_params"] is True
    assert kwargs["tools"][0]["function"]["name"] == "submit_verdict"
    assert kwargs["tool_choice"] == {"type": "function", "function": {"name": "submit_verdict"}}
    assert result["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "submit_verdict"


async def test_invoke_litellm_judge_omits_temperature_by_default() -> None:
    """Unlike invoke_anthropic_judge_async/invoke_bedrock_judge_async, there is no
    `temperature` parameter at all here — a gateway-routed model may reject it
    outright (observed live against an Azure AI deployment), so the task author
    opts in via `params: {temperature: ...}` if their model accepts it."""
    acompletion = AsyncMock(return_value=_make_response())
    with patch("litellm.acompletion", new=acompletion):
        await _invoke(max_tokens=321, system="sys", user="usr")
    kwargs: dict[str, Any] = dict(acompletion.call_args.kwargs)
    assert "temperature" not in kwargs
    assert kwargs["max_completion_tokens"] == 321
    assert kwargs["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
    ]


async def test_invoke_litellm_judge_sends_temperature_via_params() -> None:
    acompletion = AsyncMock(return_value=_make_response())
    with patch("litellm.acompletion", new=acompletion):
        await _invoke(route=_route(params={"temperature": 0.7}))
    assert acompletion.call_args.kwargs["temperature"] == 0.7


async def test_invoke_litellm_judge_passes_through_params() -> None:
    """`route.params` is arbitrary passthrough merged straight into the
    litellm.acompletion() kwargs — e.g. api_base, aws_region_name, ..."""
    acompletion = AsyncMock(return_value=_make_response())
    with patch("litellm.acompletion", new=acompletion):
        await _invoke(route=_route(params={"aws_region_name": "eu-north-1", "api_version": "2024-05-01"}))
    kwargs = acompletion.call_args.kwargs
    assert kwargs["aws_region_name"] == "eu-north-1"
    assert kwargs["api_version"] == "2024-05-01"


async def test_invoke_litellm_judge_resolves_env_params_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`route.env_params` maps a kwarg name -> ENV VAR NAME; the value is only
    ever resolved at call time, never stored on the route."""
    monkeypatch.setenv("MY_AWS_ACCESS_KEY_ID", "AKIA-fake")
    monkeypatch.setenv("MY_AWS_SECRET_ACCESS_KEY", "secret-fake")
    acompletion = AsyncMock(return_value=_make_response())
    route = _route(
        env_params={
            "aws_access_key_id": "MY_AWS_ACCESS_KEY_ID",
            "aws_secret_access_key": "MY_AWS_SECRET_ACCESS_KEY",
        }
    )
    with patch("litellm.acompletion", new=acompletion):
        await _invoke(route=route)
    kwargs = acompletion.call_args.kwargs
    assert kwargs["aws_access_key_id"] == "AKIA-fake"
    assert kwargs["aws_secret_access_key"] == "secret-fake"


async def test_invoke_litellm_judge_env_params_override_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """`env_params` is resolved AFTER `params`, so it always wins for the same key."""
    monkeypatch.setenv("REAL_KEY", "sk-real")
    acompletion = AsyncMock(return_value=_make_response())
    route = _route(params={"api_key": "sk-literal-in-yaml"}, env_params={"api_key": "REAL_KEY"})
    with patch("litellm.acompletion", new=acompletion):
        await _invoke(route=route)
    assert acompletion.call_args.kwargs["api_key"] == "sk-real"


async def test_invoke_litellm_judge_raises_on_missing_env_var() -> None:
    route = _route(env_params={"aws_access_key_id": "TOTALLY_UNSET_ENV_VAR_XYZ"})
    with pytest.raises(JudgeInfrastructureError, match="TOTALLY_UNSET_ENV_VAR_XYZ"):
        await _invoke(route=route)


async def test_invoke_litellm_judge_raises_on_empty_model() -> None:
    with pytest.raises(ValueError, match="model must not be empty"):
        await _invoke(model="")


async def test_invoke_litellm_judge_raises_when_library_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "litellm", None)
    with pytest.raises(JudgeInfrastructureError, match=r"pip install 'coder-eval\[litellm\]'"):
        await _invoke()


async def test_invoke_litellm_judge_wraps_api_error() -> None:
    from litellm.exceptions import APIError

    acompletion = AsyncMock(side_effect=APIError(status_code=500, message="boom", llm_provider="azure_ai", model="m"))
    with (
        patch("litellm.acompletion", new=acompletion),
        pytest.raises(JudgeInfrastructureError, match="LiteLLM judge API error"),
    ):
        await _invoke()


async def test_invoke_litellm_judge_wraps_bad_request_error() -> None:
    from litellm.exceptions import BadRequestError

    rejection = BadRequestError(
        message="Unsupported parameter: 'foo'.",
        model="m",
        llm_provider="azure_ai",
        body={"error": {"message": "...", "param": "foo", "code": None}},
    )
    acompletion = AsyncMock(side_effect=rejection)
    with (
        patch("litellm.acompletion", new=acompletion),
        pytest.raises(JudgeInfrastructureError, match="LiteLLM judge call failed"),
    ):
        await _invoke()
    acompletion.assert_called_once()


async def test_invoke_litellm_judge_escalates_on_signature_break() -> None:
    acompletion = AsyncMock(side_effect=TypeError("acompletion() got an unexpected keyword argument 'drop_params'"))
    with (
        patch("litellm.acompletion", new=acompletion),
        pytest.raises(JudgeInfrastructureError, match="LiteLLM judge call failed"),
    ):
        await _invoke()
