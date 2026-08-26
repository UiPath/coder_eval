"""Single-completion invoker for the LiteLLM judge backend, via the ``litellm``
library (the ``coder-eval[litellm]`` extra) rather than a hand-rolled HTTP call.

``LiteLLMRoute``'s docstring frames it as an Anthropic-compatible proxy — true
for the AGENT side (``ClaudeCodeAgent`` points ``ANTHROPIC_BASE_URL`` at the
local ``litellm/start-litellm.sh`` proxy). The checker side reuses the same
route/env vars (``LITELLM_BASE_URL``/``LITELLM_AUTH_TOKEN``) for a different
purpose: task authors point this at whatever gateway their judge model lives
behind (an Azure AI ``/openai/v1`` deployment, a multi-model marketplace, ...),
which is rarely that same Anthropic-passthrough proxy. Calling through
``litellm.acompletion`` — rather than assuming one specific wire protocol —
lets ``model`` carry its own provider hint (e.g. ``azure_ai/gpt-5.6-luna``)
and get that provider's actual request/response shape handled by the library,
including per-provider quirks (Azure AI's ``api_base``/``api_key`` shape,
``max_tokens`` vs ``max_completion_tokens`` naming, unsupported-parameter
drops via ``drop_params``) instead of this module hand-coding them.

``litellm.acompletion`` always returns an OpenAI-shaped ``ModelResponse``
regardless of the underlying provider, so the caller reuses
``extract_verdict_from_openai_response``/``token_usage_from_openai_dict``
unchanged.

Async on purpose: mirrors ``invoke_anthropic_judge_async`` /
``invoke_bedrock_judge_async`` — the judge's only network call, no sync twin.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from coder_eval.errors import JudgeInfrastructureError
from coder_eval.models import LiteLLMRoute


logger = logging.getLogger(__name__)


async def invoke_litellm_judge_async(
    *,
    route: LiteLLMRoute,
    auth_token: str | None,
    model: str,
    system: str,
    user: str,
    temperature: float,
    max_tokens: int,
    tool_spec: dict[str, Any],
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """One completion call via ``litellm.acompletion`` with a forced tool call.

    Returns the OpenAI-shaped response converted to a dict via ``model_dump``
    so the caller can reuse ``extract_verdict_from_openai_response``.

    Raises:
        ValueError: ``model`` empty.
        JudgeInfrastructureError: the ``litellm`` extra isn't installed; no
            auth token configured; or the call fails (an eval-infra fault,
            not the agent's fault — CE039).
    """
    if not model:
        raise ValueError("invoke_litellm_judge_async: model must not be empty")
    # Raise (not assert): this call runs inside LLMJudgeChecker's
    # handle_criterion_errors(_async) wrapper, which catches plain Exception
    # (including AssertionError) and downgrades it to a scored 0.0 — the
    # opposite of the intended "internal-contract violation escalates to
    # FinalStatus.ERROR" behavior.
    #
    # `route.auth` can supply ITS OWN auth entirely — a provider that doesn't
    # use `api_key` at all (e.g. Bedrock's aws_access_key_id/
    # aws_secret_access_key) has no reason to also need LITELLM_AUTH_TOKEN —
    # so the plain LITELLM_AUTH_TOKEN requirement only applies when `auth` is
    # empty too. `_resolve_backend_route` already enforces the same relaxed
    # check at resolution time; this is the runtime backstop for a route
    # built any other way.
    if not auth_token and not route.auth:
        msg = (
            "checker_context route 'litellm' requires LITELLM_AUTH_TOKEN to be set, "
            "or an explicit `auth: {api_key: ENV_VAR}` override"
        )
        raise JudgeInfrastructureError(msg)

    try:
        from litellm.exceptions import APIError, BadRequestError
        from litellm.types.utils import ModelResponse

        import litellm
    except ImportError as e:
        raise JudgeInfrastructureError(
            "checker_context route 'litellm' needs the litellm library. Install with: pip install 'coder-eval[litellm]'"
        ) from e

    openai_tool = {
        "type": "function",
        "function": {
            "name": tool_spec["name"],
            "description": tool_spec["description"],
            "parameters": tool_spec["input_schema"],
        },
    }

    def _resolve_auth() -> dict[str, str]:
        """Resolve ``route.auth`` (kwarg name -> ENV VAR NAME) into kwarg name ->
        secret value, right before the call so no resolved secret is ever stored
        on the route object itself (only the env var *name* is)."""
        if not route.auth:
            return {}
        resolved: dict[str, str] = {}
        for param_name, env_var in route.auth.items():
            value = os.environ.get(env_var)
            if not value:
                raise JudgeInfrastructureError(
                    f"checker_context.api_route.auth[{param_name!r}] references env var {env_var!r}, which is not set"
                )
            resolved[param_name] = value
        return resolved

    def _call_kwargs(*, include_temperature: bool) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "api_base": route.base_url,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "tools": [openai_tool],
            "tool_choice": {"type": "function", "function": {"name": tool_spec["name"]}},
            "max_completion_tokens": max_tokens,
            "timeout": timeout_seconds,
            # `drop_params` only covers params litellm's own static model-cost map
            # KNOWS a model rejects; a custom/gateway-routed model id (e.g. one
            # behind an Azure AI deployment) isn't in that map, so an actual
            # unsupported-parameter rejection still round-trips to the provider —
            # handled below by retrying once without `temperature`.
            "drop_params": True,
        }
        if auth_token:
            kwargs["api_key"] = auth_token
        if include_temperature:
            kwargs["temperature"] = temperature
        # `params` is arbitrary passthrough (e.g. aws_region_name, api_version, ...);
        # `auth` (resolved secrets) applies LAST so it always wins over both the
        # LITELLM_AUTH_TOKEN default above and anything in `params`.
        if route.params:
            kwargs.update(route.params)
        kwargs.update(_resolve_auth())
        return kwargs

    def _rejects_temperature(e: BadRequestError) -> bool:
        body = e.body if isinstance(e.body, dict) else {}
        # The OpenAI SDK (which litellm's Azure path calls under the hood)
        # unwraps the provider's `{"error": {...}}` envelope before attaching
        # `.body` to the exception it raises — so `body` here is normally
        # already the inner object (`{"param": "temperature", ...}`). Handle a
        # still-wrapped shape too (a different provider path, or a future
        # litellm/openai version) rather than assuming one or the other.
        wrapped = body.get("error")
        inner = wrapped if isinstance(wrapped, dict) else body
        if inner.get("param") == "temperature":
            return True
        return "temperature" in str(e) and "not supported" in str(e).lower()

    try:
        try:
            response = await litellm.acompletion(**_call_kwargs(include_temperature=route.include_temperature))
        except BadRequestError as e:
            if not (route.include_temperature and _rejects_temperature(e)):
                raise
            logger.info("LiteLLM judge model %r rejects temperature; retrying without it", model)
            response = await litellm.acompletion(**_call_kwargs(include_temperature=False))
    except APIError as e:
        raise JudgeInfrastructureError(f"LiteLLM judge API error: {e}") from e
    except Exception as e:
        raise JudgeInfrastructureError(f"LiteLLM judge call failed: {e}") from e
    if not isinstance(response, ModelResponse):
        # Never actually streamed (no `stream=True` above) -- defensive only,
        # keeps pyright's ModelResponse | CustomStreamWrapper union honest.
        raise JudgeInfrastructureError(f"LiteLLM judge returned an unexpected response type: {type(response)}")
    return response.model_dump()
