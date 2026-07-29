"""Single-completion Anthropic-SDK invoker for the Direct backend.

Hits api.anthropic.com using ANTHROPIC_API_KEY from env.

The judge issues a forced ``submit_verdict`` tool call; the SDK response is
converted to a dict via ``model_dump`` so the caller can reuse
``extract_verdict_from_anthropic_response`` — Anthropic SDK and Bedrock share
Anthropic's native message shape (content blocks with ``type: tool_use``).

Async on purpose: this is llm_judge's only implementation of the network
call (there is no sync twin) — ``AsyncAnthropic`` lets the call yield the
event loop instead of blocking a thread-pool thread for the wait, so
``SuccessChecker.check_all_async`` awaits it directly without pinning a
thread. (``check_all_async`` currently runs criteria sequentially; running
several judges concurrently is deferred to a follow-up PR.)
"""

from __future__ import annotations

from typing import Any

from anthropic import APIError, AsyncAnthropic

from coder_eval.errors import JudgeInfrastructureError
from coder_eval.evaluation.judge_models import to_anthropic_alias


async def invoke_anthropic_judge_async(
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
    client = AsyncAnthropic(timeout=timeout_seconds)
    async with client:
        try:
            response = await client.messages.create(
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
