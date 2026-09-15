"""Single-completion invoker for the LiteLLM judge backend, via the ``litellm``
library (the ``coder-eval[litellm]`` extra) rather than a hand-rolled HTTP call.

Reads NOTHING from ``coder_eval.config.settings``: the task author fully owns the
call shape via ``LiteLLMRoute.params``/``env_params``, with no implicit fallback to
the AGENT's own LiteLLM proxy settings.

``litellm.acompletion`` always returns an OpenAI-shaped ``ModelResponse``
regardless of provider, so the caller reuses the OpenAI extractors unchanged.
Async on purpose, mirroring the other two judge invokers.

Rationale: .claude/notes/contracts.md § LiteLLM params and env_params
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
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    tool_spec: dict[str, Any],
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """One completion call via ``litellm.acompletion`` with a forced tool call.

    Unlike ``invoke_anthropic_judge_async``/``invoke_bedrock_judge_async``, this
    does NOT take a ``temperature`` — a gateway-routed model may reject it
    outright (observed live against an Azure AI deployment), and there is no
    uniform way to know in advance. Set it via ``route.params`` (e.g.
    ``{temperature: 0.0}``) if the target model accepts it.

    Every other provider-specific kwarg (``api_base``, ``api_key``,
    ``aws_access_key_id``, ...) comes from ``route.params``/``route.env_params``
    — see ``LiteLLMRoute``'s docstring. This function has no opinion on what a
    valid call needs; an incomplete configuration surfaces as whatever error
    ``litellm`` itself raises, wrapped below.

    Returns the OpenAI-shaped response converted to a dict via ``model_dump``
    so the caller can reuse ``extract_verdict_from_openai_response``.

    Raises:
        ValueError: ``model`` empty.
        JudgeInfrastructureError: the ``litellm`` extra isn't installed; an
            ``env_params`` entry names an unset env var; or the call fails (an
            eval-infra fault, not the agent's fault — CE039).
    """
    if not model:
        raise ValueError("invoke_litellm_judge_async: model must not be empty")

    try:
        from litellm.exceptions import APIError
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

    def _resolve_env_params() -> dict[str, str]:
        """Resolve ``route.env_params`` (kwarg name -> ENV VAR NAME) into kwarg
        name -> value, right before the call so no resolved value is ever stored
        on the route object itself (only the env var *name* is)."""
        if not route.env_params:
            return {}
        resolved: dict[str, str] = {}
        for param_name, env_var in route.env_params.items():
            value = os.environ.get(env_var)
            if not value:
                msg = (
                    f"checker_context.api_route.env_params[{param_name!r}] references env var "
                    f"{env_var!r}, which is not set"
                )
                raise JudgeInfrastructureError(msg)
            resolved[param_name] = value
        return resolved

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "tools": [openai_tool],
        "tool_choice": {"type": "function", "function": {"name": tool_spec["name"]}},
        "max_completion_tokens": max_tokens,
        "timeout": timeout_seconds,
        # HAZARD: `drop_params` only covers params litellm's static cost map knows a
        # model rejects; a gateway-routed model id usually is not in that map.
        "drop_params": True,
    }
    # `params` (literal passthrough) applies first; `env_params` (resolved from
    # env) applies LAST so it always wins over `params` for the same key.
    if route.params:
        kwargs.update(route.params)
    kwargs.update(_resolve_env_params())

    try:
        response = await litellm.acompletion(**kwargs)
    except APIError as e:
        raise JudgeInfrastructureError(f"LiteLLM judge API error: {e}") from e
    except Exception as e:
        raise JudgeInfrastructureError(f"LiteLLM judge call failed: {e}") from e
    if not isinstance(response, ModelResponse):
        # Never actually streamed (no `stream=True` above) -- defensive only,
        # keeps pyright's ModelResponse | CustomStreamWrapper union honest.
        raise JudgeInfrastructureError(f"LiteLLM judge returned an unexpected response type: {type(response)}")
    return response.model_dump()
