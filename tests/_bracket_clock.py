"""A ``TurnClock`` stand-in anchored far from real time, for the CE064 tests.

CE064 checks only that ``timestamp=`` is PRESENT on an ``AgentStartEvent`` /
``AgentEndEvent`` emit — its own declared blind spot is that it cannot tell
``self.clock.now()`` from a ``datetime.now()`` written out at the call site.
This is the guard for the SOURCE of that stamp, on the three harnesses that own
a clock.

ANCHORED FAR FROM NOW, and that is the whole trick. A bracket left on
``StreamEvent.timestamp``'s ``default_factory=datetime.now`` lands within
microseconds of a clock-derived one, so an assertion written against real time
would pass either way. Anchoring the stand-in a year out (the same device as
``tests/test_timing_identity_contract.py``'s ``EPOCH_MS``) makes a reverted
``timestamp=`` fail by a year rather than by a microsecond.

It advances on the REAL monotonic clock instead of stepping by hand, which is
what lets the same fixture assert the second half: with the bracket and the
window bounds finally on one basis, ``decompose_turn``'s head and tail come out
as small positive measurements rather than as the clamped ``0.0`` a cross-basis
subtraction produced (see ``ce064_turn_bracket_on_the_clock``'s measured probe).
"""

import time
from datetime import datetime, timedelta


#: Far enough from ``datetime.now()`` that a defaulted bracket cannot be mistaken
#: for a clock-derived one.
ANCHOR = datetime(2027, 1, 15, 0, 0, 0)


class AnchoredClock:
    """``TurnClock``'s shape, re-anchored: ``ANCHOR`` plus real monotonic elapsed."""

    def __init__(self) -> None:
        self._mono0 = time.monotonic()

    def now(self) -> datetime:
        return ANCHOR + timedelta(seconds=time.monotonic() - self._mono0)


def assert_bracket_on_the_clock(events: list) -> None:
    """Both turn brackets were stamped from the injected clock, not from ``now()``.

    Also asserts the pair is ordered, since a start and an end drawn from two
    different bases is exactly what produced the inverted antigravity tail.
    """
    from coder_eval.streaming.events import AgentEndEvent, AgentStartEvent

    starts = [e for e in events if isinstance(e, AgentStartEvent)]
    ends = [e for e in events if isinstance(e, AgentEndEvent)]
    assert len(starts) == 1, f"expected one AgentStartEvent, saw {len(starts)}"
    assert len(ends) == 1, f"expected one AgentEndEvent, saw {len(ends)}"
    for event in (*starts, *ends):
        assert event.timestamp >= ANCHOR, (
            f"{type(event).__name__}.timestamp is {event.timestamp}, which is not from the turn's "
            f"clock (anchored at {ANCHOR}). It fell back to StreamEvent's default_factory=datetime.now, "
            "so the turn bracket and the generation-window bounds sit on two bases inside one "
            "`decompose_turn` subtraction — see CE064."
        )
    assert ends[0].timestamp >= starts[0].timestamp


def assert_overhead_is_measured(record) -> None:
    """The turn's head and tail are real sub-second measurements on one basis.

    A cross-basis subtraction shows up here rather than in the stamps: the
    clamp in ``decompose_turn`` turns the negative into a ``0.0`` that reads as
    "measured, and instant". Bounding them well below the anchor offset is what
    proves both ends came from the same clock — a mixed pair would be off by
    about a year, not by a millisecond.
    """
    for name in ("harness_startup_ms", "harness_teardown_ms"):
        value = getattr(record, name)
        assert value is not None, f"{name} was never measured"
        assert 0.0 <= value < 60_000.0, f"{name} is {value} ms — the two ends are not on one clock"
