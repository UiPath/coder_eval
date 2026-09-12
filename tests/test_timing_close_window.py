"""`close_window` — the one generation-window arithmetic the tiling reducers share.

Each of codex, opencode and pi had to get these cases right independently while
the body was copy-pasted; this file proves them once, against the helper. It is
the sensor for the arithmetic ITSELF, as distinct from the per-reducer tests,
which pin that a given reducer feeds it the right bounds and spans.
"""

from datetime import datetime, timedelta

import pytest

from coder_eval.timing import close_window


MARK = datetime(2026, 9, 11, 12, 0, 0)


def _at(ms: int) -> datetime:
    return MARK + timedelta(milliseconds=ms)


class TestCloseWindow:
    def test_no_tools_keeps_the_whole_window(self):
        started, generation_ms = close_window(mark=MARK, now=_at(1000), closed_spans=[], open_started_ats=[])
        assert started == MARK
        assert generation_ms == pytest.approx(1000.0)

    def test_a_contained_closed_tool_is_subtracted_once(self):
        _, generation_ms = close_window(
            mark=MARK,
            now=_at(1000),
            closed_spans=[(_at(200), _at(700))],
            open_started_ats=[],
        )
        assert generation_ms == pytest.approx(500.0)

    def test_overlapping_closed_tools_subtract_their_union_not_their_sum(self):
        # Two 500 ms calls overlapping by 400 ms occupy 600 ms of wall clock.
        # Summing them would leave 0 generation for a window that generated 400.
        _, generation_ms = close_window(
            mark=MARK,
            now=_at(1000),
            closed_spans=[(_at(100), _at(600)), (_at(200), _at(700))],
            open_started_ats=[],
        )
        assert generation_ms == pytest.approx(400.0)

    def test_an_open_tool_is_bounded_at_now(self):
        # Still running when the window closes: it owns [300, 1000], not nothing.
        _, generation_ms = close_window(mark=MARK, now=_at(1000), closed_spans=[], open_started_ats=[_at(300)])
        assert generation_ms == pytest.approx(300.0)

    def test_a_tool_straddling_the_mark_is_clipped_to_the_post_mark_part(self):
        # The pre-mark half belongs to the PREVIOUS window, which already
        # subtracted it. Counting it again here would over-subtract.
        _, generation_ms = close_window(
            mark=MARK,
            now=_at(1000),
            closed_spans=[(_at(-400), _at(300))],
            open_started_ats=[],
        )
        assert generation_ms == pytest.approx(700.0)

    def test_item_start_before_the_mark_wins(self):
        # A stamp that went backwards: the window must cover the item, so the
        # min() moves the start back rather than inverting the span.
        started, generation_ms = close_window(
            mark=MARK,
            now=_at(1000),
            item_start=_at(-200),
            closed_spans=[],
            open_started_ats=[],
        )
        assert started == _at(-200)
        assert generation_ms == pytest.approx(1200.0)

    def test_item_start_after_the_mark_keeps_the_mark(self):
        # The normal tiling case: the gap between the previous close and this
        # item's first stamp IS model time and belongs inside the window.
        started, generation_ms = close_window(
            mark=MARK,
            now=_at(1000),
            item_start=_at(400),
            closed_spans=[],
            open_started_ats=[],
        )
        assert started == MARK
        assert generation_ms == pytest.approx(1000.0)

    def test_an_inverted_window_clamps_to_zero_rather_than_going_negative(self):
        started, generation_ms = close_window(mark=_at(1000), now=MARK, closed_spans=[], open_started_ats=[])
        assert started == _at(1000)
        assert generation_ms == 0.0

    def test_an_open_tool_starting_after_now_is_ignored(self):
        _, generation_ms = close_window(mark=MARK, now=_at(1000), closed_spans=[], open_started_ats=[_at(1500)])
        assert generation_ms == pytest.approx(1000.0)

    def test_an_open_tool_starting_exactly_at_now_is_ignored(self):
        _, generation_ms = close_window(mark=MARK, now=_at(1000), closed_spans=[], open_started_ats=[_at(1000)])
        assert generation_ms == pytest.approx(1000.0)

    def test_closed_and_open_spans_are_unioned_together(self):
        # A closed [100, 400] and an open from 300 bounded at 1000 union to
        # [100, 1000] — 900 ms busy, 100 ms of generation.
        _, generation_ms = close_window(
            mark=MARK,
            now=_at(1000),
            closed_spans=[(_at(100), _at(400))],
            open_started_ats=[_at(300)],
        )
        assert generation_ms == pytest.approx(100.0)

    def test_tools_covering_the_whole_window_leave_zero_not_a_negative(self):
        _, generation_ms = close_window(
            mark=MARK,
            now=_at(1000),
            closed_spans=[(_at(-500), _at(1500))],
            open_started_ats=[],
        )
        assert generation_ms == 0.0

    def test_mark_is_keyword_only_and_has_no_default(self):
        # A reducer cannot open a window without STATING what it tiles from.
        # The value is still the caller's to get right — see the docstring.
        with pytest.raises(TypeError):
            close_window(MARK, _at(1000), closed_spans=[], open_started_ats=[])  # type: ignore[misc]
        with pytest.raises(TypeError):
            close_window(now=_at(1000), closed_spans=[], open_started_ats=[])  # type: ignore[call-arg]


class TestTurnClock:
    """One (wall, monotonic) pair per turn, every later stamp derived from it."""

    def test_successive_reads_never_go_backwards(self):
        from coder_eval.timing import TurnClock

        clock = TurnClock()
        stamps = [clock.now() for _ in range(50)]
        assert stamps == sorted(stamps)

    def test_a_derived_stamp_advances_by_the_monotonic_delta(self):
        import time as _time

        from coder_eval.timing import TurnClock

        clock = TurnClock()
        before = clock.now()
        mono_before = _time.monotonic()
        while _time.monotonic() - mono_before < 0.01:
            pass
        elapsed_ms = (_time.monotonic() - mono_before) * 1000.0
        derived_ms = (clock.now() - before).total_seconds() * 1000.0
        assert derived_ms == pytest.approx(elapsed_ms, abs=5.0)

    def test_a_fresh_clock_anchors_on_its_own_pair(self):
        """Each clock holds its OWN (wall, monotonic) origin — the per-turn part.

        Note what this deliberately does NOT assert: that two clocks report
        different times. They should AGREE, and closely, because both derive
        from the same monotonic source — re-anchoring exists to correct drift
        against real wall time, not to introduce an offset. An earlier version
        of this test asserted `second.now() != first.now()`; that passed only
        on sub-microsecond skew between the two constructors' reads, so it was
        flaky under load and asserted the opposite of the design.
        """
        import time as _time

        from coder_eval.timing import TurnClock

        first = TurnClock()
        mono = _time.monotonic()
        while _time.monotonic() - mono < 0.005:
            pass
        second = TurnClock()

        assert second._mono0 > first._mono0
        assert second._wall0 >= first._wall0
