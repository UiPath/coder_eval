"""Single-completion Bedrock invoker for judge-style criteria.

Narrow on purpose: one POST to bedrock-runtime, bearer-token auth, no streaming.
The judge issues a forced ``submit_verdict`` tool call; we return the parsed
response dict so the caller can walk ``content`` for ``tool_use`` blocks via
``extract_verdict_from_anthropic_response``.

agent_judge uses the Claude Code SDK subprocess instead — the two paths
intentionally do not share an HTTP client.
"""

from __future__ import annotations

from typing import Any

import httpx

from coder_eval.evaluation.judge_models import to_bedrock_model
from coder_eval.models import BedrockRoute


def invoke_bedrock_judge(
    *,
    route: BedrockRoute,
    model: str,
    system: str,
    user: str,
    temperature: float,
    max_tokens: int,
    tool_spec: dict[str, Any],
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """POST a Bedrock Messages request with ``tools`` + forced ``tool_choice``.

    Returns the parsed response dict so the caller can walk ``content`` for
    ``tool_use`` blocks via ``extract_verdict_from_anthropic_response``.

    Raises:
        ValueError: ``model`` empty.
        RuntimeError: transport failure or HTTP non-2xx.
    """
    qualified = to_bedrock_model(model, route.region)
    url = f"https://bedrock-runtime.{route.region}.amazonaws.com/model/{qualified}/invoke"
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "tools": [tool_spec],
        "tool_choice": {"type": "tool", "name": tool_spec["name"]},
    }
    headers = {
        "Authorization": f"Bearer {route.bearer_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        response = httpx.post(url, headers=headers, json=body, timeout=timeout_seconds)
    except httpx.HTTPError as e:
        raise RuntimeError(f"Bedrock invoke transport error: {e}") from e
    if response.status_code >= 300:
        raise RuntimeError(f"Bedrock invoke failed: {response.status_code} {response.text[:500]}")
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Bedrock response is not a JSON object: {str(data)[:500]}")
    return data
