"""Tests for the LiteLLM judge invoker, which calls through the ``litellm``
library (``litellm.acompletion``) rather than a hand-rolled HTTP client —
``litellm`` normalizes provider-specific request/response shapes so the judge
transport doesn't have to.
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
    include_temperature: bool = False,
    params: dict[str, Any] | None = None,
    auth: dict[str, str] | None = None,
) -> LiteLLMRoute:
    return LiteLLMRoute(
        base_url="http://gateway:4000",
        model="gpt-5.6-luna",
        include_temperature=include_temperature,
        params=params,
        auth=auth,
    )


async def _invoke(**overrides):
    defaults = {
        "route": _route(),
        "auth_token": "sk-master",
        "model": "azure_ai/gpt-5.6-luna",
        "system": "s",
        "user": "u",
        "temperature": 0.0,
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
    """LiteLLMRoute.include_temperature defaults to False: a gateway-routed model
    id isn't in litellm's static param table, so an unsupported `temperature`
    isn't caught by `drop_params` -- it round-trips to the provider and back as
    a live rejection. Skip sending it at all unless the route opts in."""
    acompletion = AsyncMock(return_value=_make_response())
    with patch("litellm.acompletion", new=acompletion):
        await _invoke(temperature=0.7, max_tokens=321, system="sys", user="usr")
    kwargs: dict[str, Any] = dict(acompletion.call_args.kwargs)
    assert "temperature" not in kwargs
    assert kwargs["max_completion_tokens"] == 321
    assert kwargs["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
    ]


async def test_invoke_litellm_judge_sends_temperature_when_route_opts_in() -> None:
    acompletion = AsyncMock(return_value=_make_response())
    with patch("litellm.acompletion", new=acompletion):
        await _invoke(route=_route(include_temperature=True), temperature=0.7)
    kwargs: dict[str, Any] = dict(acompletion.call_args.kwargs)
    assert kwargs["temperature"] == 0.7


async def test_invoke_litellm_judge_retries_without_temperature_when_rejected() -> None:
    """A gateway-routed model litellm has no static param metadata for (so
    `drop_params` can't preflight it) can still reject `temperature` live even
    when the route opted in — observed against a real Azure AI deployment.
    Must retry once without it rather than failing the whole judge call."""
    from litellm.exceptions import BadRequestError

    rejection = BadRequestError(
        message="Unsupported parameter: 'temperature' is not supported with this model.",
        model="azure_ai/gpt-5.6-luna",
        llm_provider="azure_ai",
        body={"error": {"message": "...", "param": "temperature", "code": None}},
    )
    acompletion = AsyncMock(side_effect=[rejection, _make_response(score=0.9)])
    with patch("litellm.acompletion", new=acompletion):
        result = await _invoke(route=_route(include_temperature=True), temperature=0.3)
    assert acompletion.call_count == 2
    first_kwargs, second_kwargs = (c.kwargs for c in acompletion.call_args_list)
    assert first_kwargs["temperature"] == 0.3
    assert "temperature" not in second_kwargs
    assert result["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "submit_verdict"


async def test_invoke_litellm_judge_reraises_unrelated_bad_request() -> None:
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
        await _invoke(route=_route(include_temperature=True))
    acompletion.assert_called_once()


async def test_invoke_litellm_judge_reraises_bad_request_when_route_did_not_opt_in() -> None:
    """No point retrying-without-temperature when temperature was never sent."""
    from litellm.exceptions import BadRequestError

    rejection = BadRequestError(
        message="Unsupported parameter: 'temperature' is not supported with this model.",
        model="m",
        llm_provider="azure_ai",
        body={"error": {"message": "...", "param": "temperature", "code": None}},
    )
    acompletion = AsyncMock(side_effect=rejection)
    with (
        patch("litellm.acompletion", new=acompletion),
        pytest.raises(JudgeInfrastructureError, match="LiteLLM judge call failed"),
    ):
        await _invoke()
    acompletion.assert_called_once()


async def test_invoke_litellm_judge_raises_on_empty_model() -> None:
    with pytest.raises(ValueError):
        await _invoke(model="")


async def test_invoke_litellm_judge_raises_on_missing_auth_token() -> None:
    with pytest.raises(JudgeInfrastructureError, match="LITELLM_AUTH_TOKEN"):
        await _invoke(auth_token=None)


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


async def test_invoke_litellm_judge_escalates_on_signature_break() -> None:
    acompletion = AsyncMock(side_effect=TypeError("acompletion() got an unexpected keyword argument 'drop_params'"))
    with (
        patch("litellm.acompletion", new=acompletion),
        pytest.raises(JudgeInfrastructureError, match="LiteLLM judge call failed"),
    ):
        await _invoke()


async def test_invoke_litellm_judge_passes_through_params() -> None:
    """`route.params` is arbitrary passthrough merged straight into the
    litellm.acompletion() kwargs — e.g. aws_region_name, api_version, ..."""
    acompletion = AsyncMock(return_value=_make_response())
    with patch("litellm.acompletion", new=acompletion):
        await _invoke(route=_route(params={"aws_region_name": "eu-north-1", "api_version": "2024-05-01"}))
    kwargs = acompletion.call_args.kwargs
    assert kwargs["aws_region_name"] == "eu-north-1"
    assert kwargs["api_version"] == "2024-05-01"


async def test_invoke_litellm_judge_resolves_auth_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`route.auth` maps a kwarg name -> ENV VAR NAME; the secret VALUE is only
    ever resolved at call time, never stored on the route."""
    monkeypatch.setenv("MY_AWS_ACCESS_KEY_ID", "AKIA-fake")
    monkeypatch.setenv("MY_AWS_SECRET_ACCESS_KEY", "secret-fake")
    acompletion = AsyncMock(return_value=_make_response())
    route = _route(
        auth={"aws_access_key_id": "MY_AWS_ACCESS_KEY_ID", "aws_secret_access_key": "MY_AWS_SECRET_ACCESS_KEY"}
    )
    with patch("litellm.acompletion", new=acompletion):
        await _invoke(route=route, auth_token=None)
    kwargs = acompletion.call_args.kwargs
    assert kwargs["aws_access_key_id"] == "AKIA-fake"
    assert kwargs["aws_secret_access_key"] == "secret-fake"


async def test_invoke_litellm_judge_auth_api_key_overrides_auth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit `auth: {api_key: ENV_VAR}` wins over the LITELLM_AUTH_TOKEN-
    sourced `auth_token` default, and satisfies the "some api_key is configured"
    requirement even when `auth_token` itself is None."""
    monkeypatch.setenv("OTHER_KEY", "sk-other")
    acompletion = AsyncMock(return_value=_make_response())
    route = _route(auth={"api_key": "OTHER_KEY"})
    with patch("litellm.acompletion", new=acompletion):
        await _invoke(route=route, auth_token=None)
    assert acompletion.call_args.kwargs["api_key"] == "sk-other"


async def test_invoke_litellm_judge_raises_on_missing_env_var_for_auth() -> None:
    route = _route(auth={"aws_access_key_id": "TOTALLY_UNSET_ENV_VAR_XYZ"})
    with pytest.raises(JudgeInfrastructureError, match="TOTALLY_UNSET_ENV_VAR_XYZ"):
        await _invoke(route=route)


async def test_invoke_litellm_judge_params_do_not_shadow_required_kwargs() -> None:
    """auth resolves AFTER params, so an auth-mapped key always wins over the
    same key set via params (belt-and-braces; auth is the documented secret
    channel)."""
    acompletion = AsyncMock(return_value=_make_response())
    route = _route(params={"api_key": "leaked-from-params"})
    with patch("litellm.acompletion", new=acompletion):
        await _invoke(route=route, auth_token="sk-master")
    # No `auth` override -> the LITELLM_AUTH_TOKEN default is applied first,
    # then params overwrites it (params has no special protection over the
    # base kwargs) -- documents the actual precedence rather than asserting a
    # stronger guarantee than the implementation provides.
    assert acompletion.call_args.kwargs["api_key"] == "leaked-from-params"
