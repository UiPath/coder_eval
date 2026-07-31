"""LiteLLM proxy success-logging callback: capture per-call ACTUAL cost + cache.

Why this exists
---------------
For coder_eval's open-weight (``litellm``) backend the Claude Code binary calls
this proxy in **Anthropic Messages** format. OpenRouter's real per-call
``usage.cost`` and cache-read tokens are dropped in the OpenAI->Anthropic
translation, so the Python harness only ever sees an aggregate turn total priced
at Claude list rates. This callback runs **inside the proxy**, where each call's
OpenAI-shaped response still carries the real numbers, and appends one JSONL
record per call to ``$LITELLM_COST_LOG``. The harness later joins those records
back to a run/task/turn by the ``x-ce-*`` correlation headers the agent stamped
via ``ANTHROPIC_CUSTOM_HEADERS``.

IMPORTANT: read ``response_obj.usage.cost`` (OpenRouter's real, routed-provider
cost), **never** ``standard_logging_object.response_cost`` (LiteLLM's own map,
which is ``0.0`` for models it doesn't price -- e.g.
``openrouter/deepseek/deepseek-v4-pro``). Requires ``usage: {include: true}`` on
each ``openrouter/*`` model in ``litellm-config.yaml`` so OpenRouter populates
``usage.cost``.

Register in ``litellm-config.yaml``::

    litellm_settings:
      callbacks: cost_logger.proxy_handler_instance

This module is intentionally **self-contained** (no ``coder_eval`` import): the
proxy may run in its own ephemeral environment
(``uvx --from 'litellm[proxy]' litellm``).
"""

from __future__ import annotations

import json
import math
import os
from typing import Any


# LiteLLM is present in the proxy's environment but NOT in coder_eval's test env.
# Guard the import so the pure helpers below stay unit-testable standalone.
try:
    from litellm.integrations.custom_logger import CustomLogger
except Exception:  # pragma: no cover - only when litellm is absent (i.e. under test)

    class CustomLogger:  # type: ignore[no-redef]
        """Fallback base so this module imports without litellm installed."""


# Correlation-header prefix, matching claude_code_agent._build_sdk_env's tags.
_TAG_PREFIX = "x-ce-"


def _num(value: Any) -> float | int | None:
    """Keep only real, FINITE numbers (reject bool — an int subclass — strings, and
    NaN/Inf). A non-finite cost would serialize as a bare ``NaN`` token (invalid JSON
    that breaks parsing the whole ``task.json``) and make the ``max_usd`` gate's
    ``cost > limit`` silently never fire, so it is filtered at this boundary."""
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return None


