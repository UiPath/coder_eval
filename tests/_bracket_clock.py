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

from coder_eval.models import TurnRecord
from coder_eval.streaming.events import AgentEndEvent, AgentStartEvent, StreamEvent


#: Far enough from ``datetime.now()`` that a defaulted bracket cannot be mistaken
#: for a clock-derived one. RELATIVE, never a written date: a fixed anchor stops
#: discriminating the moment wall time passes it, and the assertion below would
#: then be satisfied by exactly the `datetime.now()` stamp it exists to reject —
#: a green sensor measuring nothing, on a date nobody would connect to the test.
ANCHOR = datetime.now() + timedelta(days=365)


class AnchoredClock:
    """``TurnClock``'s shape, re-anchored: ``ANCHOR`` plus real monotonic elapsed."""

    def __init__(self) -> None:
        self._mono0 = time.monotonic()

    def now(self) -> datetime:
        return ANCHOR + timedelta(seconds=time.monotonic() - self._mono0)


def assert_bracket_on_the_clock(events: list[StreamEvent]) -> None:
    """Both turn brackets were stamped from the injected clock, not from ``now()``.

    Also asserts the pair is ordered, since a start and an end drawn from two
    different bases is exactly what produced the inverted antigravity tail.
    """
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


def assert_overhead_is_measured(record: TurnRecord) -> None:
    """The turn's head and tail are real measurements taken on one basis.

    The two ends fail differently, and each needs its own assertion.

    A defaulted ``AgentStartEvent`` lands ~365 days before the clock-derived
    first window, so the HEAD blows any sane bound by that whole offset — the
    upper bound is what catches it.

    A defaulted ``AgentEndEvent`` fails the other way: it lands ~365 days
    BEFORE its own last message, so ``decompose_turn`` clamps the negative and
    publishes ``0.0`` — "measured, and instant", which sails through an upper
    bound. Only a strict ``> 0.0`` catches it, and it holds on all three
    harnesses because a turn's last flush and its end event are separated by
    real work. The margin is small where it is smallest: antigravity holds its
    process across turns and measures 0.007-0.03 ms here, which is 7-30 ticks
    of the 1 us resolution both `datetime` and `time.monotonic()` have on
    Linux, macOS and Windows. That is the magnitude the clamped defect hid, so
    do not relax this to ``>= 0.0`` — a zero is the defect.
    """
    assert record.harness_startup_ms is not None, "harness_startup_ms was never measured"
    assert record.harness_teardown_ms is not None, "harness_teardown_ms was never measured"
    assert 0.0 <= record.harness_startup_ms < 60_000.0, (
        f"harness_startup_ms is {record.harness_startup_ms} ms — the bracket and the first "
        "window bound are not on one clock"
    )
    assert 0.0 < record.harness_teardown_ms < 60_000.0, (
        f"harness_teardown_ms is {record.harness_teardown_ms} ms — a 0.0 here is the clamped "
        "inversion CE064 exists to remove, not an instant teardown"
    )
