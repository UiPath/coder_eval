"""`close_window` — the one generation-window arithmetic the tiling reducers share.

Each of codex, opencode and pi had to get these cases right independently while
the body was copy-pasted; this file proves them once, against the helper. It is
the sensor for the arithmetic ITSELF, as distinct from the per-reducer tests,
which pin that a given reducer feeds it the right bounds and spans.
"""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coder_eval.timing import busy_ms, close_window, decompose_turn, union_ms


MARK = datetime(2026, 9, 11, 12, 0, 0)


def _at(ms: int) -> datetime:
    return MARK + timedelta(milliseconds=ms)


def _load_decompose_run():
    """Import `scripts/timing/decompose_run.py`, which is not an importable package."""
    path = Path(__file__).parents[1] / "scripts" / "timing" / "decompose_run.py"
    spec = importlib.util.spec_from_file_location("decompose_run_for_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


class TestNaiveAwareMix:
    """A mixed naive/aware pair fails loudly at the seam, not cryptically inside it.

    `decompose_turn`'s bare arithmetic raised `TypeError: can't subtract
    offset-naive and offset-aware datetimes` straight out of
    `EventCollector.build_turn_record`, killing the turn with a message naming
    neither the field nor the harness.

    Unreachable from this repo — every stamp in `agents/` and `streaming/` is a
    naive `datetime.now()`. The exposure is a THIRD-PARTY agent registered
    through the `coder_eval.plugins` SPI, which lives outside
    `src/coder_eval/agents/` and which a lint rule scoped to that directory
    could never see. That is why this is a guard and not a rule.
    """

    AWARE = MARK.replace(tzinfo=UTC)

    def test_a_mixed_head_names_the_field(self):
        with pytest.raises(TypeError, match="harness_startup_ms"):
            decompose_turn(self.AWARE, None, MARK, None)

    def test_a_mixed_tail_names_the_field(self):
        with pytest.raises(TypeError, match="harness_teardown_ms"):
            decompose_turn(None, self.AWARE, None, MARK)

    def test_a_mixed_busy_ms_window_names_the_field(self):
        with pytest.raises(TypeError, match="busy_ms window"):
            busy_ms([(MARK, _at(500))], MARK, self.AWARE)

    def test_a_mixed_span_start_is_caught_too_and_not_by_the_bare_comparison(self):
        """The clipping compares each span against the window.

        Left unguarded that raises "can't compare offset-naive and offset-aware
        datetimes" — the exact message this replaces — so checking only the
        bounds would leave the guard not covering its own function.
        """
        with pytest.raises(TypeError, match="tool span's start"):
            busy_ms([(self.AWARE, self.AWARE)], MARK, _at(1000))

    def test_a_mixed_span_end_is_caught_by_its_own_branch(self):
        """The end is a separate check against `hi`, so it needs its own case.

        A span whose START matches the window and whose END does not passes the
        previous branch and must still raise — otherwise that branch is live,
        reachable and unexercised.
        """
        with pytest.raises(TypeError, match="tool span's end"):
            busy_ms([(MARK, self.AWARE)], MARK, _at(1000))

    def test_one_wording_for_every_call_site(self):
        """One helper, so one template — checked across ALL FIVE call sites.

        Two inline guards would drift, and a test asserting the text would then
        pin only whichever one it happened to call. What varies between sites
        is deliberate and only that: the field name, and which side is aware.
        Everything after that clause is the advice, and it must be identical or
        the sites are no longer sharing a helper.
        """
        advice = set()
        for call in (
            lambda: decompose_turn(self.AWARE, None, MARK, None),  # head
            lambda: decompose_turn(None, self.AWARE, None, MARK),  # tail
            lambda: busy_ms([(MARK, _at(500))], MARK, self.AWARE),  # window bounds
            lambda: busy_ms([(self.AWARE, self.AWARE)], MARK, _at(1000)),  # span start
            lambda: busy_ms([(MARK, self.AWARE)], MARK, _at(1000)),  # span end
        ):
            with pytest.raises(TypeError) as excinfo:
                call()
            message = str(excinfo.value)
            assert "is timezone-aware and the other is naive" in message
            advice.add(message.split("), ", 1)[1])
        assert len(advice) == 1, advice

    def test_all_naive_is_unchanged(self):
        assert decompose_turn(_at(1000), _at(2000), MARK, _at(3000)) == (1000.0, 1000.0)

    def test_all_aware_works_because_the_guard_is_about_the_mix(self):
        def aware(ms: int) -> datetime:
            return _at(ms).replace(tzinfo=UTC)

        assert decompose_turn(aware(1000), aware(2000), self.AWARE, aware(3000)) == (1000.0, 1000.0)

    def test_an_empty_span_list_is_not_checked_at_all(self):
        """Not even the bounds, and the MIXED case is the one that proves it.

        With no spans the comprehension never runs: nothing is compared and
        nothing is subtracted, so there is no pair for the guard to be about.
        Checking the bounds anyway rejected a call that has always returned
        `0.0` — asserted here on mixed bounds, because the naive case would
        pass either way and so could not tell the two behaviours apart.
        """
        assert busy_ms([], MARK, _at(1000)) == 0.0
        assert busy_ms([], MARK, self.AWARE) == 0.0


class TestUnionMs:
    """`union_ms` is `busy_ms` over the spans' own extent.

    Extracted because the golden sensor (`tests/_fixtures/golden_streams/_scrub.py`)
    and the live residual gate (`scripts/timing/decompose_run.py`) had copied
    that same `min`/`max`/`busy_ms` tail. Both answer the same question about
    the same recorded commands, so the two copies could only ever agree by
    hand — `test_the_two_recorded_command_readers_agree` below is the half
    that pins them together.
    """

    def test_no_spans_is_zero_not_a_min_of_an_empty_sequence(self):
        assert union_ms([]) == 0.0

    def test_overlapping_spans_are_the_union_not_the_sum(self):
        # Two 500 ms calls overlapping by 400 ms occupy 600 ms of wall clock.
        assert union_ms([(_at(100), _at(600)), (_at(200), _at(700))]) == pytest.approx(600.0)

    def test_disjoint_spans_add(self):
        assert union_ms([(_at(100), _at(200)), (_at(400), _at(900))]) == pytest.approx(600.0)

    def test_the_extent_is_the_spans_own_bounds(self):
        # No window is passed, so nothing clips: a span far from the origin is
        # measured in full rather than dropped as out of range.
        assert union_ms([(_at(10_000), _at(10_250))]) == pytest.approx(250.0)

    def test_the_two_recorded_command_readers_agree(self):
        """`_scrub.py` and `decompose_run.py` must report one tool total.

        They read the SAME `task.json` shape — the golden sensor from a dumped
        record, the gate from the file on disk — and a divergence would let one
        pass while the other failed on identical bytes. They keep their own
        stamp parsing (the inputs differ in how they are reached); the union
        tail is what this pins.
        """
        from tests._fixtures.golden_streams._scrub import _tool_union_ms

        # Loaded by path: `scripts/` is deliberately not a package (it sits
        # outside the Makefile's LINT_PATHS), so there is no import to make.
        _tool_ms = _load_decompose_run()._tool_ms

        turn = {
            "commands": [
                {"execution_started_at": _at(100).isoformat(), "execution_completed_at": _at(600).isoformat()},
                {"execution_started_at": _at(200).isoformat(), "execution_completed_at": _at(700).isoformat()},
                # Never timed: contributes nothing on either side.
                {"execution_started_at": None, "execution_completed_at": None},
                # Inverted bounds: both readers drop these while BUILDING their
                # span list, which is why `union_ms` does not filter them.
                {"execution_started_at": _at(900).isoformat(), "execution_completed_at": _at(800).isoformat()},
            ]
        }
        assert _tool_union_ms(turn) == pytest.approx(600.0)
        assert _tool_ms(turn) == _tool_union_ms(turn)


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