def _to_dict(obj: Any) -> dict[str, Any]:
    """Coerce a dict / pydantic model / litellm object to a plain dict."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    for attr in ("model_dump", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            # Best-effort coercion: if one dumper raises on a quirky litellm/pydantic
            # object, fall through to the next attr (or to {} below) rather than
            # propagate — a logging callback must never break the proxy response path.
            try:
                result = fn()
                if isinstance(result, dict):
                    return result
            except Exception:
                # This dumper failed on a quirky object; try the next attr / {}.
                pass
    return {}


def build_cost_record(
    usage: dict[str, Any] | None,
    tags: dict[str, str],
    *,
    model: str | None,
    call_id: str | None,
) -> dict[str, Any] | None:
    """Build one per-call JSONL record from an OpenAI-shaped ``usage`` dict plus
    the forwarded ``x-ce-*`` correlation tags.

    Returns ``None`` when there is no usable signal (no correlation tag AND no
    cost), so the harness falls back to static pricing instead of recording a
    bogus ``$0`` row.
    """
    usage = usage or {}
    cost = _num(usage.get("cost"))
    input_total, cache_read, cache_write, output = _extract_token_buckets(usage)
    record: dict[str, Any] = {
        "run_id": tags.get(f"{_TAG_PREFIX}run-id"),
        "task_id": tags.get(f"{_TAG_PREFIX}task-id"),
        "iteration": tags.get(f"{_TAG_PREFIX}iteration"),
        "attempt": tags.get(f"{_TAG_PREFIX}attempt"),
        "call_id": call_id,
        "model": model,
        # OpenRouter's real routed-provider price. NOT LiteLLM's response_cost.
        "cost": cost,
        "input": input_total,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "output": output,
    }
    if record["run_id"] is None and cost is None:
        return None
    return record


def _extract_token_buckets(
    usage: dict[str, Any],
) -> tuple[int | float | None, int | float | None, int | float | None, int | float | None]:
    """Return ``(input_total, cache_read, cache_write, output)`` from a usage dict
    of EITHER shape.

    On the Anthropic-inbound ``/v1/messages`` path a call's usage can come back
    OpenAI-shaped (``prompt_tokens`` / ``completion_tokens`` /
    ``prompt_tokens_details.cached_tokens``) OR Anthropic-shaped (``input_tokens``
    / ``output_tokens`` / ``cache_read_input_tokens`` /
    ``cache_creation_input_tokens``). Reading only the OpenAI keys left tokens
    ``None`` on the Anthropic-shaped calls (while cost survived).

    ``input_total`` is normalized to the FULL prompt (incl. cached) so the
    harness's ``uncached = input - cache_read - cache_write`` holds either way:
    OpenAI ``prompt_tokens`` is already the total; Anthropic ``input_tokens`` is
    only the uncached slice, so the cache buckets are added back.
    """
    details = usage.get("prompt_tokens_details") or {}
    cache_read = _num(details.get("cached_tokens"))
    if cache_read is None:
        cache_read = _num(usage.get("cache_read_input_tokens"))
    cache_write = _num(details.get("cache_write_tokens"))
    if cache_write is None:
        cache_write = _num(usage.get("cache_creation_input_tokens"))
    output = _num(usage.get("completion_tokens"))
    if output is None:
        output = _num(usage.get("output_tokens"))
    input_total = _num(usage.get("prompt_tokens"))
    if input_total is None:
        uncached = _num(usage.get("input_tokens"))
        if uncached is not None:
            input_total = uncached + (cache_read or 0) + (cache_write or 0)
    return input_total, cache_read, cache_write, output


def extract_tags(kwargs: dict[str, Any]) -> dict[str, str]:
    """Pull the ``x-ce-*`` correlation headers out of the LiteLLM call kwargs.

    Headers surface in different places across LiteLLM versions/paths; merge the
    known locations and keep only our ``x-ce-*`` tags (keys lower-cased).
    """
    headers: dict[str, Any] = {}
    lp = kwargs.get("litellm_params") or {}
    for source in (
        (lp.get("metadata") or {}).get("headers"),
        (lp.get("proxy_server_request") or {}).get("headers"),
        (kwargs.get("proxy_server_request") or {}).get("headers"),
        (kwargs.get("metadata") or {}).get("headers"),
    ):
        if isinstance(source, dict):
            headers.update(source)
    return {str(k).lower(): str(v) for k, v in headers.items() if str(k).lower().startswith(_TAG_PREFIX)}


def append_record(record: dict[str, Any] | None) -> None:
    """Append one record as a JSONL line to ``$LITELLM_COST_LOG`` (no-op if unset).

    One ``write()`` of a small (<4 KiB) line in append mode is atomic on POSIX,
    so parallel tasks funneled through one proxy interleave lines safely.
    """
    path = os.environ.get("LITELLM_COST_LOG")
    if not path or record is None:
        return
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)


class CostLogger(CustomLogger):
    """LiteLLM success callback → one per-call JSONL cost record."""

    def _emit(self, kwargs: dict[str, Any], response_obj: Any) -> None:
        try:
            response = _to_dict(response_obj)
            slo = kwargs.get("standard_logging_object") or {}
            lp = kwargs.get("litellm_params") or {}
            # model surfaces in different places across paths (top-level response,
            # kwargs, litellm_params, or the standard logging object).
            model = (
                response.get("model")
                or kwargs.get("model")
                or lp.get("model")
                or (slo.get("model") if isinstance(slo, dict) else None)
            )
            record = build_cost_record(
                _to_dict(response.get("usage")),
                extract_tags(kwargs),
                model=model,
                call_id=response.get("id"),
            )
            append_record(record)
        except Exception:
            # A logging callback must NEVER break the proxy's response path.
            pass

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._emit(kwargs, response_obj)

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._emit(kwargs, response_obj)


# The instance litellm-config.yaml references: `callbacks: cost_logger.proxy_handler_instance`.
proxy_handler_instance = CostLogger()
