"""Unit tests for `coder_eval.optimize.fronts` — the three fronts and the headroom ceilings.

All three fronts treat a hole as ABSENT rather than zero, require row coverage before one arm may
dominate another, and guard non-finite values; a parametrized test asserts they agree.
"""

import json
import math
from pathlib import Path

import pytest

from coder_eval.models import ArmRowScores
from coder_eval.optimize.fronts import (
    CostQualityPoint,
    RuleCeiling,
    arm_row_scores,
    cost_quality_front,
    cost_quality_points,
    headroom_ceiling,
    instance_best_front,
    pareto_front,
)
from coder_eval.reports_optimize import COST_FRONT_ADVISORY, render_cost_quality, render_row_matrix
from tests.optimize_fixtures import (
    HEADROOM_FLOOR,
    HEADROOM_ROW_SCORES,
    HEADROOM_RULE_ROWS,
    SUITE,
    cost_quality_arm,
    costed_result,
    eval_result,
    scored_result,
    write_row,
)


def _write_scored_arm(tmp_path: Path, variant: str, per_row: dict[str, list[float]]) -> list[Path]:
    """One arm whose row scores differ per run dir, so the replicate reduction is exercised."""
    invocations = max(len(v) for v in per_row.values())
    run_dirs = []
    for i in range(invocations):
        run_dir = tmp_path / f"run-{i}"
        for row_id, scores in per_row.items():
            if i < len(scores):
                write_row(run_dir, variant, row_id, scored_result(row_id, scores[i]))
        run_dirs.append(run_dir)
    return run_dirs


