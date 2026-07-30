"""Scrubbing + reconciliation helpers shared by the golden-master harness."""

from __future__ import annotations

from typing import Any


SCRUB_PLACEHOLDER = "<scrubbed>"

# Fields whose values are inherently per-run (wall-clock timestamps, measured
# durations, rate-card cost) and so must be masked before byte-comparison. The
# set is enumerated from the models (TurnRecord / CommandTelemetry /
# AssistantMessage / UserMessage / TokenUsage) so a recursive walk catches every
# nested occurrence — e.g. ``commands[*].generation_completed_at`` and
# ``messages[*].started_at`` — not just the top level.
SCRUB_KEYS = frozenset(
    {
        "timestamp",
        "started_at",
        "completed_at",
        "generation_completed_at",
        "execution_started_at",
        "execution_completed_at",
        "duration_ms",
        "duration_seconds",
        "generation_duration_ms",
        # Cost is a rate-card-dependent float (and is backfilled from the rate
        # card on timeout/kill), so it is masked too — keeping the snapshot
        # rate-card-independent. The integer TOKEN buckets stay EXACT; those are
        # the real invariant.
        "total_cost_usd",
    }
)

# Fields DROPPED (not masked) from the snapshot because they are NOT produced by
# the agent turn-loop this golden captures — they are populated later by the
# orchestrator (e.g. the LiteLLM actual-cost join sets ``provider_call_costs``).
# Always empty here, and agent-agnostic, so dropping keeps the golden stable
# across backends (Claude + Codex) without a per-field regen.
DROP_KEYS = frozenset({"provider_call_costs", "cost_usd"})


def scrub(obj: Any) -> Any:
    """Recursively replace scrub-listed field values with a stable placeholder,
    and drop ``DROP_KEYS`` fields entirely.

    A ``None`` value is preserved (so the meaningful, deterministic
    present-vs-absent distinction survives — e.g. ``duration_ms=None`` on an
    orphaned command, or ``total_cost_usd=None`` when no cost was computed);
    only non-``None`` values are masked.
    """
    if isinstance(obj, dict):
        return {
            key: (SCRUB_PLACEHOLDER if (key in SCRUB_KEYS and value is not None) else scrub(value))
            for key, value in obj.items()
            if key not in DROP_KEYS
        }
    if isinstance(obj, list):
        return [scrub(item) for item in obj]
    return obj


def assert_reconciliation(record: dict[str, Any]) -> None:
    """Assert the per-bucket reconciliation invariant on a TurnRecord dump.

    Over the assistant + reconciliation transcript entries (simulator
    ``UserMessage`` tokens are a separate bill, excluded — matching
    ``EventCollector._reconciled_messages``), summing each of the four token
    buckets reproduces the authoritative ``token_usage`` exactly:

        Σ messages[*].input_tokens          == token_usage.uncached_input_tokens
        Σ messages[*].output_tokens         == token_usage.output_tokens
        Σ messages[*].cache_creation_tokens == token_usage.cache_creation_input_tokens
        Σ messages[*].cache_read_tokens     == token_usage.cache_read_input_tokens

    The DERIVED ``token_usage.input_tokens`` (= uncached + cache_creation +
    cache_read) is intentionally NOT compared against ``Σ input_tokens`` — that
    would compare the full prompt against the uncached slice and falsely fail.

    Skipped when ``token_usage`` is absent (crash/timeout partials that captured
    no usage), where the invariant does not apply.
    """
    usage = record.get("token_usage")
    if usage is None:
        return

    in_sum = out_sum = cw_sum = cr_sum = 0
    for message in record.get("messages") or []:
        if message.get("role") not in ("assistant", "reconciliation"):
            continue
        in_sum += message.get("input_tokens", 0)
        out_sum += message.get("output_tokens", 0)
        cw_sum += message.get("cache_creation_tokens", 0)
        cr_sum += message.get("cache_read_tokens", 0)

    assert in_sum == usage["uncached_input_tokens"], "input bucket does not reconcile"
    assert out_sum == usage["output_tokens"], "output bucket does not reconcile"
    assert cw_sum == usage["cache_creation_input_tokens"], "cache_creation bucket does not reconcile"
    assert cr_sum == usage["cache_read_input_tokens"], "cache_read bucket does not reconcile"
