"""Single-completion Anthropic-SDK invoker for DirectRoute and ProxyRoute.

Direct hits api.anthropic.com using ANTHROPIC_API_KEY from env; Proxy hits
the local LLM-Gateway proxy at http://127.0.0.1:{port}/v1/messages with a
sentinel api_key (the proxy doesn't validate it). Both call sites flow
through the same SDK code path for parity.
"""

from __future__ import annotations

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
    timeout_seconds: float = 120.0,
) -> str:
    """One Messages call via the Anthropic SDK; returns concatenated assistant text.

    Raises:
        ValueError: ``model`` empty after translation.
        RuntimeError: response has no text content (anthropic SDK errors
            propagate as the SDK's own exception types).
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
        )
    text = "".join(block.text for block in response.content if block.type == "text")
    if not text:
        raise RuntimeError("Anthropic API returned no text content")
    return text
