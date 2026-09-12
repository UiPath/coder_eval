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

from coder_eval.timing import busy_ms, close_window, decompose_turn, main_thread_tool_spans, union_ms


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
    """The RAW window: where it opens, where it ends, and the clamp.

    The tool subtraction these cases used to cover moved to
    `timing.py::subtract_tool_time`, where it happens once for all
    five harnesses instead of five times in five reducers — see
    `tests/test_event_collector.py::TestSubtractToolTime`, which carries the
    union, grouping, clamping and non-mutation cases. What is left here is the
    part that is genuinely per-reducer: the mark.
    """

    def test_the_window_is_the_whole_span_from_the_mark(self):
        started, span_ms = close_window(mark=MARK, now=_at(1000))
        assert started == MARK
        assert span_ms == pytest.approx(1000.0)

    def test_item_start_before_the_mark_wins(self):
        # A stamp that went backwards: the window must cover the item, so the
        # min() moves the start back rather than inverting the span.
        started, span_ms = close_window(mark=MARK, now=_at(1000), item_start=_at(-200))
        assert started == _at(-200)
        assert span_ms == pytest.approx(1200.0)

    def test_item_start_after_the_mark_keeps_the_mark(self):
        # The normal tiling case: the gap between the previous close and this
        # item's first stamp IS model time and belongs inside the window.
        started, span_ms = close_window(mark=MARK, now=_at(1000), item_start=_at(400))
        assert started == MARK
        assert span_ms == pytest.approx(1000.0)

    def test_an_inverted_window_clamps_to_zero_rather_than_going_negative(self):
        started, span_ms = close_window(mark=_at(1000), now=MARK)
        assert started == _at(1000)
        assert span_ms == 0.0

    def test_mark_is_keyword_only_and_has_no_default(self):
        # A reducer cannot open a window without STATING what it tiles from.
        # The value is still the caller's to get right — see the docstring.
        with pytest.raises(TypeError):
            close_window(MARK, _at(1000))  # type: ignore[misc]
        with pytest.raises(TypeError):
            close_window(now=_at(1000))  # type: ignore[call-arg]

    def test_it_no_longer_accepts_the_span_arguments_that_moved(self):
        """The subtraction moved; the parameters must not linger as no-ops.

        A reducer still passing `closed_spans=` would otherwise keep compiling
        while its tool time was silently subtracted a second time centrally.
        """
        with pytest.raises(TypeError):
            close_window(mark=MARK, now=_at(1000), closed_spans=[])  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            close_window(mark=MARK, now=_at(1000), open_started_ats=[])  # type: ignore[call-arg]


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
        pass while the other failed on identical bytes. Both now validate into a
        `TurnRecord` and call the one typed selector, so what this pins is that
        neither has quietly grown a second path back.
        """
        from tests._fixtures.golden_streams._scrub import _tool_union_ms

        # Loaded by path: `scripts/` is deliberately not a package (it sits
        # outside the Makefile's LINT_PATHS), so there is no import to make.
        _tool_ms = _load_decompose_run()._tool_ms

        turn = _turn(
            commands=[
                _command("a", _at(100), _at(600)),
                _command("b", _at(200), _at(700)),
                # Never timed: contributes nothing on either side.
                _command("c", None, None),
                # Inverted bounds: dropped while BUILDING the span list, which
                # is why `union_ms` itself does not filter them.
                _command("d", _at(900), _at(800)),
            ]
        )
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


def _command(tool_id: str, started, completed) -> dict:
    """A recorded command, MODEL-VALID: both sensors validate before selecting."""
    return {
        "tool_id": tool_id,
        "tool_name": "Bash",
        "timestamp": (started or _at(0)).isoformat(),
        "execution_started_at": started.isoformat() if started is not None else None,
        "execution_completed_at": completed.isoformat() if completed is not None else None,
    }


def _turn(*, commands: list[dict], messages: list[dict] | None = None, duration_seconds: float = 3.0) -> dict:
    """A `task.json` turn dict that validates as a `TurnRecord`.

    The two sensors no longer parse stamps out of a raw dict; they validate and
    call the typed selector, so a fixture below the model's required fields
    would fail in validation rather than on the thing the test is about.
    """
    return {
        "iteration": 1,
        "user_input": "",
        "agent_output": "",
        "duration_seconds": duration_seconds,
        "commands": commands,
        "messages": messages or [],
    }


class TestTheThreeToolUnionsAgree:
    """Three readers answer "how long did this turn's tools run". They must agree.

    * `timing.main_thread_tool_spans` + `union_ms` — the TYPED selector the
      collector subtracts from its generation windows and measures the head and
      tail against, and which now writes `TurnRecord.tool_union_ms`.
    * `tests/_fixtures/golden_streams/_scrub.py::_tool_union_ms` — the golden
      corpus's identity check, which validates the dump and calls that selector.
    * `scripts/timing/decompose_run.py::_tool_ms` — the LIVE two-sided residual
      gate, which `.github/workflows/pr-checks.yml` runs against a real run, and
      which does the same.

    The three used to be three COPIES of the selection rule, and they agreed by
    luck once at a real cost: the collector filtered its GENERATIONS to the main
    thread and then passed EVERY command as a tool span. A child nests inside
    the parent Agent call, whose interval the union already covers, so nothing
    failed — but Codex's recovered child tools carry the CHILD's clock, so the
    nesting is not guaranteed. Now there is ONE selector and two callers of it,
    and what remains worth pinning is that neither sensor has grown a second
    path back, and that each still computes its OWN union rather than reading
    the producer's stored answer.
    """

    @staticmethod
    def _record() -> dict:
        """A turn with a sub-agent whose own tool sits OUTSIDE the parent call.

        Inside, the three agree whatever they filter, so the fixture has to put
        the child's tool where the parent's interval does not cover it.
        """
        return _turn(
            duration_seconds=3.0,
            commands=[
                _command("agent-call", _at(1000), _at(1500)),
                _command("child-tool", _at(2000), _at(2400)),
            ],
            messages=[
                {
                    "role": "assistant",
                    "started_at": _at(0).isoformat(),
                    "completed_at": _at(1000).isoformat(),
                    "parent_tool_use_id": None,
                    "tool_use_ids": ["agent-call"],
                },
                {
                    "role": "assistant",
                    "started_at": _at(2000).isoformat(),
                    "completed_at": _at(2400).isoformat(),
                    "parent_tool_use_id": "agent-call",
                    "tool_use_ids": ["child-tool"],
                },
            ],
        )

    def test_the_two_recomputing_readers_exclude_the_sub_agent_tool(self):
        from tests._fixtures.golden_streams._scrub import _tool_union_ms

        tool_ms = _load_decompose_run()._tool_ms
        record = self._record()
        # Only the parent Agent call's own 500 ms. Counting the child's 400 ms
        # books time no main-thread bucket claims.
        assert _tool_union_ms(record) == pytest.approx(500.0)
        assert tool_ms(record) == pytest.approx(500.0)

    def test_the_collector_excludes_it_too(self):
        from coder_eval.models import AssistantMessage, CommandTelemetry
        from coder_eval.streaming.collector import EventCollector
        from coder_eval.streaming.events import ToolEndEvent

        collector = EventCollector()
        for tool_id, lo, hi in (("agent-call", 1000, 1500), ("child-tool", 2000, 2400)):
            collector.on_event(
                ToolEndEvent(
                    task_id="t",
                    turn_id="t1",
                    tool=CommandTelemetry(
                        tool_name="Agent",
                        tool_id=tool_id,
                        timestamp=_at(lo),
                        execution_started_at=_at(lo),
                        execution_completed_at=_at(hi),
                        result_status="success",
                    ),
                )
            )
        messages = [
            AssistantMessage(
                started_at=_at(0), completed_at=_at(1000), generation_duration_ms=1000.0, tool_use_ids=["agent-call"]
            ),
            AssistantMessage(
                started_at=_at(2000),
                completed_at=_at(2400),
                generation_duration_ms=400.0,
                parent_tool_use_id="agent-call",
                tool_use_ids=["child-tool"],
            ),
        ]
        spans = main_thread_tool_spans(messages, collector._commands.values())
        assert union_ms(spans) == pytest.approx(500.0), "the same 500 ms the other two report"


class TestTheSensorsCrossCheckTheStoredUnion:
    """Each sensor verifies the producer's BOOKKEEPING, not its answer.

    `TurnRecord.tool_union_ms` is written by the collector from the span set it
    measures all four buckets against. A sensor that simply READ it would stop
    being a sensor — it would restate the implementation. So each computes its
    own union from the commands and asserts the stored value agrees, which
    catches exactly the class of defect this branch kept producing: a span that
    reached one consumer and not the other.
    """

    @staticmethod
    def _scrub_check(turn: dict) -> None:
        from tests._fixtures.golden_streams._scrub import assert_timing_captured

        assert_timing_captured(turn, expect_generation_window=False, check_identity=True)

    @staticmethod
    def _generating_turn(*, stored: float | None, tool_ms: float = 500.0) -> dict:
        turn = _turn(
            duration_seconds=10.0,
            commands=[_command("t1", _at(1000), _at(1000 + tool_ms))],
            messages=[
                {
                    "role": "assistant",
                    "started_at": _at(0).isoformat(),
                    "completed_at": _at(1000).isoformat(),
                    "generation_duration_ms": 1000.0,
                }
            ],
        )
        turn["harness_startup_ms"] = 0.0
        turn["harness_teardown_ms"] = 5.0
        if stored is not None:
            turn["tool_union_ms"] = stored
        return turn

    def test_the_golden_sensor_fails_when_the_stored_value_disagrees(self):
        with pytest.raises(AssertionError, match="tool_union_ms is"):
            self._scrub_check(self._generating_turn(stored=999.0))

    def test_the_golden_sensor_passes_when_it_agrees(self):
        self._scrub_check(self._generating_turn(stored=500.0))

    def test_the_golden_sensor_skips_a_record_without_the_field(self):
        """The legacy case, and the common one for records already on disk."""
        self._scrub_check(self._generating_turn(stored=None))

    def test_the_live_gate_reports_a_disagreement(self):
        breach = _load_decompose_run()._union_breach(self._generating_turn(stored=999.0))
        assert breach is not None
        assert "999.000000" in breach and "500.000000" in breach

    def test_the_live_gate_is_silent_when_they_agree_or_the_field_is_absent(self):
        union_breach = _load_decompose_run()._union_breach
        assert union_breach(self._generating_turn(stored=500.0)) is None
        assert union_breach(self._generating_turn(stored=None)) is None

    def test_a_sub_agent_tool_outside_the_parent_call_is_excluded_by_all_three(self):
        """One record carrying every shape the selection rule has to decide.

        A sub-agent tool NESTED inside its parent call is covered by the
        parent's own interval whatever anyone filters, so the fixture puts one
        outside it — which is the only arrangement in which a missing filter
        changes the answer.
        """
        from tests._fixtures.golden_streams._scrub import _tool_union_ms

        turn = _turn(
            duration_seconds=10.0,
            commands=[
                _command("agent-call", _at(1000), _at(2000)),
                _command("nested-child", _at(1200), _at(1400)),
                _command("outside-child", _at(3000), _at(3400)),
                _command("unbounded", None, None),
                _command("inverted", _at(5000), _at(4000)),
            ],
            messages=[
                {
                    "role": "assistant",
                    "started_at": _at(0).isoformat(),
                    "completed_at": _at(1000).isoformat(),
                    "generation_duration_ms": 1000.0,
                    "tool_use_ids": ["agent-call", "unbounded", "inverted"],
                },
                {
                    "role": "assistant",
                    "started_at": _at(1200).isoformat(),
                    "completed_at": _at(3400).isoformat(),
                    "generation_duration_ms": 400.0,
                    "parent_tool_use_id": "agent-call",
                    "tool_use_ids": ["nested-child", "outside-child"],
                },
            ],
        )
        # Only the parent Agent call's own 1000 ms: both children excluded, the
        # unbounded one unplaceable, the inverted one dropped.
        expected = 1000.0
        assert _tool_union_ms(turn) == pytest.approx(expected)
        assert _load_decompose_run()._tool_ms(turn) == pytest.approx(expected)

        from coder_eval.models import TurnRecord

        record = TurnRecord.model_validate(turn)
        assert union_ms(main_thread_tool_spans(record.messages, record.commands)) == pytest.approx(expected)

    def test_a_turn_missing_messages_or_commands_produces_an_empty_span_set(self):
        from coder_eval.models import TurnRecord
        from tests._fixtures.golden_streams._scrub import _tool_union_ms

        for turn in ({"iteration": 1, "user_input": "", "agent_output": ""},):
            record = TurnRecord.model_validate(turn)
            assert main_thread_tool_spans(record.messages, record.commands) == []
            assert _tool_union_ms(turn) == 0.0
            assert _load_decompose_run()._tool_ms(turn) == 0.0
