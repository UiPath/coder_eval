"""Single-completion Bedrock invoker for judge-style criteria.

Narrow on purpose: one POST to bedrock-runtime, bearer-token auth, no streaming.
The judge issues a forced ``submit_verdict`` tool call; we return the parsed
response dict so the caller can walk ``content`` for ``tool_use`` blocks via
``extract_verdict_from_anthropic_response``.

Transient failures (transport errors, 429 throttles, 5xx) are retried with
jittered exponential backoff; exhaustion and non-retryable failures raise
``JudgeInfrastructureError`` so the row escalates to ``FinalStatus.ERROR``
instead of being scored 0.0.

agent_judge uses the Claude Code SDK subprocess instead — the two paths
intentionally do not share an HTTP client.

Async on purpose: this is llm_judge's only implementation of the network
call (there is no sync twin) — ``httpx.AsyncClient`` lets the call yield the
event loop instead of blocking a thread-pool thread for the wait, so
``SuccessChecker.check_all_async`` can run several judge criteria
concurrently without pinning a thread per judge.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from coder_eval.errors import JudgeInfrastructureError
from coder_eval.errors.categories import RetryConfig
from coder_eval.errors.retry import compute_backoff
from coder_eval.evaluation.judge_models import to_bedrock_model
from coder_eval.models import BedrockRoute


logger = logging.getLogger(__name__)

_JUDGE_RETRY = RetryConfig(max_retries=3, initial_delay=2.0, backoff_multiplier=2.0)


def _is_retryable_status(status_code: int) -> bool:
    """Throttles and server-side failures are worth retrying; 4xx (auth, bad request) are not."""
    return status_code == 429 or status_code >= 500


async def invoke_bedrock_judge_async(
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

    Transport errors, 429, and 5xx responses are retried up to
    ``_JUDGE_RETRY.max_retries`` times with jittered exponential backoff.

    Raises:
        ValueError: ``model`` empty.
        JudgeInfrastructureError: retries exhausted, non-retryable HTTP failure
            (e.g. 400/401/403), or a non-dict JSON body.
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

    attempts = _JUDGE_RETRY.max_retries + 1
    last_failure = ""
    last_exc: Exception | None = None
    async with httpx.AsyncClient() as client:
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(compute_backoff(_JUDGE_RETRY, attempt - 1))
            try:
                response = await client.post(url, headers=headers, json=body, timeout=timeout_seconds)
            except httpx.HTTPError as e:
                last_failure = f"Bedrock invoke transport error: {e}"
                last_exc = e
                logger.warning("Bedrock judge attempt %d/%d failed: %s", attempt + 1, attempts, last_failure)
                continue
            if _is_retryable_status(response.status_code):
                last_failure = f"Bedrock invoke failed: {response.status_code} {response.text[:500]}"
                last_exc = None
                logger.warning("Bedrock judge attempt %d/%d failed: %s", attempt + 1, attempts, last_failure)
                continue
            if response.status_code >= 300:
                raise JudgeInfrastructureError(f"Bedrock invoke failed: {response.status_code} {response.text[:500]}")
            try:
                data = response.json()
            except ValueError as e:
                # A malformed/truncated 2xx body (flaky proxy/gateway) is infra, not
                # agent quality — escalate like the non-dict arm below.
                raise JudgeInfrastructureError(f"Bedrock response is not valid JSON: {e}") from e
            if not isinstance(data, dict):
                raise JudgeInfrastructureError(f"Bedrock response is not a JSON object: {str(data)[:500]}")
            return data
    raise JudgeInfrastructureError(f"{last_failure} (after {attempts} attempts)") from last_exc
