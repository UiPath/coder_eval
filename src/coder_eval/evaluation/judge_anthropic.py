"""Single-completion Anthropic-SDK invoker for DirectRoute and ProxyRoute.

Direct hits api.anthropic.com using ANTHROPIC_API_KEY from env; Proxy hits
the local LLM-Gateway proxy at http://127.0.0.1:{port}/v1/messages with a
sentinel api_key (the proxy doesn't validate it). Both call sites flow
through the same SDK code path for parity.

The judge issues a forced ``submit_verdict`` tool call; the SDK response is
converted to a dict via ``model_dump`` so the caller can reuse
``extract_verdict_from_anthropic_response`` — Anthropic SDK and Bedrock share
Anthropic's native message shape (content blocks with ``type: tool_use``).
"""

from __future__ import annotations

from typing import Any

from anthropic import Anthropic

from coder_eval.evaluation.judge_models import to_anthropic_alias
from coder_eval.models import DirectRoute, ProxyRoute


# Mirrors ClaudeCodeAgent's ProxyRoute env injection (claude_code_agent.py:340).
_PROXY_API_KEY_SENTINEL = "llmgw-proxy"


def invoke_anthropic_judge(
    *,
    route: DirectRoute | ProxyRoute,
    model: str,
    system: str,
    user: str,
    temperature: float,
    max_tokens: int,
    tool_spec: dict[str, Any],
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """One Messages call with ``tools`` + forced ``tool_choice``.

    Returns the SDK response converted to a dict via ``model_dump`` so the
    caller can reuse ``extract_verdict_from_anthropic_response`` — Anthropic's
    native shape (content blocks with ``type: tool_use``) is identical
    between this SDK call and the Bedrock httpx-direct call.
    """
    alias = to_anthropic_alias(model)
    if isinstance(route, ProxyRoute):
        client = Anthropic(
            base_url=f"http://127.0.0.1:{route.port}",
            api_key=_PROXY_API_KEY_SENTINEL,
            timeout=timeout_seconds,
        )
    else:
        client = Anthropic(timeout=timeout_seconds)
    with client:
        response = client.messages.create(
            model=alias,
            system=system,
            messages=[{"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=temperature,
            tools=[tool_spec],  # type: ignore[arg-type]
            tool_choice={"type": "tool", "name": tool_spec["name"]},
        )
    return response.model_dump()
