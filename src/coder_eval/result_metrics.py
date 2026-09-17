"""Metrics derived from a finished ``EvaluationResult``.

These are consumed by the orchestrator *during* a run as well as by the
reporters afterwards, which is why they do not live in a ``reports*`` module:
a metric the core layer needs is not a report, and CE066 enforces that
distinction.

They are equally not part of ``timing.py``. That module operates on
``AssistantMessage`` / ``CommandTelemetry`` / ``TranscriptMessage`` and has no
``EvaluationResult`` dependency; adding one would widen the surface every agent
adapter imports.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import NamedTuple

from coder_eval.models import AssistantMessage, EvaluationResult, TurnRecord
from coder_eval.timing import main_thread_tool_spans, union_ms


class TurnTimeBuckets(NamedTuple):
    """The four wall-clock buckets of a whole run, plus what they leave over.

    Each is ``None`` when NOTHING in the run measured it — a run recorded before
    the head and tail were captured has no startup at all, a run that recorded
    no bounded tool span has no tool total, and a run with no duration has no
    residual. Rendering any of those as ``0ms`` claims a measurement nobody
    took (CE058, and the reason the evalboard's ``sumMeasured`` returns
    ``null``). A MEASURED zero stays ``0.0`` and renders as ``0ms``.

    DISPLAY AND ARITHMETIC DIFFER HERE, on purpose. An unmeasured bucket renders
    as a dash and counts as ``0.0`` toward ``unaccounted``, so the missing time
    surfaces as residual rather than vanishing. That is the rule
    ``scripts/timing/decompose_run.py::_turn_buckets`` already applies, and
    keeping the two the same is what lets a reader compare them.
    """

    startup_ms: float | None
    generation_ms: float | None
    tool_ms: float | None
    teardown_ms: float | None
    unaccounted_ms: float | None


def turn_time_buckets(result: EvaluationResult) -> TurnTimeBuckets:
    """Sum the four timing buckets across a run's turns, and the residual.

    The arithmetic lives HERE rather than in the renderer because this module is
    the designated home for shared report statistics: the evalboard, the
    markdown report and the HTML report must not each grow their own version.
    ``reports_html`` formats what this returns and decides nothing.

    ``unaccounted`` is measured against ``EvaluationResult.duration_seconds`` —
    the TASK's wall clock, which is what the card's existing Total Latency uses
    and what the evalboard's own Unaccounted cell uses. It therefore legitimately
    contains sandbox setup and grading, and is LARGER than the per-turn residual
    ``decompose_run.py`` reports. The two are not comparable and the label says
    so.
    """
    turns = result.iterations or []
    startup = _sum_measured(t.harness_startup_ms for t in turns)
    teardown = _sum_measured(t.harness_teardown_ms for t in turns)
    # MAIN THREAD ONLY, the same filter the collector and the evalboard apply:
    # a sub-agent's generations bubble into the same stream, and the spawning
    # Agent call's own interval already spans them.
    generation = _sum_measured(
        m.generation_duration_ms
        for t in turns
        for m in t.messages
        if isinstance(m, AssistantMessage) and m.parent_tool_use_id is None
    )
    # `None` only when NO turn recorded a bounded tool span. A turn that ran
    # tools and timed none is indistinguishable from a turn that ran none, so
    # the presence of a SPAN — not the presence of a turn — is what decides
    # measured-versus-not. `_sum_measured` over a list of plain floats could
    # never return None, which made this read `0ms` ("measured and instant")
    # for a run nobody timed.
    per_turn = [_turn_tool_union_ms(t) for t in turns]
    tool = _sum_measured(per_turn) if any(ms is not None for ms in per_turn) else None

    # `duration_seconds` is a non-optional float defaulting to 0.0, so there is
    # no None arm to write — but a 0.0 duration is a run that was never timed,
    # and subtracting real buckets from it renders a fabricated negative
    # residual. The evalboard keeps that null for the same reason; so do we.
    unaccounted = (
        result.duration_seconds * 1000.0 - (startup or 0.0) - (generation or 0.0) - (tool or 0.0) - (teardown or 0.0)
        if result.duration_seconds > 0.0
        else None
    )
    return TurnTimeBuckets(startup, generation, tool, teardown, unaccounted)


def _sum_measured(values: Iterable[float | None]) -> float | None:
    """Sum what was measured, or ``None`` when nothing was.

    The Python twin of the evalboard's ``sumMeasured``: a run with no measured
    value anywhere returns ``None`` (never measured), while a run that measured
    a genuine zero returns ``0.0``.
    """
    total: float | None = None
    for value in values:
        if isinstance(value, (int, float)) and math.isfinite(value):
            total = (total or 0.0) + value
    return total


def _turn_tool_union_ms(turn: TurnRecord) -> float | None:
    """One turn's tool execution — the UNION of its main-thread command spans.

    PREFERS THE STORED ``TurnRecord.tool_union_ms``, which the collector writes from
    the single span set it measures all four buckets against, so this surface and the
    collector are guaranteed to agree rather than merely observed to. The derivation
    is the LEGACY path for a ``task.json`` written before that field existed.

    The stored value is checked with ``is not None``, never truthiness: a stored
    ``0.0`` is a MEASUREMENT and must not fall through to a re-derivation.

    Rationale: .claude/notes/reporting.md § Read the stored value, do not re-derive it
    """
    if turn.tool_union_ms is not None:
        return turn.tool_union_ms
    spans = main_thread_tool_spans(turn.messages, turn.commands)
    return union_ms(spans) if spans else None


def has_final_reply(result: EvaluationResult) -> bool:
    """True iff any iteration emitted a non-empty ResultMessage.result.

    Mirrors the evalboard rendering: a "final reply" is a text answer the
    agent produced that becomes the trailing entry in the Turn timeline.
    """
    for t in result.iterations:
        if t.result_summary is not None:
            r = t.result_summary.result
            if isinstance(r, str) and r.strip():
                return True
    return False


def visible_turn_count(result: EvaluationResult) -> int:
    """Count of agent actions visible in the timeline so far.

    A "turn" here is one entry rendered in the Turn timeline: each tool
    invocation contributes 1, plus 1 for the final assistant reply when
    present. This is the canonical metric — distinct from the SDK's
    ``num_turns`` which counts assistant *messages* and can bundle tool
    use with trailing text into a single turn.
    """
    commands = sum(len(t.commands) for t in result.iterations)
    return commands + (1 if has_final_reply(result) else 0)


def recorded_run_limit(result: EvaluationResult, name: str) -> int | None:
    """The positive int ``run_limits.<name>`` from the recorded resolved config, else None."""
    task_cfg = result.task_config
    run_limits = (task_cfg.resolved or {}).get("run_limits") if task_cfg is not None else None
    value = run_limits.get(name) if isinstance(run_limits, dict) else None
    return value if isinstance(value, int) and value >= 1 else None


def expected_tool_calls_overage(result: EvaluationResult) -> tuple[int, int] | None:
    """``(visible_turns, expected)`` when the visible-events count strictly exceeds
    ``run_limits.expected_tool_calls``; else ``None``.
    """
    expected = recorded_run_limit(result, "expected_tool_calls")
    if expected is None:
        return None
    actual = visible_turn_count(result)
    if actual > expected:
        return actual, expected
    return None


def expected_turns_overage(result: EvaluationResult) -> tuple[int, int] | None:
    """``(model_turns, expected)`` when recorded model turns strictly exceed ``run_limits.expected_turns``."""
    expected = recorded_run_limit(result, "expected_turns")
    if expected is None or result.model_turns is None or result.model_turns <= expected:
        return None
    return result.model_turns, expected
