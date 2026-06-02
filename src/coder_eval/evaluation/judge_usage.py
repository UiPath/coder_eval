"""Usage extraction from judge LLM responses.

Kept separate from ``judge_models.py`` (whose docstring scopes it to pure
string utilities for model-name translation) — usage parsing is a distinct
concern. These helpers turn a judge response into a ``TokenUsage`` so
``llm_judge`` can populate ``JudgeCriterionResult.token_usage`` uniformly
across Anthropic / Bedrock-invoke (dict shape) and LangChain / LLMGW
(``AIMessage.usage_metadata`` shape) backends.

Both return ``None`` (not a zero ``TokenUsage``) when usage is absent or
empty, so "unknown" stays distinguishable from "zero" — important for the
Proxy-only reconciliation invariant.
"""

from __future__ import annotations

from typing import Any

from coder_eval.models import TokenUsage


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


def token_usage_from_anthropic_dict(resp: dict[str, Any]) -> TokenUsage | None:
    """Extract usage from an Anthropic / Bedrock-invoke Messages response dict.

    Both ``invoke_anthropic_judge`` (``response.model_dump()``) and
    ``invoke_bedrock_judge`` (parsed ``/invoke`` JSON) carry an Anthropic-shaped
    ``usage`` block. Returns ``None`` when usage is missing or carries no tokens.
    """
    u = resp.get("usage")
    if not isinstance(u, dict):
        return None
    tu = TokenUsage(
        input_tokens=_coerce_int(u.get("input_tokens")),
        output_tokens=_coerce_int(u.get("output_tokens")),
        cache_creation_input_tokens=_coerce_int(u.get("cache_creation_input_tokens")),
        cache_read_input_tokens=_coerce_int(u.get("cache_read_input_tokens")),
    )
    return None if tu.is_empty() else tu


def token_usage_from_langchain_message(msg: object) -> TokenUsage | None:
    """Extract usage from a LangChain ``AIMessage.usage_metadata``.

    ``usage_metadata`` is a dict with ``input_tokens`` / ``output_tokens`` /
    ``total_tokens``. Absent on some providers — returns ``None`` gracefully.
    """
    meta = getattr(msg, "usage_metadata", None)
    if not isinstance(meta, dict):
        return None
    tu = TokenUsage(
        input_tokens=_coerce_int(meta.get("input_tokens")),
        output_tokens=_coerce_int(meta.get("output_tokens")),
    )
    return None if tu.is_empty() else tu