class TestArmRowScores:
    def test_reads_every_row_and_variant(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run-0"
        for variant, score in (("incumbent", 0.4), ("cand-a", 0.9)):
            for row in ("r1", "r2"):
                write_row(run_dir, variant, row, scored_result(row, score))

        arms = arm_row_scores(run_dirs=[run_dir], variant_ids=["incumbent", "cand-a"], suite_id=SUITE)
        assert [a.variant_id for a in arms] == ["incumbent", "cand-a"]
        assert arms[0].row_scores == {"r1": 0.4, "r2": 0.4}
        assert arms[1].row_scores == {"r1": 0.9, "r2": 0.9}

    def test_averages_replicates_across_run_dirs(self, tmp_path: Path) -> None:
        # A row scoring 0.0 and 1.0 in two run dirs reduces to 0.5 — not to whichever dir was
        # read first, which is what a single-run-dir signature would have given.
        run_dirs = _write_scored_arm(tmp_path, "incumbent", {"r1": [0.0, 1.0]})
        arms = arm_row_scores(run_dirs=run_dirs, variant_ids=["incumbent"], suite_id=SUITE)
        assert arms[0].row_scores == {"r1": 0.5}

    def test_reads_a_criterion_score_when_given_an_index(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run-0"
        write_row(run_dir, "incumbent", "r1", eval_result("r1", [("yes", "no"), ("yes", "yes")]))
        by_criterion = arm_row_scores(run_dirs=[run_dir], variant_ids=["incumbent"], suite_id=SUITE, criterion_index=1)
        assert by_criterion[0].row_scores == {"r1": 1.0}

    def test_a_row_without_a_score_is_absent_not_zero(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run-0"
        write_row(run_dir, "incumbent", "r1", scored_result("r1", 0.8))
        write_row(run_dir, "incumbent", "r2", eval_result("r2", [("yes", "yes")]))  # weighted_score is None
        arms = arm_row_scores(run_dirs=[run_dir], variant_ids=["incumbent"], suite_id=SUITE)
        assert arms[0].row_scores == {"r1": 0.8}


class TestParetoFront:
    def _arm(self, name: str, **rows: float) -> ArmRowScores:
        return ArmRowScores(variant_id=name, row_scores=rows)

    def test_excludes_a_dominated_arm(self) -> None:
        better = self._arm("cand-a", r1=1.0, r2=1.0)
        worse = self._arm("cand-b", r1=0.5, r2=1.0)
        assert pareto_front([better, worse]) == ["cand-a"]

    def test_keeps_complementary_arms(self) -> None:
        # Each wins a disjoint row set — the merge opportunity a suite average hides.
        a = self._arm("cand-a", r1=1.0, r2=0.0)
        b = self._arm("cand-b", r1=0.0, r2=1.0)
        assert pareto_front([a, b]) == ["cand-a", "cand-b"]

    def test_identical_arms_all_stay_on_the_front(self) -> None:
        arms = [self._arm(f"cand-{n}", r1=0.7, r2=0.7) for n in "abc"]
        assert pareto_front(arms) == ["cand-a", "cand-b", "cand-c"]

    def test_a_single_arm_is_its_own_front(self) -> None:
        assert pareto_front([self._arm("only", r1=0.1)]) == ["only"]

    def test_empty_input(self) -> None:
        assert pareto_front([]) == []

    def test_a_hole_never_fabricates_domination(self) -> None:
        # `full` beats `holed` on r1 and never scored r2 at all. It is NOT entitled to dominate:
        # counting its missing cell as 0.0 would fabricate a loss for `holed` on a row it won, and
        # ignoring the row entirely would let an arm dominate on the subset it happens to share.
        # Domination requires COVERAGE of everything the other arm scored.
        full = ArmRowScores(variant_id="full", row_scores={"r1": 1.0})
        holed = ArmRowScores(variant_id="holed", row_scores={"r1": 0.5, "r2": 1.0})
        assert pareto_front([full, holed]) == ["full", "holed"]

    def test_an_arm_that_covers_everything_still_dominates(self) -> None:
        # The other direction: coverage is a precondition, not a way to survive by scoring MORE.
        covered = ArmRowScores(variant_id="covered", row_scores={"r1": 1.0, "r2": 1.0})
        partial = ArmRowScores(variant_id="partial", row_scores={"r1": 0.5})
        assert pareto_front([covered, partial]) == ["covered"]

    def test_a_nan_cell_does_not_take_the_front_by_incomparability(self) -> None:
        # Every `>=` against NaN is False, so an unguarded NaN arm is undominatable AND dominates
        # nobody — it lands on the front in bold beside arms that earned it. Treated as a hole, it
        # goes through the coverage rule instead: `poisoned` is then a one-row arm that `winner`
        # covers and beats.
        winner = ArmRowScores(variant_id="winner", row_scores={"r1": 1.0, "r2": 1.0})
        poisoned = ArmRowScores(variant_id="poisoned", row_scores={"r1": 0.5, "r2": float("nan")})
        assert pareto_front([winner, poisoned]) == ["winner"]

    def test_an_arm_whose_every_cell_is_non_finite_is_excluded(self) -> None:
        # Same rule as an arm that scored no rows: nothing about an empty vector is a win.
        real = ArmRowScores(variant_id="real", row_scores={"r1": 0.5})
        broken = ArmRowScores(variant_id="broken", row_scores={"r1": float("nan"), "r2": float("inf")})
        assert pareto_front([real, broken]) == ["real"]


class TestEveryFrontGuardsNonFiniteScores:
    """The claim `cost_quality_front`'s docstring and CLAUDE.md both make, asserted rather than read.

    The three fronts answer different questions and guard by different mechanisms — a hole on the
    coverage front, a skipped maximum on GEPA's, an excluded arm on the cost one. What has to agree
    is the OUTCOME: a non-finite cell never wins anything and never makes its arm undominatable.
    """

    _CLEAN = ArmRowScores(variant_id="clean", row_scores={"r1": 1.0, "r2": 1.0})
    _POISONED = ArmRowScores(variant_id="poisoned", row_scores={"r1": 0.5, "r2": float("nan")})

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"])
    def test_no_front_carries_the_non_finite_arm(self, bad: float) -> None:
        poisoned = ArmRowScores(variant_id="poisoned", row_scores={"r1": 0.5, "r2": bad})
        arms = [self._CLEAN, poisoned]
        assert pareto_front(arms) == ["clean"]
        assert instance_best_front(arms) == ["clean"]
        points = [
            CostQualityPoint("clean", 1.0, 0.5, frozenset({"r1", "r2"})),
            CostQualityPoint("poisoned", bad, 0.1, frozenset({"r1", "r2"})),
        ]
        assert cost_quality_front(points) == ["clean"]

    def test_the_finite_rows_of_a_mixed_arm_still_participate(self) -> None:
        # Not a stricter rule than `instance_best_front`'s: a NaN cell is dropped, the arm is not.
        # `poisoned` still owns r1, so both row-vector fronts keep it.
        poisoned = ArmRowScores(variant_id="poisoned", row_scores={"r1": 1.0, "r2": float("nan")})
        other = ArmRowScores(variant_id="other", row_scores={"r1": 0.5, "r2": 0.9})
        assert pareto_front([poisoned, other]) == ["poisoned", "other"]
        assert instance_best_front([poisoned, other]) == ["poisoned", "other"]


class TestAnArmThatScoredNothing:
    def test_is_not_on_the_front(self) -> None:
        # Nothing can COVER an empty vector, so the domination rule alone would make a candidate
        # that crashed on every row undominatable — and render it bold beside a real winner.
        real = ArmRowScores(variant_id="real", row_scores={"r1": 0.9})
        crashed = ArmRowScores(variant_id="crashed", row_scores={})
        assert pareto_front([real, crashed]) == ["real"]

    def test_is_named_in_the_matrix_as_a_wiring_problem(self) -> None:
        arms = [
            ArmRowScores(variant_id="real", row_scores={"r1": 0.9}),
            ArmRowScores(variant_id="crashed", row_scores={}),
        ]
        text = render_row_matrix(arms, pareto_front(arms))
        assert "scored no rows at all" in text
        assert "not a result" in text
        assert "**crashed**" not in text

    def test_all_arms_empty_yields_an_empty_front(self) -> None:
        arms = [ArmRowScores(variant_id=n, row_scores={}) for n in ("a", "b")]
        assert pareto_front(arms) == []


class TestAnErroredRowIsAHoleNotAZero:
    def test_on_the_execution_track_too(self, tmp_path: Path) -> None:
        """`weighted_score` is 0.0 for an empty result list, so an errored row looked scored.

        Measured before the fix: arm B, identical to A except that it ERRORED on r1, was dropped
        from the Pareto front — discarded for crashing, with no `—` in the matrix to show why.
        """
        run_dir = tmp_path / "run-0"
        write_row(run_dir, "a", "r0", scored_result("r0", 0.5))
        write_row(run_dir, "a", "r1", scored_result("r1", 0.5))
        write_row(run_dir, "b", "r0", scored_result("r0", 0.5))
        errored = eval_result("r1", []).model_copy(update={"weighted_score": 0.0})
        write_row(run_dir, "b", "r1", errored)

        arms = arm_row_scores(run_dirs=[run_dir], variant_ids=["a", "b"], suite_id=SUITE)
        assert arms[1].row_scores == {"r0": 0.5}, "the errored row must be ABSENT, not 0.0"
        assert pareto_front(arms) == ["a", "b"]
        assert "| r1 | 0.500 | — |" in render_row_matrix(arms, pareto_front(arms))


class TestInstanceBestFront:
    """GEPA's frontier, beside ours. Neither set contains the other, and that is the point."""

    def _arm(self, name: str, **rows: float) -> ArmRowScores:
        return ArmRowScores(variant_id=name, row_scores=rows)

    def _four_arms(self) -> list[ArmRowScores]:
        # The fixture from the docstring: A is dominated by nobody yet wins nothing; D ties a row's
        # maximum yet is dominated outright by B.
        return [
            self._arm("A", r1=0.5, r2=0.5),
            self._arm("B", r1=1.0, r2=0.4),
            self._arm("C", r1=0.4, r2=1.0),
            self._arm("D", r1=1.0, r2=0.3),
        ]

    def test_the_two_fronts_diverge_in_both_directions(self) -> None:
        arms = self._four_arms()
        assert pareto_front(arms) == ["A", "B", "C"]
        assert instance_best_front(arms) == ["B", "C", "D"]

    def test_keeps_a_single_row_winner(self) -> None:
        # The whole reason a merge reads this set rather than the coverage one.
        arms = [
            self._arm("broad", r1=0.9, r2=0.9, r3=0.9),
            self._arm("narrow", r1=0.1, r2=0.1, r3=1.0),
        ]
        assert pareto_front(arms) == ["broad", "narrow"]
        assert instance_best_front(arms) == ["broad", "narrow"]
        # And when the narrow arm IS dominated, coverage drops it while instance-best keeps it.
        arms = [
            self._arm("broad", r1=0.9, r2=0.9, r3=1.0),
            self._arm("narrow", r1=0.1, r2=0.1, r3=1.0),
        ]
        assert pareto_front(arms) == ["broad"]
        assert instance_best_front(arms) == ["broad", "narrow"]

    def test_excludes_an_arm_that_scored_nothing(self) -> None:
        real = self._arm("real", r1=0.9)
        crashed = ArmRowScores(variant_id="crashed", row_scores={})
        assert instance_best_front([real, crashed]) == ["real"]

    def test_counts_a_row_only_one_arm_scored(self) -> None:
        # A hole is not a zero, so the max is over the arms that MEASURED the row — an arm alone on
        # a row is trivially best on it. That is the "wins a row nobody else measured" case a merge
        # wants to see.
        alone = self._arm("alone", r1=0.1, r2=0.2)
        other = self._arm("other", r1=0.9)
        assert instance_best_front([alone, other]) == ["alone", "other"]

    def test_keeps_every_tied_arm(self) -> None:
        arms = [self._arm(f"cand-{n}", r1=0.7, r2=0.7) for n in "abc"]
        assert instance_best_front(arms) == ["cand-a", "cand-b", "cand-c"]
        assert pareto_front(arms) == instance_best_front(arms)

    def test_empty_input(self) -> None:
        assert instance_best_front([]) == []

    def test_render_row_matrix_names_the_disagreement(self) -> None:
        arms = self._four_arms()
        text = render_row_matrix(arms, pareto_front(arms), instance_best=instance_best_front(arms))
        assert "Instance-best front (GEPA's, the merge shortlist): B, C, D" in text
        assert "Pareto front (**bold**): A, B, C" in text
        # The diff is the finding — two bare lists teach a reader nothing.
        assert "on coverage without winning any row: A" in text
        assert "wins a row despite being dominated overall: D" in text
        assert "DISCARD" in text and "MERGE" in text

    def test_render_row_matrix_says_so_when_the_fronts_agree(self) -> None:
        arms = [self._arm("a", r1=1.0, r2=0.0), self._arm("b", r1=0.0, r2=1.0)]
        text = render_row_matrix(arms, pareto_front(arms), instance_best=instance_best_front(arms))
        assert "Both fronts agree on these arms." in text

    def test_a_non_finite_score_does_not_poison_the_rows_maximum(self) -> None:
        """A NaN must not drop the arm that genuinely won the row.

        `value > nan` is False, so an unguarded max latches NaN from whichever arm set it first —
        and then `v == best[r]` is False for EVERY arm, so the real winner falls off the merge
        shortlist silently. `pareto_front` degrades the other way (NaN comparisons are False in
        both directions, so nothing dominates and everything stays), which would leave the two
        fronts disagreeing for a reason the render then narrates as a finding.
        """
        nan = float("nan")
        arms = [
            self._arm("winner", r1=1.0, r2=0.5),
            ArmRowScores(variant_id="broken", row_scores={"r1": nan, "r2": 0.1}),
        ]
        assert instance_best_front(arms) == ["winner"]

    def test_render_says_nothing_about_agreement_when_no_arm_scored(self) -> None:
        # Both fronts empty is every arm having crashed. "Both fronts agree" would read as a
        # result, directly above the line calling it a wiring problem.
        arms = [ArmRowScores(variant_id=n, row_scores={}) for n in ("a", "b")]
        text = render_row_matrix(arms, pareto_front(arms), instance_best=instance_best_front(arms))
        assert "Both fronts agree" not in text
        assert "scored no rows at all" in text

    def test_render_row_matrix_without_instance_best_is_unchanged(self) -> None:
        # Byte-identical to today's output when the new keyword is omitted — the sole existing call
        # site in SKILL.md passes two positional arguments and must keep working.
        arms = self._four_arms()
        assert render_row_matrix(arms, pareto_front(arms)) == render_row_matrix(
            arms, pareto_front(arms), instance_best=None
        )
        assert "Instance-best" not in render_row_matrix(arms, pareto_front(arms))


class TestCostQualityFront:
    """Cost as a second AXIS of the shortlist — never a second gate."""

    def _points(self, tmp_path: Path, arms: dict[str, dict[str, tuple[float, float | None]]]):
        for variant, per_row in arms.items():
            cost_quality_arm(tmp_path, variant, per_row)
        return cost_quality_points(
            run_dirs=[tmp_path / "run-0"], variant_ids=list(arms), suite_id=SUITE, criterion_index=None
        )

    def test_a_non_finite_cost_cannot_reach_this_front_from_disk(self, tmp_path: Path) -> None:
        """Why the coordinate invariant holds, pinned on the mechanism rather than assumed.

        `row_cost_levels` drops a row whose cost is not finite, which would put the cost coordinate
        on FEWER rows than the score coordinate while `row_ids` still claimed the full set — an arm
        dominating on a cost it under-measured. It cannot happen HERE, and the reason is worth a test
        rather than a comment: pydantic accepts a non-finite `total_cost_usd` in memory but
        serialises it as `null`, and this front reads every row from `task.json`. So the row arrives
        with no cost at all, which is a state the front has always handled.

        The guard in `cost_quality_points` is therefore defence-in-depth for a caller-supplied row,
        not a live path — and if this test ever fails because the serialiser starts round-tripping
        `Infinity`, the guard is what stops the invariant breaking silently.
        """
        points = self._points(
            tmp_path,
            {
                "incumbent": {f"r{i}": (0.90, 1.00) for i in range(6)},
                "cand-corrupt": {**{f"r{i}": (0.90, 0.10) for i in range(5)}, "r5": (0.90, float("inf"))},
            },
        )
        corrupt = next(p for p in points if p.variant_id == "cand-corrupt")
        assert corrupt.score == 0.90, "the SCORE coordinate is unaffected — those rows scored fine"
        assert len(corrupt.row_ids) == 6, "coverage is about rows SCORED, and all six were"
        # The row recorded NO cost, so it is absent from the cost median exactly as an unmeasured row
        # is — and the five clean rows still state this arm's cost.
        assert corrupt.cost_per_row == 0.10
        row = next((tmp_path / "run-0" / "cand-corrupt" / SUITE / "r5").rglob("task.json"))
        recorded = json.loads(row.read_text(encoding="utf-8"))["total_token_usage"]["total_cost_usd"]
        assert recorded is None, "a non-finite cost reaching disk would break the coordinate invariant"

    def test_keeps_a_cheaper_slightly_worse_arm(self, tmp_path: Path) -> None:
        # The headline case: 2% worse and 40% cheaper is a trade worth showing the user.
        points = self._points(
            tmp_path,
            {
                "incumbent": {f"r{i}": (0.90, 1.00) for i in range(6)},
                "cand-cheap": {f"r{i}": (0.88, 0.60) for i in range(6)},
            },
        )
        assert cost_quality_front(points) == ["incumbent", "cand-cheap"]

    def test_drops_a_dearer_and_worse_arm(self, tmp_path: Path) -> None:
        points = self._points(
            tmp_path,
            {
                "incumbent": {f"r{i}": (0.90, 1.00) for i in range(6)},
                "cand-bad": {f"r{i}": (0.70, 1.50) for i in range(6)},
            },
        )
        assert cost_quality_front(points) == ["incumbent"]

    def test_a_free_arm_is_on_the_front_not_excluded(self, tmp_path: Path) -> None:
        # A zero cost is a real coordinate — a free model is legitimately the cheapest arm there
        # is. A truthiness test would exclude it, which is the register_pricing rule restated.
        points = self._points(
            tmp_path,
            {
                "paid": {f"r{i}": (0.90, 1.00) for i in range(6)},
                "free": {f"r{i}": (0.40, 0.0) for i in range(6)},
            },
        )
        assert next(p for p in points if p.variant_id == "free").cost_per_row == 0.0
        assert cost_quality_front(points) == ["paid", "free"]

    def test_an_arm_with_no_recorded_cost_is_excluded_and_named(self, tmp_path: Path) -> None:
        points = self._points(
            tmp_path,
            {
                "measured": {f"r{i}": (0.90, 1.00) for i in range(6)},
                "costless": {f"r{i}": (0.95, None) for i in range(6)},
            },
        )
        assert next(p for p in points if p.variant_id == "costless").cost_per_row is None
        front = cost_quality_front(points)
        assert front == ["measured"]
        text = render_cost_quality(points, front)
        assert "costless" in text
        assert "An unmeasured cost is not a free one" in text

    def test_identical_costs_degenerate_to_the_quality_maxima(self, tmp_path: Path) -> None:
        points = self._points(
            tmp_path,
            {
                "best": {f"r{i}": (0.90, 1.00) for i in range(6)},
                "mid": {f"r{i}": (0.70, 1.00) for i in range(6)},
                "worst": {f"r{i}": (0.50, 1.00) for i in range(6)},
            },
        )
        assert cost_quality_front(points) == ["best"]

    def test_identical_quality_degenerates_to_the_cheapest(self, tmp_path: Path) -> None:
        points = self._points(
            tmp_path,
            {
                "dear": {f"r{i}": (0.80, 2.00) for i in range(6)},
                "cheap": {f"r{i}": (0.80, 0.50) for i in range(6)},
            },
        )
        assert cost_quality_front(points) == ["cheap"]

    def test_a_single_arm_is_its_own_front(self, tmp_path: Path) -> None:
        points = self._points(tmp_path, {"only": {f"r{i}": (0.5, 1.0) for i in range(4)}})
        assert cost_quality_front(points) == ["only"]

    def test_empty_input(self) -> None:
        assert cost_quality_front([]) == []
        assert "No arms" in render_cost_quality([], [])

    def test_a_crashed_arm_cannot_take_the_front_on_the_rows_it_skipped(self, tmp_path: Path) -> None:
        """Both coordinates must be averaged over the SAME rows — the ones the arm actually scored.

        A crashed row produces no criterion results, so `row_score` returns None and quality
        excludes it — but the row still burned tokens, so an unrestricted cost median includes it.
        Measured before the fix: an arm completing 1 of 6 rows at a perfect score took the whole
        front and knocked the incumbent off it, rendered as two clean numbers with nothing showing
        the other five rows were missing. This is the failure `_dominates`'s coverage rule and
        `render_row_matrix`'s `—` cells exist to prevent one screen earlier.
        """
        run_dir = tmp_path / "run-0"
        for row in range(6):
            good = costed_result(f"r{row}", [("yes", "yes")], cost=1.0, duration=10.0)
            write_row(run_dir, "incumbent", f"r{row}", good.model_copy(update={"weighted_score": 0.9}))
            # The candidate finished r0 and crashed the rest: no criterion results, cost still recorded.
            if row == 0:
                write_row(run_dir, "crashy", f"r{row}", good.model_copy(update={"weighted_score": 1.0}))
            else:
                crashed = costed_result(f"r{row}", [], cost=1.0, duration=10.0)
                write_row(run_dir, "crashy", f"r{row}", crashed.model_copy(update={"weighted_score": 0.0}))

        points = cost_quality_points(
            run_dirs=[run_dir], variant_ids=["incumbent", "crashy"], suite_id=SUITE, criterion_index=None
        )
        by_id = {p.variant_id: p for p in points}
        assert (by_id["incumbent"].n_rows, by_id["crashy"].n_rows) == (6, 1)
        # The incumbent must stay on the front: an arm measured on one row is not entitled to a
        # claim about "everywhere", so it cannot displace one measured on six. Both stay — the
        # crashed arm is shown with its row count rather than silently dropped or silently believed.
        assert cost_quality_front(points) == ["incumbent", "crashy"]
        # And the render must SAY the arm is standing on less evidence.
        text = render_cost_quality(points, cost_quality_front(points))
        assert "crashy (1/6)" in text
        assert "may be the missing rows rather than a real trade" in text

    def test_a_non_finite_coordinate_is_excluded_not_undominatable(self) -> None:
        # Every >=/<= against NaN is False, so a NaN arm would be undominatable and render in bold
        # as a live trade. Same guard, same reason, as instance_best_front.
        nan = float("nan")
        points = [
            CostQualityPoint(variant_id="good", score=1.0, cost_per_row=0.1, row_ids=frozenset("abcdef")),
            CostQualityPoint(variant_id="broken", score=0.5, cost_per_row=nan, row_ids=frozenset("abcdef")),
        ]
        assert cost_quality_front(points) == ["good"]

    def test_coverage_is_a_set_test_not_a_count(self) -> None:
        """Two arms on disjoint row sets of equal size must not dominate each other.

        A count-based precondition (`other_rows >= n_rows`) reads as satisfied in BOTH directions
        here, so whichever arm is better on the two aggregate numbers takes the front — while
        neither has a single row of evidence about where the other was measured. `_dominates`
        gates on set coverage for exactly this reason, and the aggregate rule has to agree with it.
        """
        disjoint = [
            CostQualityPoint(variant_id="rows-abc", score=0.9, cost_per_row=1.0, row_ids=frozenset("abc")),
            CostQualityPoint(variant_id="rows-xyz", score=0.5, cost_per_row=2.0, row_ids=frozenset("xyz")),
        ]
        assert cost_quality_front(disjoint) == ["rows-abc", "rows-xyz"]

        # Same numbers, but now the better arm COVERS the other's rows — it may dominate.
        covering = [
            CostQualityPoint(variant_id="rows-abcxyz", score=0.9, cost_per_row=1.0, row_ids=frozenset("abcxyz")),
            CostQualityPoint(variant_id="rows-xyz", score=0.5, cost_per_row=2.0, row_ids=frozenset("xyz")),
        ]
        assert cost_quality_front(covering) == ["rows-abcxyz"]

    def test_the_point_reports_the_rows_it_was_measured_on(self, tmp_path: Path) -> None:
        points = self._points(tmp_path, {"only": {f"r{i}": (0.5, 1.0) for i in range(4)}})
        assert points[0].row_ids == frozenset({"r0", "r1", "r2", "r3"})
        assert points[0].n_rows == 4  # the derived count the render shows

    def test_render_renders_the_advisory_constant(self, tmp_path: Path) -> None:
        # The sensor for the "advisory only, the gate is unchanged" decision. Verbatim, so the
        # claim cannot drift between the render and the two prose surfaces.
        points = self._points(tmp_path, {"only": {f"r{i}": (0.5, 1.0) for i in range(4)}})
        assert COST_FRONT_ADVISORY in render_cost_quality(points, cost_quality_front(points))

    def test_the_control_arm_sits_on_the_front_by_construction(self, tmp_path: Path) -> None:
        # Cheap and bad is undominated, so the emptied-body control is on the front — which is why
        # the standing advisory tells the reader to read it with the arms they are choosing between.
        points = self._points(
            tmp_path,
            {
                "incumbent": {f"r{i}": (0.90, 1.00) for i in range(6)},
                "control": {f"r{i}": (0.05, 0.10) for i in range(6)},
            },
        )
        assert "control" in cost_quality_front(points)
        assert "cheap because it does less" in render_cost_quality(points, cost_quality_front(points))


class TestHeadroomCeiling:
    def test_headroom_ceiling_uses_full_row_count_as_denominator(self) -> None:
        """THE load-bearing property: a 2-row subset of a 15-row suite divides by 15.

        Dividing by the subset reports a per-row LIFT, which makes every rule look promotable —
        and the gate compares a suite MEAN, over every row including the ones at ceiling.
        """
        ceiling = headroom_ceiling(HEADROOM_ROW_SCORES, rule="R1", rows=HEADROOM_RULE_ROWS["R1"])
        assert ceiling.n_failing == 2
        assert ceiling.headroom == pytest.approx(0.45)
        assert ceiling.ceiling == pytest.approx(0.45 / 15)
        # The subset-denominator reading, spelled out so the difference is a number rather than an
        # argument: it is 7.5x larger, and it is above the floor rather than barely at it.
        assert ceiling.ceiling != pytest.approx(0.45 / 2)

    @pytest.mark.parametrize(
        ("rule", "expected"),
        [("R1", 0.0300), ("R6", 0.0223), ("R7", 0.0191), ("R8", 0.0095)],
    )
    def test_headroom_ceiling_reproduces_the_measured_round(self, rule: str, expected: float) -> None:
        # The round this whole block exists because of: three of these four rules could not clear
        # the 0.0255 floor however good a candidate was, and about $40 was spent gating them.
        ceiling = headroom_ceiling(HEADROOM_ROW_SCORES, rule=rule, rows=HEADROOM_RULE_ROWS[rule])
        assert round(ceiling.ceiling, 4) == expected
        assert (ceiling.ceiling >= HEADROOM_FLOOR) is (rule == "R1")

    def test_headroom_ceiling_empty_rows_is_zero(self) -> None:
        assert headroom_ceiling({}) == RuleCeiling(rule="", headroom=0.0, ceiling=0.0, n_failing=0, n_dropped=0)

    def test_an_empty_subset_is_not_the_whole_suite(self) -> None:
        # `rows=set()` and `rows=None` are DIFFERENT, and the difference is a real authoring path:
        # a rule that failed nowhere is ABSENT from `rule_row_map`, so `rule_map.get(rule)` is
        # None. Reading that as "every row" would report the suite's ceiling under that rule's name
        # — the exact inverse of the truth, which is that the rule has no headroom at all.
        assert headroom_ceiling(HEADROOM_ROW_SCORES, rule="R9", rows=set()).ceiling == 0.0
        assert headroom_ceiling(HEADROOM_ROW_SCORES).ceiling > 0.0

    def test_headroom_ceiling_counts_dropped_unknown_ids(self) -> None:
        # A stale rule map naming rows this run did not produce must not inflate the ceiling.
        ceiling = headroom_ceiling(HEADROOM_ROW_SCORES, rule="R1", rows={"sku-labels", "a-row-that-moved"})
        assert ceiling.n_failing == 1 and ceiling.n_dropped == 1
        assert ceiling.headroom == pytest.approx(0.25)

    def test_headroom_ceiling_excludes_non_finite_scores(self) -> None:
        # `_finite_scores`' convention, applied to a bare mapping: a NaN is ABSENT, so it neither
        # poisons the sum (every arithmetic op on it returns NaN) nor sits in the denominator.
        scores = {"a": 0.5, "b": float("nan"), "c": 1.0}
        ceiling = headroom_ceiling(scores)
        assert math.isfinite(ceiling.ceiling)
        assert ceiling.n_failing == 2 and ceiling.n_dropped == 1
        assert ceiling.ceiling == pytest.approx(0.5 / 2)

    def test_headroom_ceiling_clamps_scores_above_max(self) -> None:
        # A mis-scaled score must not cancel real headroom elsewhere — the row contributes 0.0,
        # never a negative that quietly subtracts from another row's room.
        assert headroom_ceiling({"a": 1.5, "b": 0.5}).headroom == pytest.approx(0.5)

    def test_max_score_scales_the_headroom(self) -> None:
        # A suite whose grader tops out below 1.0 has less room than its scores suggest.
        assert headroom_ceiling({"a": 0.5}, max_score=0.8).headroom == pytest.approx(0.3)
