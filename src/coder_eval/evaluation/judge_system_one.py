"""Single-request invoker for a System One model (TypeSafe's ``jev``).

One POST to ``<base_url>/systemone``, bearer-token auth, no streaming. The whole
rubric travels in that one request — a System One model answers every question in
parallel against a shared state — so there is no loop here and no tool channel to
coax: the response schema is fixed by the questions that were asked.

Transient failures (transport errors, 429 throttles, 5xx, the 529 overload code)
are retried with jittered exponential backoff; exhaustion and non-retryable
failures raise ``JudgeInfrastructureError`` so the row escalates to
``FinalStatus.ERROR`` instead of being scored 0.0.

Rationale: .claude/notes/contracts.md § System One rubric scoring
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx2

from coder_eval.errors import JudgeInfrastructureError
from coder_eval.errors.categories import RetryConfig
from coder_eval.errors.retry import compute_backoff


logger = logging.getLogger(__name__)

_SYSTEM_ONE_RETRY = RetryConfig(max_retries=3, initial_delay=2.0, backoff_multiplier=2.0)


def _is_retryable_status(status_code: int) -> bool:
    """Throttles, overload and server-side failures retry; 401/422 are the author's to fix."""
    return status_code == 429 or status_code >= 500


async def invoke_system_one_async(
    *,
    base_url: str,
    api_key: str,
    model: str,
    state: Any,
    questions: dict[str, dict[str, Any]],
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """POST one System One request and return the parsed response dict.

    The response carries ``answers`` (one entry per question id, keyed exactly as
    the rubric named them) and an Anthropic-shaped ``usage`` block, so the caller
    reuses ``token_usage_from_anthropic_dict`` unchanged.

    Raises:
        ValueError: ``model`` empty, or ``questions`` empty.
        JudgeInfrastructureError: no API key; retries exhausted; a non-retryable
            HTTP failure; or a body that is not a JSON object.
    """
    # Raise, not assert: the wrapper around this call catches plain Exception
    # (AssertionError included) and downgrades it to a scored 0.0.
    # Rationale: .claude/notes/contracts.md § What escalates instead of scoring 0.0
    if not model:
        raise ValueError("invoke_system_one_async: model must not be empty")
    if not questions:
        raise ValueError("invoke_system_one_async: questions must not be empty")
    if not api_key:
        raise JudgeInfrastructureError("system_one_judge requires an API key")

    url = f"{base_url.rstrip('/')}/systemone"
    body = {"model": model, "state": state, "questions": questions}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    attempts = _SYSTEM_ONE_RETRY.max_retries + 1
    last_failure = ""
    last_exc: Exception | None = None
    async with httpx2.AsyncClient() as client:
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(compute_backoff(_SYSTEM_ONE_RETRY, attempt - 1))
            try:
                response = await client.post(url, headers=headers, json=body, timeout=timeout_seconds)
            except httpx2.HTTPError as e:
                last_failure = f"System One transport error: {e}"
                last_exc = e
                logger.warning("System One judge attempt %d/%d failed: %s", attempt + 1, attempts, last_failure)
                continue
            if _is_retryable_status(response.status_code):
                last_failure = f"System One request failed: {response.status_code} {response.text[:500]}"
                last_exc = None
                logger.warning("System One judge attempt %d/%d failed: %s", attempt + 1, attempts, last_failure)
                continue
            if response.status_code >= 300:
                raise JudgeInfrastructureError(
                    f"System One request failed: {response.status_code} {response.text[:500]}"
                )
            try:
                data = response.json()
            except ValueError as e:
                raise JudgeInfrastructureError(f"System One response is not valid JSON: {e}") from e
            if not isinstance(data, dict):
                raise JudgeInfrastructureError(f"System One response is not a JSON object: {str(data)[:500]}")
            return data
    raise JudgeInfrastructureError(f"{last_failure} (after {attempts} attempts)") from last_exc
