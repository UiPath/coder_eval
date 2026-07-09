"""Single-completion Anthropic-SDK invoker for the Direct backend.

Hits api.anthropic.com using ANTHROPIC_API_KEY from env.

The judge issues a forced ``submit_verdict`` tool call; the SDK response is
converted to a dict via ``model_dump`` so the caller can reuse
``extract_verdict_from_anthropic_response`` — Anthropic SDK and Bedrock share
Anthropic's native message shape (content blocks with ``type: tool_use``).
"""

from __future__ import annotations

from typing import Any

from anthropic import Anthropic, APIError

from coder_eval.errors import JudgeInfrastructureError
from coder_eval.evaluation.judge_models import to_anthropic_alias


def invoke_anthropic_judge(
    *,
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
    client = Anthropic(timeout=timeout_seconds)
    with client:
        try:
            response = client.messages.create(
                model=alias,
                system=system,
                messages=[{"role": "user", "content": user}],
                max_tokens=max_tokens,
                temperature=temperature,
                tools=[tool_spec],  # type: ignore[arg-type]
                tool_choice={"type": "tool", "name": tool_spec["name"]},
            )
        except APIError as e:
            # The SDK already retries transient failures internally (2 attempts
            # by default) — do not add another retry loop here.
            raise JudgeInfrastructureError(f"Anthropic judge API error: {e}") from e
    return response.model_dump()
