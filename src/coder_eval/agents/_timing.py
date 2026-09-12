"""Shared timing helpers for agent implementations.

Two harnesses interleave tool execution into a single generation window —
Antigravity (the Step for the tool arrives and only a later ``usage_metadata``
Step cuts the message) and Codex (``_flush_message``'s window is extended to
the last item's ``completed_at_ms``). Both must therefore subtract the tool
time from the window before publishing ``generation_duration_ms``, and both
must subtract the same thing: the UNION of the closed intervals, clipped to
the window.
"""

from datetime import datetime


def busy_ms(spans: list[tuple[datetime, datetime]], lo: datetime, hi: datetime) -> float:
    """Wall milliseconds inside ``[lo, hi]`` where at least ONE span was running.

    The union, not the sum. Tool intervals overlap in practice — Antigravity
    resolves several calls from one ``Step`` and backgrounds anything over ten
    seconds; Codex spawns collab agents that run concurrently — so adding
    their durations over-counts the busy time by exactly the overlap.
    Subtracting such a sum from a generation window understates generation
    and, with enough concurrency, drives it negative: four concurrent 400 ms
    calls inside a 1000 ms window sum to 1600 ms, clamping the result to the
    ``0.0`` that "unknown timing says unknown" exists to eliminate.

    Clipping to ``[lo, hi]`` is the other half: a tool that opened before this
    window only spent part of its life inside it, and only that part is not
    generation time here.
    """
    clipped = sorted((max(s, lo), min(e, hi)) for s, e in spans if min(e, hi) > max(s, lo))
    if not clipped:
        return 0.0
    total = 0.0
    open_start, open_end = clipped[0]
    for start, end in clipped[1:]:
        if start > open_end:  # disjoint — bank the run and start a new one
            total += (open_end - open_start).total_seconds() * 1000.0
            open_start, open_end = start, end
        else:  # overlapping or adjacent — extend the run
            open_end = max(open_end, end)
    return total + (open_end - open_start).total_seconds() * 1000.0
