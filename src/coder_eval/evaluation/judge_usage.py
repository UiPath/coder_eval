"""Usage extraction from judge LLM responses.

Kept separate from ``judge_models.py`` (whose docstring scopes it to pure
string utilities for model-name translation) — usage parsing is a distinct
concern. This helper turns a judge response into a ``TokenUsage`` so
``llm_judge`` can populate ``JudgeCriterionResult.token_usage`` uniformly
across the Anthropic / Bedrock-invoke (dict shape) backends.

Returns ``None`` (not a zero ``TokenUsage``) when usage is absent or empty,
so "unknown" stays distinguishable from "zero".
"""

from __future__ import annotations

from typing import Any

from coder_eval.models import TokenUsage
from coder_eval.pricing import calculate_cost


def _coerce_int(value: Any) -> int:
    """Best-effort non-negative int coercion for a usage counter.

    A malformed provider payload (non-numeric token value) must degrade to 0,
    not fail the judge criterion — these extractors promise "usage or None,
    never raise". ``int(value or 0)`` already handles falsy/missing; this also
    absorbs genuinely non-coercible values.
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def token_usage_from_anthropic_dict(resp: dict[str, Any], *, model: str | None = None) -> TokenUsage | None:
    """Extract usage from an Anthropic / Bedrock-invoke Messages response dict.

    Both ``invoke_anthropic_judge_async`` (``response.model_dump()``) and
    ``invoke_bedrock_judge_async`` (parsed ``/invoke`` JSON) carry an Anthropic-shaped
    ``usage`` block. Returns ``None`` when usage is missing or carries no tokens.

    ``model`` prices the call from the rate card. Neither judge backend returns a
    cost, so without it the judge's spend is invisible in every rollup. Left
    unpriced (``total_cost_usd=None``) when the model is absent from the card,
    which ``RunSummary.tasks_cost_incomplete`` then surfaces.
    """
    u = resp.get("usage")
    if not isinstance(u, dict):
        return None
    tu = TokenUsage(
        uncached_input_tokens=_coerce_int(u.get("input_tokens")),
        output_tokens=_coerce_int(u.get("output_tokens")),
        cache_creation_input_tokens=_coerce_int(u.get("cache_creation_input_tokens")),
        cache_read_input_tokens=_coerce_int(u.get("cache_read_input_tokens")),
    )
    if tu.is_empty():
        return None
    if model:
        tu.total_cost_usd = calculate_cost(
            model,
            uncached_input_tokens=tu.uncached_input_tokens,
            output_tokens=tu.output_tokens,
            cache_creation_tokens=tu.cache_creation_input_tokens,
            cache_read_tokens=tu.cache_read_input_tokens,
        )
    return tu


def token_usage_from_openai_dict(resp: dict[str, Any], *, model: str | None = None) -> TokenUsage | None:
    """Extract usage from an OpenAI Chat-Completions-shaped response dict.

    ``invoke_litellm_judge_async`` (parsed ``/chat/completions`` JSON) carries an
    OpenAI-shaped ``usage`` block: ``prompt_tokens``/``completion_tokens``, with
    the cached prefix (if any) nested under ``prompt_tokens_details.cached_tokens``.
    Returns ``None`` when usage is missing or carries no tokens.

    Follows the OpenAI/Codex cache-bucket convention documented on
    ``TokenUsage``: ``prompt_tokens`` is the FULL prompt inclusive of the cached
    prefix, so the fresh (uncached) slice is ``prompt_tokens - cached_tokens``;
    OpenAI bills no separate cache-write fee, so ``cache_creation_input_tokens``
    is always 0 (mirrors ``CodexAgent._token_usage_from_sdk``).

    ``model`` prices the call from the rate card, same as the Anthropic-shaped
    extractor above.
    """
    u = resp.get("usage")
    if not isinstance(u, dict):
        return None
    prompt_tokens = _coerce_int(u.get("prompt_tokens"))
    details = u.get("prompt_tokens_details")
    cached = _coerce_int(details.get("cached_tokens")) if isinstance(details, dict) else 0
    tu = TokenUsage(
        uncached_input_tokens=max(prompt_tokens - cached, 0),
        output_tokens=_coerce_int(u.get("completion_tokens")),
        cache_creation_input_tokens=0,
        cache_read_input_tokens=cached,
    )
    if tu.is_empty():
        return None
    if model:
        tu.total_cost_usd = calculate_cost(
            model,
            uncached_input_tokens=tu.uncached_input_tokens,
            output_tokens=tu.output_tokens,
            cache_creation_tokens=tu.cache_creation_input_tokens,
            cache_read_tokens=tu.cache_read_input_tokens,
        )
    return tu
