"""Scrubbing + reconciliation helpers shared by the golden-master harness."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from coder_eval.timing import busy_ms


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
        # Measured wall intervals like the two above, so they vary run to run;
        # masking keeps None-vs-set (the meaningful distinction) visible while
        # the value itself stays out of the snapshot.
        "harness_startup_ms",
        "harness_teardown_ms",
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
DROP_KEYS = frozenset({"provider_call_costs"})


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


# The four buckets are disjoint by construction, so their sum cannot exceed the
# turn's own wall clock. Flag only an overshoot past BOTH bounds: the relative
# one is what catches the defect (an orphaned tool double-booked into the tail
# read +55% of wall on ``antigravity_d_orphaned_tool``), and the absolute floor
# keeps a replay whose whole turn is 40 microseconds from failing on scheduler
# jitter. Healthy fixtures overshoot by at most 0.003 ms / 2%.
_IDENTITY_FLOOR_MS = 0.1
_IDENTITY_SHARE = 0.20


def _tool_union_ms(record: dict[str, Any]) -> float:
    """Wall ms this turn spent executing tools — the union, never the sum."""
    spans: list[tuple[datetime, datetime]] = []
    for command in record.get("commands") or []:
        start = _parse_stamp(command.get("execution_started_at"))
        end = _parse_stamp(command.get("execution_completed_at"))
        if start is not None and end is not None and end >= start:
            spans.append((start, end))
    if not spans:
        return 0.0
    return busy_ms(spans, min(s for s, _ in spans), max(e for _, e in spans))


def _parse_stamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def assert_timing_captured(
    record: dict[str, Any], *, expect_generation_window: bool, check_identity: bool = True
) -> None:
    """Assert a TurnRecord dump actually recorded the timing it could measure.

    Run on the UNSCRUBBED dump. ``scrub()`` masks values but preserves ``None``
    (see its docstring), and present-vs-absent IS the whole assertion here — a
    scrubbed snapshot can tell you a field was set, never that it was set to
    something meaningful.

    An AST rule cannot see that an SDK returned ``0.0``; this replay-based
    sensor can. Two checks:

    **Unconditional.** Every command that RESOLVED (``result_status`` of
    ``"success"`` or ``"error"``) carries ``execution_started_at``,
    ``execution_completed_at`` and ``duration_ms``. A force-closed orphan
    (``"unknown"``) is exempt: it was never timed, and saying so is the honest
    record. Where a scenario resolves no command the check is vacuously true,
    which is correct rather than weak — the scenario is asserting nothing
    about commands because it has none.

    **Flagged.** When ``expect_generation_window``, at least one assistant
    entry reports a ``generation_duration_ms`` that is non-``None`` AND
    greater than zero AND whose recorded bounds actually span it
    (``completed_at > started_at``).

    The bounds half is not redundant. Two harnesses derive the duration from a
    MONOTONIC clock and the bounds from the wall clock, so the two can
    disagree: a reducer could report a healthy duration beside two stamps that
    collapsed to one instant. CE059 catches that statically only when both
    bounds are the same ``ast.Name``; when they are two different names
    holding the same value it cannot, and this is the check that does.

    **Unconditional, and keyed on the messages rather than on the flag.** A
    turn's head and tail (``harness_startup_ms`` / ``harness_teardown_ms``) are
    set exactly when the turn produced an assistant message with a MEASURABLE
    window, because that is what the collector measures them against — so both
    are non-``None`` when one exists and both are ``None`` when none does.

    Both halves of that key are load-bearing. The flag is the wrong one:
    ``codex_e_orphan_tool`` streams a generation whose window subtracts to
    zero, so it clears the flag while still having a head and a tail to report.
    And "any assistant message" is too weak: ``codex_g_items_rebuild`` rebuilds
    its transcript from the rollout after the turn ended, with
    ``generation_duration_ms=None`` and placeholder ``now()`` bounds, so there
    is nothing there to measure an end against and the honest answer is
    ``None`` for both.

    PRESENCE is all the fixtures can support, and it is the thing worth
    asserting: the replays run in ~0.3 ms of synthetic wall clock, so their
    head and tail are microseconds and any bound or ordering check would be
    noise. A ``>= 0`` check would be worse than noise — ``decompose_turn``
    clamps with ``max(..., 0.0)``, so it would restate the implementation and
    could never fail.

    **The four-bucket identity**, when ``check_identity``. Generation plus the
    UNION of the tool intervals plus the head plus the tail cannot exceed the
    turn's ``duration_seconds``, because the four are disjoint: the windows are
    tool-subtracted and so are the head and tail. This is the one assertion
    that catches a DOUBLE-COUNT rather than an absence — it is how an orphaned
    tool force-closed inside the tail, booked both as tool and as teardown, was
    found reconciling at -86% of wall clock while all 72 golden tests passed.

    ``check_identity`` is off for the scenarios that inject their own SDK
    timestamps (see ``FICTIONAL_DURATIONS``): those declare integer-millisecond
    item durations of 17-900 ms while the replay itself takes ~0.3 ms of real
    wall clock, so no rebasing can make the two commensurable — the SDK's
    stamps are milliseconds and the replay is faster than one.

    Why a scenario-level floor rather than a per-entry rule: no per-entry form
    works against the real snapshots. ``claude_d_subagent_terminal`` holds two
    content-bearing assistant messages of which exactly one is legitimately
    ``None`` (the synthesized sub-agent generation, delivered as a tool result
    and never streamed), so no scenario-level flag can express "this one but
    not that one". And "never exactly 0.0" conflicts with the clamps that can
    legitimately produce a measured zero. The detailed per-message contract
    lives in each agent's own unit tests; this is the cross-harness floor.
    """
    for command in record.get("commands") or []:
        if command.get("result_status") not in ("success", "error"):
            continue
        tool = command.get("tool_id")
        for field in ("execution_started_at", "execution_completed_at", "duration_ms"):
            assert command.get(field) is not None, (
                f"resolved command {tool!r} has no {field}: a command that ran and "
                "returned was timed, so the record must say when and for how long"
            )

    assistant = [m for m in record.get("messages") or [] if m.get("role") == "assistant"]
    measurable = [m for m in assistant if m.get("generation_duration_ms") is not None]
    for field in ("harness_startup_ms", "harness_teardown_ms"):
        value = record.get(field)
        if measurable:
            assert value is not None, (
                f"{field} is None on a turn carrying {len(measurable)} measurable generation "
                "window(s): the collector measures the head and tail against the earliest and "
                "latest of those, so a turn that generated has both — None says never measured"
            )
        else:
            assert value is None, (
                f"{field} is {value!r} on a turn with no measurable generation window "
                f"({len(assistant)} assistant message(s), none reporting a duration): there is "
                "nothing to measure an end against, and a number here claims a measurement "
                "nobody could have taken"
            )

    if check_identity:
        wall_ms = (record.get("duration_seconds") or 0.0) * 1000.0
        # Main thread only: a sub-agent's generations bubble into the same
        # stream, and the spawning Agent call's own interval already spans them.
        generation_ms = sum(
            m.get("generation_duration_ms") or 0.0
            for m in record.get("messages") or []
            if m.get("role") == "assistant" and m.get("parent_tool_use_id") is None
        )
        tool_ms = _tool_union_ms(record)
        bucket_sum = (
            generation_ms
            + tool_ms
            + (record.get("harness_startup_ms") or 0.0)
            + (record.get("harness_teardown_ms") or 0.0)
        )
        overshoot = bucket_sum - wall_ms
        assert overshoot <= max(_IDENTITY_FLOOR_MS, _IDENTITY_SHARE * wall_ms), (
            f"the four buckets sum to {bucket_sum:.4f} ms against a {wall_ms:.4f} ms turn "
            f"(over by {overshoot:.4f} ms): generation={generation_ms:.4f}, tool_union={tool_ms:.4f}, "
            f"startup={record.get('harness_startup_ms')!r}, teardown={record.get('harness_teardown_ms')!r}. "
            "They are meant to be DISJOINT, so a sum this far over the turn means something is "
            "booked twice — most likely a tool that ran outside every generation window and was "
            "left in the head or tail as well as in the tool union. The one KNOWN exception is "
            "claude-code, which does not subtract tool time from its generation windows, so a "
            "trajectory where a tool runs concurrently with a later emission of the same turn "
            "overlaps legitimately; see docs/agents/HARNESS_PARITY.md before concluding this is "
            "a new bug"
        )

    if not expect_generation_window:
        return
    windows = [(m.get("generation_duration_ms"), m.get("started_at"), m.get("completed_at")) for m in assistant]
    assert any(
        duration is not None and duration > 0 and started is not None and completed is not None and completed > started
        for duration, started, completed in windows
    ), (
        f"no assistant message reports a positive generation window with bounds that span it "
        f"(saw (duration, started_at, completed_at) = {windows!r}); the harness measured no model "
        "time at all for a turn that streamed one, or its two clocks disagree"
    )
