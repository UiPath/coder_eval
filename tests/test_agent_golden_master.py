"""Golden-master characterization tests for the agent turn-loops.

This is the safety net for decomposing ``ClaudeCodeAgent.communicate`` and
``CodexAgent._run_turn_with_streaming``: each scenario replays a recorded SDK
event stream through ``communicate()`` and asserts the resulting
``TurnRecord`` / ``pending_turn`` is byte-identical (post-scrub) to a committed
JSON snapshot. The decomposition must not change any snapshot.

Regenerate the snapshots after an INTENTIONAL behavior change with::

    GOLDEN_REGEN=1 uv run pytest tests/test_agent_golden_master.py

and review the resulting JSON diff before committing.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import pytest

from coder_eval.models import AgentKind
from tests._fixtures.golden_streams import assert_reconciliation, assert_timing_captured, scrub
from tests._fixtures.golden_streams.antigravity_fixtures import ANTIGRAVITY_SCENARIOS, run_antigravity_scenario
from tests._fixtures.golden_streams.claude_fixtures import CLAUDE_SCENARIOS, run_claude_scenario
from tests._fixtures.golden_streams.opencode_fixtures import OPENCODE_SCENARIOS, run_opencode_scenario
from tests._fixtures.golden_streams.pi_fixtures import PI_SCENARIOS, run_pi_scenario


# Codex is an optional extra (mirrors test_codex_agent's guard). Import its
# fixtures only when present so the Claude golden tests in this module still
# collect and run in a base (no-codex) environment instead of erroring at import.
_HAS_CODEX = importlib.util.find_spec("openai_codex") is not None
if _HAS_CODEX:
    from tests._fixtures.golden_streams.codex_fixtures import CODEX_SCENARIOS, run_codex_scenario
else:  # pragma: no cover - only without the optional codex extra
    CODEX_SCENARIOS = []
    run_codex_scenario = None


# Scenarios that legitimately produce no measurable generation window. Strict
# is the default: a new scenario is asserted to have one until it is named
# here, so a harness that silently stops recording windows fails instead of
# passing. Each entry carries the reason it cannot have one.
NO_GENERATION_WINDOW: frozenset[str] = frozenset(
    {
        "claude_g_crash_format_placeholder",  # crash partial: 0 assistant messages
        "claude_h1_timeout_process_error",  # timeout partial: 0 assistant messages
        "claude_h2_process_error_crash",  # crash partial: 0 assistant messages
        # Drives a SCRIPTED monotonic clock (a constant 1000.0 until the
        # deadline flips) so the deadline break is deterministic. The window
        # is zero by fixture construction, not by anything the harness did.
        "claude_i_in_loop_deadline_break",
        "codex_g_items_rebuild",  # rollout rebuild: Turn items carry no timestamps
        # Codex emissions whose ENTIRE measurable window was tool execution.
        # The window is subtracted down to 0 because that is the honest
        # answer, not because nothing was recorded — see the generation-window
        # subtraction in codex_agent._flush_message.
        "codex_d_cross_flush_is_error",  # flush lands before the tool completes: zero-width window
        "codex_e_orphan_tool",  # the tool never completes, so the window never opens
        # Same shape, reached from the opposite direction. This scenario injects
        # a 5 ms CLI tool interval into a replay whose whole turn is well under
        # one millisecond, so the tool spans BOTH windows entirely and the
        # central subtraction takes each down to a measured 0.0. It is the tool
        # interval that is fictional, not the subtraction — which is why the
        # scenario is in FICTIONAL_DURATIONS too.
        #
        # BE HONEST ABOUT WHAT IS LEFT. With both exemptions on, this snapshot
        # asserts neither the identity nor a positive window, and it does NOT
        # record the tiling the scenario is named for — `SCRUB_KEYS` masks
        # `started_at`, `completed_at` and `generation_duration_ms`, so nothing
        # about where a window opened survives into the JSON. What it still
        # pins is the STRUCTURE: two assistant messages, their content blocks,
        # their token buckets, and one resolved command. OpenCode's tiling is
        # asserted where it can be — `tests/test_timing_identity_contract.py`
        # (scripted clock, ms-exact) and
        # `tests/test_opencode_agent.py::TestGenerationWindowsTileTheTurn`.
        # `pi_c_multi_turn_tiling` is the same scenario shape on a harness whose
        # stamps come from its own clock, and it needs neither exemption.
        "opencode_c_multi_step_tiling",
    }
)


def _expect_window(harness: str, scenario_name: str) -> bool:
    return f"{harness}_{scenario_name}" not in NO_GENERATION_WINDOW


# Scenarios that inject their own SDK timestamps, so their recorded durations
# are FICTIONAL and cannot be reconciled against the replay's real wall clock.
# `_rebase_notifications` / `_rebase_lines` put those stamps on the replay's
# clock, which fixes the era — but the SDK's stamps are integer MILLISECONDS
# and these scenarios declare 17-900 ms of item time, while the replay itself
# runs in well under one. No rebasing closes that; the agent's own clock would
# have to be faked too. Everything else — every claude, antigravity and pi
# scenario, and the codex/opencode ones that inject nothing — is checked.
#
# The last two entries were ADDED to buy stability, and the trade is worth
# stating. They previously injected NO stamps at all, so `_flush_message` took
# `_ms_to_dt(None)` for both window bounds — two adjacent `datetime.now()`
# reads, which collide at microsecond resolution often enough that
# `assert_timing_captured`'s `completed_at > started_at` failed roughly one run
# in twenty under parallel load, naming a different scenario each time. Their
# identity check was near-vacuous anyway (a window of width zero reconciles
# trivially), so giving them real bounds trades that for a stable, meaningful
# bounds-span assertion.
FICTIONAL_DURATIONS: frozenset[str] = frozenset(
    {
        "codex_b_command_execution",  # 250 ms command + 150 ms generation
        "codex_c_reasoning_placeholder",  # 300 ms of item time — see below
        "codex_d_cross_flush_is_error",  # 400 ms command
        "codex_e_orphan_tool",  # command started, never completed
        "codex_f_collab_fallback",  # 900 ms collab wait
        "codex_h_no_turn_completed_crash",  # 200 ms of item time — see below
        "opencode_b_tool_call_resolved",  # 17 ms tool interval
        # 5 ms tool interval, injected as CLI epoch stamps. OpenCode takes its
        # tool bounds from the CLI payload rather than from its own clock, so
        # every scenario of this harness that resolves a tool injects them —
        # there is no version of this scenario that stays commensurable with a
        # sub-millisecond replay. Its TILING property (the second window opens
        # at the first `step_finish`) is what the scenario is for, and that is
        # still snapshotted; the identity is asserted for this harness by
        # tests/test_timing_identity_contract.py, on a scripted clock.
        "opencode_c_multi_step_tiling",
    }
)


def _check_identity(harness: str, scenario_name: str) -> bool:
    return f"{harness}_{scenario_name}" not in FICTIONAL_DURATIONS


_EXPECTED_DIR = Path(__file__).parent / "_fixtures" / "golden_streams" / "expected"
_REGEN = os.environ.get("GOLDEN_REGEN", "").strip().lower() in {"1", "true", "yes", "on"}


def _compare_or_regen(name: str, actual_scrubbed: dict[str, Any]) -> None:
    """Compare a scrubbed snapshot to its committed JSON, or regenerate it."""
    path = _EXPECTED_DIR / f"{name}.json"
    serialized = json.dumps(actual_scrubbed, indent=2, sort_keys=True)

    if _REGEN:
        path.write_text(serialized + "\n", encoding="utf-8")
        return

    assert path.exists(), (
        f"Missing golden snapshot {path.name}. Generate it once with "
        f"`GOLDEN_REGEN=1 uv run pytest tests/test_agent_golden_master.py` and commit it."
    )
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert actual_scrubbed == expected, (
        f"Golden snapshot drift for {name!r}. The turn-loop output changed.\n"
        f"If this change is intentional, regenerate with GOLDEN_REGEN=1 and review the diff."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", CLAUDE_SCENARIOS, ids=lambda s: s.name)
async def test_claude_golden(scenario, tmp_path):
    raw = await run_claude_scenario(scenario, str(tmp_path))
    # Reconciliation is asserted on the UNscrubbed dump (token buckets are never
    # scrubbed, but cost/timestamps are — assert before masking to be explicit).
    assert_reconciliation(raw)
    assert_timing_captured(
        raw,
        expect_generation_window=_expect_window("claude", scenario.name),
        check_identity=_check_identity("claude", scenario.name),
    )
    _compare_or_regen(f"claude_{scenario.name}", scrub(raw))


@pytest.mark.skipif(not _HAS_CODEX, reason="openai_codex extra not installed")
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", CODEX_SCENARIOS, ids=lambda s: s.name)
async def test_codex_golden(scenario, tmp_path):
    raw = await run_codex_scenario(scenario, str(tmp_path))
    assert_reconciliation(raw)
    assert_timing_captured(
        raw,
        expect_generation_window=_expect_window("codex", scenario.name),
        check_identity=_check_identity("codex", scenario.name),
    )
    _compare_or_regen(f"codex_{scenario.name}", scrub(raw))


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", CLAUDE_SCENARIOS, ids=lambda s: s.name)
async def test_claude_reconciliation_invariant(scenario, tmp_path):
    """The per-bucket reconciliation invariant holds for every Claude snapshot."""
    raw = await run_claude_scenario(scenario, str(tmp_path))
    assert_reconciliation(raw)


@pytest.mark.skipif(not _HAS_CODEX, reason="openai_codex extra not installed")
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", CODEX_SCENARIOS, ids=lambda s: s.name)
async def test_codex_reconciliation_invariant(scenario, tmp_path):
    """The per-bucket reconciliation invariant holds for every Codex snapshot."""
    raw = await run_codex_scenario(scenario, str(tmp_path))
    assert_reconciliation(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ANTIGRAVITY_SCENARIOS, ids=lambda s: s.name)
async def test_antigravity_golden(scenario, tmp_path):
    raw = await run_antigravity_scenario(scenario, str(tmp_path))
    assert_reconciliation(raw)
    assert_timing_captured(
        raw,
        expect_generation_window=_expect_window("antigravity", scenario.name),
        check_identity=_check_identity("antigravity", scenario.name),
    )
    _compare_or_regen(f"antigravity_{scenario.name}", scrub(raw))


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ANTIGRAVITY_SCENARIOS, ids=lambda s: s.name)
async def test_antigravity_reconciliation_invariant(scenario, tmp_path):
    """The per-bucket reconciliation invariant holds for every Antigravity snapshot."""
    assert_reconciliation(await run_antigravity_scenario(scenario, str(tmp_path)))


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", OPENCODE_SCENARIOS, ids=lambda s: s.name)
async def test_opencode_golden(scenario, tmp_path):
    raw = await run_opencode_scenario(scenario, str(tmp_path))
    assert_reconciliation(raw)
    assert_timing_captured(
        raw,
        expect_generation_window=_expect_window("opencode", scenario.name),
        check_identity=_check_identity("opencode", scenario.name),
    )
    _compare_or_regen(f"opencode_{scenario.name}", scrub(raw))


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", OPENCODE_SCENARIOS, ids=lambda s: s.name)
async def test_opencode_reconciliation_invariant(scenario, tmp_path):
    """The per-bucket reconciliation invariant holds for every OpenCode snapshot."""
    assert_reconciliation(await run_opencode_scenario(scenario, str(tmp_path)))


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", PI_SCENARIOS, ids=lambda s: s.name)
async def test_pi_golden(scenario, tmp_path):
    raw = await run_pi_scenario(scenario, str(tmp_path))
    assert_reconciliation(raw)
    assert_timing_captured(
        raw,
        expect_generation_window=_expect_window("pi", scenario.name),
        check_identity=_check_identity("pi", scenario.name),
    )
    _compare_or_regen(f"pi_{scenario.name}", scrub(raw))


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", PI_SCENARIOS, ids=lambda s: s.name)
async def test_pi_reconciliation_invariant(scenario, tmp_path):
    """The per-bucket reconciliation invariant holds for every Pi snapshot."""
    assert_reconciliation(await run_pi_scenario(scenario, str(tmp_path)))


# The ONE place a harness is listed for golden coverage. Derived from AgentKind
# rather than from register_builtins, whose built-in list is a hardcoded tuple
# inside the function body that returns nothing and exposes no set.
SCENARIOS_BY_AGENT: dict[AgentKind, list[Any]] = {
    AgentKind.CLAUDE_CODE: CLAUDE_SCENARIOS,
    AgentKind.CODEX: CODEX_SCENARIOS,
    AgentKind.ANTIGRAVITY: ANTIGRAVITY_SCENARIOS,
    AgentKind.OPENCODE: OPENCODE_SCENARIOS,
    AgentKind.PI: PI_SCENARIOS,
}

# An ALLOWLIST of exclusions, not a denylist of inclusions: a new AgentKind
# member fails the coverage test until someone decides which it is.
_NO_GOLDEN_COVERAGE: dict[AgentKind, str] = {
    # The agentless backend (NoOpAgent): it runs no model and streams nothing,
    # so there is no event stream to record.
    AgentKind.NONE: "agentless backend — runs no model, streams nothing",
    # A sentinel for "agent type could not be determined". Never registered.
    AgentKind.UNKNOWN: "sentinel for an undeterminable type — never registered",
}


def unaccounted_harnesses(mapping: dict[AgentKind, list[Any]]) -> set[AgentKind]:
    """AgentKind members that are neither covered by `mapping` nor excluded."""
    return set(AgentKind) - (set(mapping) | set(_NO_GOLDEN_COVERAGE))


def harnesses_without_scenarios(mapping: dict[AgentKind, list[Any]]) -> set[AgentKind]:
    """Covered harnesses whose scenario list is EMPTY.

    An empty list must fail: a coverage check guarding zero streams is not a
    coverage check (mirrors runner.py's "a lint rule guarding zero files must
    fail, not pass").
    """
    return {kind for kind, scenarios in mapping.items() if not scenarios}


class TestGoldenCoverage:
    """Every built-in harness has at least one recorded stream.

    A third-party plugin agent (e.g. coder_eval_uipath's ``delegate-sdk``, when
    that package happens to be installed in the dev env) is deliberately out of
    scope — which is why this keys on ``AgentKind`` and not on the live
    registry.

    The two checks are extracted as module-level functions so the negative
    cases below can run the REAL check against a mutated copy, rather than
    restating the condition (which passes whatever the check does).
    """

    def test_every_builtin_agent_kind_is_accounted_for(self):
        missing = unaccounted_harnesses(SCENARIOS_BY_AGENT)
        assert not missing, (
            f"AgentKind member(s) {sorted(k.value for k in missing)} have no golden scenarios and are "
            "not excluded. Add scenarios, or add an entry to _NO_GOLDEN_COVERAGE with the reason."
        )
        assert not (set(SCENARIOS_BY_AGENT) & set(_NO_GOLDEN_COVERAGE)), "a harness cannot be both covered and excluded"

    def test_every_covered_harness_has_scenarios(self):
        empty = harnesses_without_scenarios(SCENARIOS_BY_AGENT)
        if not _HAS_CODEX:
            # The optional extra is absent, so CODEX_SCENARIOS is [] by
            # construction — not by anyone forgetting to record a stream.
            empty -= {AgentKind.CODEX}
        assert not empty, f"listed as covered but has NO scenarios: {sorted(k.value for k in empty)}"

    def test_the_check_catches_an_unaccounted_member(self):
        # Mutate a COPY and run the REAL check against it.
        shrunk = {k: v for k, v in SCENARIOS_BY_AGENT.items() if k is not AgentKind.PI}
        assert unaccounted_harnesses(shrunk) == {AgentKind.PI}

    def test_the_check_catches_an_empty_scenario_list(self):
        emptied = dict(SCENARIOS_BY_AGENT) | {AgentKind.PI: []}
        assert AgentKind.PI in harnesses_without_scenarios(emptied)


class TestAssertTimingCaptured:
    """The sensor itself. An AST rule cannot see that an SDK returned 0.0."""

    @staticmethod
    def _record(
        *,
        windows: list[float | None] = (),
        commands: list[dict[str, Any]] = (),
        bounds_collapse: bool = False,
        overhead: tuple[float | None, float | None] = (0.0, 3.5),
        duration_seconds: float = 10.0,
    ) -> dict[str, Any]:
        """A record whose bounds span each window, unless `bounds_collapse`.

        `overhead` is the (head, tail) pair. It defaults to a MEASURED pair —
        a 0.0 head is antigravity's real answer — because every record here
        carries an assistant message unless a test says otherwise, and the
        sensor requires both buckets on such a turn.

        `duration_seconds` defaults to a turn long enough that the four-bucket
        identity is trivially satisfied, so these cases constrain only what
        each is about; the identity has its own cases below.
        """
        # MODEL-VALID, not merely shaped like a record. `assert_timing_captured`
        # validates the dump into a `TurnRecord` so it can call production's own
        # span selector instead of re-deriving one, and a fixture missing the
        # required fields would fail there rather than on the thing it is about.
        return {
            "iteration": 1,
            "user_input": "",
            "agent_output": "",
            "duration_seconds": duration_seconds,
            "messages": [
                {
                    "role": "assistant",
                    "generation_duration_ms": w,
                    "started_at": "2026-01-01T00:00:00",
                    "completed_at": "2026-01-01T00:00:00" if bounds_collapse else "2026-01-01T00:00:01",
                }
                for w in windows
            ],
            "commands": [{"tool_name": "Bash", "timestamp": "2026-01-01T00:00:00", **command} for command in commands],
            "harness_startup_ms": overhead[0],
            "harness_teardown_ms": overhead[1],
        }

    def test_a_positive_window_passes(self):
        assert_timing_captured(self._record(windows=[12.5]), expect_generation_window=True)

    def test_a_none_window_raises_when_one_is_expected(self):
        # overhead=(None, None) because a turn with no measurable window has no
        # head or tail either; this isolates the generation-window assertion.
        with pytest.raises(AssertionError, match="positive generation window"):
            assert_timing_captured(self._record(windows=[None], overhead=(None, None)), expect_generation_window=True)

    def test_exactly_zero_raises_too(self):
        # The Antigravity defect's exact signature: a value that is present,
        # numeric, and means nothing was measured.
        with pytest.raises(AssertionError, match="positive generation window"):
            assert_timing_captured(self._record(windows=[0.0]), expect_generation_window=True)

    def test_collapsed_bounds_raise_even_with_a_healthy_duration(self):
        # Two harnesses take the duration from a MONOTONIC clock and the
        # bounds from the wall clock, so a reducer can report a real duration
        # beside two stamps that collapsed to one instant. CE059 sees that
        # statically only when both bounds are the same ast.Name; this is the
        # check for when they are two different names holding one value.
        with pytest.raises(AssertionError, match="bounds that span it"):
            assert_timing_captured(self._record(windows=[500.0], bounds_collapse=True), expect_generation_window=True)

    def test_a_none_window_passes_when_none_is_expected(self):
        assert_timing_captured(self._record(windows=[None], overhead=(None, None)), expect_generation_window=False)

    def test_one_positive_among_several_passes(self):
        # The FLOOR, not a per-entry rule. claude_d_subagent_terminal holds two
        # content-bearing messages of which exactly one is legitimately None,
        # and no scenario-level flag could express "this one but not that one".
        assert_timing_captured(self._record(windows=[None, 8.0]), expect_generation_window=True)

    def test_a_resolved_command_missing_a_bound_raises(self):
        record = self._record(
            windows=[5.0],
            commands=[
                {
                    "tool_id": "t1",
                    "result_status": "success",
                    "duration_ms": 10.0,
                    "execution_started_at": "2026-01-01T00:00:00",
                    "execution_completed_at": None,
                }
            ],
        )
        with pytest.raises(AssertionError, match="execution_completed_at"):
            assert_timing_captured(record, expect_generation_window=True)

    def test_a_resolved_command_missing_its_duration_raises(self):
        record = self._record(
            windows=[5.0],
            commands=[
                {
                    "tool_id": "t1",
                    "result_status": "error",
                    "duration_ms": None,
                    "execution_started_at": "2026-01-01T00:00:00",
                    "execution_completed_at": "2026-01-01T00:00:01",
                }
            ],
        )
        with pytest.raises(AssertionError, match="duration_ms"):
            assert_timing_captured(record, expect_generation_window=True)

    def test_an_unresolved_command_is_exempt(self):
        # Force-closed without a result: never timed, and saying so is the
        # honest record.
        record = self._record(
            windows=[5.0],
            commands=[
                {
                    "tool_id": "orphan",
                    "result_status": "unknown",
                    "duration_ms": None,
                    "execution_started_at": None,
                    "execution_completed_at": None,
                }
            ],
        )
        assert_timing_captured(record, expect_generation_window=True)

    def test_a_scenario_with_no_commands_is_vacuously_fine(self):
        assert_timing_captured(self._record(windows=[5.0]), expect_generation_window=True)

    # The turn's head and tail. Presence only — the replays run in ~0.3 ms of
    # synthetic wall clock, so any bound check here would be noise.
    def test_a_generating_turn_must_report_a_head(self):
        with pytest.raises(AssertionError, match="harness_startup_ms is None"):
            assert_timing_captured(self._record(windows=[5.0], overhead=(None, 3.5)), expect_generation_window=True)

    def test_a_generating_turn_must_report_a_tail(self):
        with pytest.raises(AssertionError, match="harness_teardown_ms is None"):
            assert_timing_captured(self._record(windows=[5.0], overhead=(0.0, None)), expect_generation_window=True)

    def test_a_turn_with_no_generation_must_report_neither(self):
        # A number here claims a measurement nobody could have taken: the
        # collector measures both against the messages that report a window.
        with pytest.raises(AssertionError, match=r"harness_startup_ms is 0\.0"):
            assert_timing_captured(self._record(windows=[], overhead=(0.0, 3.5)), expect_generation_window=False)
        assert_timing_captured(self._record(windows=[], overhead=(None, None)), expect_generation_window=False)

    def test_an_unmeasurable_window_is_not_something_to_measure_against(self):
        # codex_g_items_rebuild's shape: an assistant message exists, but it was
        # rebuilt after the turn ended with placeholder now() bounds and says so
        # via generation_duration_ms=None. Those stamps are not window bounds, so
        # the honest head and tail are None — keying on "any assistant message"
        # would have demanded a number derived from a placeholder.
        with pytest.raises(AssertionError, match=r"harness_startup_ms is 0\.0"):
            assert_timing_captured(self._record(windows=[None], overhead=(0.0, 3.5)), expect_generation_window=False)

    # The four-bucket identity: generation + tool union + head + tail cannot
    # exceed the turn, because the four are disjoint.
    def test_buckets_summing_past_the_turn_raise(self):
        # 4s generation + a 3.5ms tail on a 1s turn.
        with pytest.raises(AssertionError, match="booked twice"):
            assert_timing_captured(self._record(windows=[4000.0], duration_seconds=1.0), expect_generation_window=True)

    def test_a_tool_double_booked_into_the_tail_is_caught(self):
        """The exact defect: an orphan force-closed inside the tail, counted
        both in the tool union and in harness_teardown_ms."""
        record = self._record(
            windows=[40.0],
            duration_seconds=0.1,  # 100 ms turn
            overhead=(0.0, 50.0),
            commands=[
                {
                    "tool_id": "orphan",
                    "result_status": "success",
                    "duration_ms": 50.0,
                    "execution_started_at": "2026-01-01T00:00:00.020000",
                    "execution_completed_at": "2026-01-01T00:00:00.070000",
                }
            ],
        )
        with pytest.raises(AssertionError, match="booked twice"):
            assert_timing_captured(record, expect_generation_window=True)

    def test_the_identity_can_be_waived_for_a_fictional_clock(self):
        # codex/opencode scenarios declare integer-millisecond item durations
        # that a sub-millisecond replay can never contain.
        assert_timing_captured(
            self._record(windows=[4000.0], duration_seconds=1.0),
            expect_generation_window=True,
            check_identity=False,
        )

    def test_buckets_well_inside_the_turn_pass(self):
        assert_timing_captured(self._record(windows=[40.0], duration_seconds=1.0), expect_generation_window=True)

    def test_the_buckets_are_checked_even_when_no_window_is_expected(self):
        # codex_e_orphan_tool clears the flag (its window subtracts to zero)
        # while still having a head and a tail — so the flag is the wrong key
        # for this half of the sensor, and the early return must not skip it.
        with pytest.raises(AssertionError, match="harness_teardown_ms is None"):
            assert_timing_captured(self._record(windows=[5.0], overhead=(0.0, None)), expect_generation_window=False)
