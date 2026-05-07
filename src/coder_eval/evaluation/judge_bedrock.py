"""Single-completion Bedrock invoker for judge-style criteria.

Narrow on purpose: one POST to bedrock-runtime, bearer-token auth, no streaming,
no tools. agent_judge uses the Claude Code SDK subprocess instead — the two
paths intentionally do not share an HTTP client.
"""

from __future__ import annotations

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
    timeout_seconds: float = 120.0,
) -> str:
    """POST one Anthropic-Messages request to Bedrock and return assistant text.

    Raises:
        ValueError: ``model`` empty.
        RuntimeError: transport failure, HTTP non-2xx, missing ``content`` key,
            or no text blocks. Transport exceptions are wrapped so callers see
            one consistent failure type.
    """
    qualified = to_bedrock_model(model, route.region)
    url = f"https://bedrock-runtime.{route.region}.amazonaws.com/model/{qualified}/invoke"
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": [{"role": "user", "content": user}],
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
    blocks = data.get("content")
    if not isinstance(blocks, list):
        raise RuntimeError(f"Bedrock response missing 'content' list: {str(data)[:500]}")
    text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
    if not text:
        raise RuntimeError("Bedrock returned no text content")
    return text
