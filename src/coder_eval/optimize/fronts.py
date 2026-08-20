"""The three fronts over the Stage A row matrix, and the vectors they are computed from.

Rank 3 of the optimize family. Each front answers a different question and none contains the
others: :func:`pareto_front` (not dominated on the row vector — the DISCARD rule),
:func:`instance_best_front` (GEPA's, wins at least one row — the MERGE shortlist, since it keeps an
arm that owns a single row), and :func:`cost_quality_front` (2-D quality x cost, and **advisory
only**).

All three treat a hole as ABSENT rather than zero, require row coverage before one arm may dominate
another, and guard non-finite values — every ``>=`` against NaN is False, so an unguarded NaN cell
makes its arm undominatable and puts it on the front in bold. The mechanisms differ because the
three answer different questions; a parametrized test asserts they agree rather than leaving the
claim to prose.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import NamedTuple

from coder_eval.models import (
    ArmRowScores,
)
from coder_eval.optimize.load import (
    load_arm_rows,
    load_suite_rows,
    pool_replicates,
    reconcile_arms,
    require_valid_criterion_index,
    row_cost_levels,
    row_costs,
    row_score,
    stale_tree_reason,
)
from coder_eval.reports_stats import (
    mean,
    median_or_none,
)


logger = logging.getLogger(__name__)


def arm_row_scores(
    *,
    run_dirs: Sequence[Path],
    variant_ids: Sequence[str],
    suite_id: str,
    criterion_index: int | None = None,
) -> list[ArmRowScores]:
    """Each arm's per-row score vector, averaged across replicates.

    ``run_dirs`` is a LIST, like every other function here, because Stage A and Stage B are
    separate invocations — a single-run-dir signature would silently compute the front off one
    replicate. Replicates reduce to one number per (row, arm) by the **mean**, the same reduction
    ``paired_comparison`` applies before pairing, so the two surfaces agree about what a row scored.

    ``criterion_index=None`` reads the row's ``weighted_score`` (the execution track); an index
    reads that criterion's score (the activation track). A row an arm produced no score for is
    left ABSENT from the vector rather than recorded as 0.0 — see :func:`pareto_front`.

    **A contaminated tree WARNS rather than refusing** (CE053), and that is a consequence of the
    return type rather than a softer stance: ``ArmRowScores`` is a vector with nowhere to put a
    refusal, and the three fronts computed from it take a list. Both noise floors, which return
    ``NoiseFloor | None``, refuse on the same detection. The message is the same either way
    (:func:`stale_tree_reason`) so the two read alike in a log.
    """
    require_valid_criterion_index(criterion_index)
    # ONE sweep over every arm before any load. It does NOT save a `run.json` parse — reconciling
    # is per (variant, dir) either way — but it collects every location into one message, so a run
    # dir carrying both arms reports one warning naming both rather than one warning per arm for
    # what is a single re-used `--run-dir`.
    stale, _unknown_dirs = reconcile_arms([(vid, run_dirs) for vid in variant_ids], suite_id)
    if stale:
        logger.warning("Row scores may be over a contaminated tree: %s", stale_tree_reason(stale))
    arms: list[ArmRowScores] = []
    for variant_id in variant_ids:
        rows = pool_replicates([load_suite_rows(d, variant_id, suite_id) for d in run_dirs])
        scores: dict[str, float] = {}
        for row_id, results in sorted(rows.items()):
            values = [v for r in results if (v := row_score(r, criterion_index)) is not None]
            if values:
                scores[row_id] = mean(values)
        arms.append(ArmRowScores(variant_id=variant_id, row_scores=scores))
    return arms


def _finite_scores(arm: ArmRowScores) -> dict[str, float]:
    """An arm's row vector with non-finite cells removed — a NaN is treated as ABSENT.

    The same guard :func:`instance_best_front` and :func:`cost_quality_front` already apply, in the
    one place the coverage front was missing it. Every ``>=`` and ``>`` against NaN is False, so a
    NaN cell makes its arm undominatable by anyone AND unable to dominate anyone — it takes the
    front by incomparability, rendered in bold beside arms that earned it. Treating it as absent
    instead routes it through the coverage rule, which is the answer already agreed for a hole.

    Nothing produces a non-finite score today (``row_score`` returns means of scores bounded
    [0, 1]), which is exactly why it would be silent.
    """
    return {row_id: value for row_id, value in arm.row_scores.items() if math.isfinite(value)}


def _dominates(a: ArmRowScores, b: ArmRowScores) -> bool:
    """True when ``a`` covers every row ``b`` scored, matches it on all of them, and beats it on one.

    Holes are handled by requiring **coverage**, and both halves of that matter:

    - A missing cell is never read as 0.0. That would fabricate domination against the arm that
      happens to have the hole, which is the opposite of what a hole means.
    - An arm cannot dominate on the rows it happens to share while being ABSENT from a row the
      other arm won. It has no evidence there, and "at least as good everywhere" is a claim about
      everywhere the other arm was measured — so it is not entitled to make it.

    A non-finite cell counts as a hole rather than as a value — see :func:`_finite_scores`.
    """
    a_scores, b_scores = _finite_scores(a), _finite_scores(b)
    scored_by_b = sorted(b_scores)
    if not scored_by_b or not set(scored_by_b) <= set(a_scores):
        return False
    return all(a_scores[r] >= b_scores[r] for r in scored_by_b) and any(a_scores[r] > b_scores[r] for r in scored_by_b)


def pareto_front(arms: list[ArmRowScores]) -> list[str]:
    """Variant ids no other arm dominates on the row vector.

    Identical arms all stay on the front (nothing is strictly better), which is itself the finding:
    the candidates did not differ anywhere the suite could see. A single arm is its own front.

    An arm that scored **no** rows is excluded rather than undominatable. Nothing can cover an
    empty vector, so the domination rule alone would put a candidate that crashed on every row on
    the front — rendered indistinguishably from one that won something nobody else did. An arm
    whose every cell is non-finite is excluded by the same rule, since :func:`_finite_scores`
    leaves it with an empty vector.
    """
    scored = [arm for arm in arms if _finite_scores(arm)]
    return [
        arm.variant_id
        for i, arm in enumerate(scored)
        if not any(_dominates(other, arm) for j, other in enumerate(scored) if i != j)
    ]


def instance_best_front(arms: list[ArmRowScores]) -> list[str]:
    """Variant ids achieving the highest score on at least one row — GEPA's frontier definition.

    A DIFFERENT set from :func:`pareto_front`, and the difference is the point. Ours is
    "not dominated on the row vector", which is the right rule for **discarding**: an arm off it
    was matched or beaten on every row it was measured on. GEPA's is the right rule for **merging**,
    because
    it deliberately retains an arm that wins exactly one row — precisely the raw material a merge
    candidate is built from, and precisely what a coverage rule can drop.

    Neither contains the other. Measured on a four-arm fixture:
    ``A={r1:0.5, r2:0.5}``, ``B={r1:1.0, r2:0.4}``, ``C={r1:0.4, r2:1.0}``, ``D={r1:1.0, r2:0.3}``
    gives coverage ``[A, B, C]`` and instance-best ``[B, C, D]``. ``A`` is dominated by nobody yet
    wins nothing; ``D`` ties a row's maximum yet is dominated outright by ``B``.

    The maximum on a row is taken over the arms that SCORED it — a hole is never a zero, exactly as
    in :func:`_dominates` — so an arm that alone measured a row is trivially the best on it.

    Ties all qualify: an arm equal to the best on a row achieved the best on it, which mirrors
    :func:`pareto_front` keeping identical arms. Exact ``==`` is the right comparison and a
    tolerance would silently widen the front — every score comes from the same ``mean(values)``
    reduction over the same replicates, so equal scores are equal for a reason, not by luck.

    An arm that scored no rows is excluded for the same reason it is there: nothing about an empty
    vector is a win. Returns in input order, matching :func:`pareto_front`.
    """
    scored = [arm for arm in arms if arm.row_scores]
    best: dict[str, float] = {}
    for arm in scored:
        for row_id, value in arm.row_scores.items():
            # Non-finite values are skipped when SEEDING the maximum. `value > nan` is False, so a
            # single NaN landing in a row would pin that row's maximum at NaN forever — and then
            # `v == best[r]` is False for every arm, dropping not just the NaN arm but the arm that
            # genuinely won the row. Nothing produces one today (`row_score` returns means of
            # scores bounded [0, 1]), which is exactly why it would be silent.
            if math.isfinite(value) and (row_id not in best or value > best[row_id]):
                best[row_id] = value
    return [arm.variant_id for arm in scored if any(v == best.get(r) for r, v in arm.row_scores.items())]


class RuleCeiling(NamedTuple):
    """The largest suite-mean gain any candidate for one rule could possibly produce.

    A NamedTuple rather than a float, for the same reason :class:`CostQualityPoint` is one: the
    render needs the headroom, the rows it was summed over and the ids it had to drop, and a bare
    float has no channel for any of them. Computed and rendered, never persisted.
    """

    rule: str  # "" for the suite-level ceiling, which is every row rather than a rule's subset
    headroom: float  # sum of (max_score - score) over the selected rows, each clamped at zero
    ceiling: float  # headroom / the FULL row count — see `headroom_ceiling` for why
    n_failing: int  # rows actually summed over
    n_dropped: int  # selected ids that were absent from `row_scores`, or non-finite


def headroom_ceiling(
    row_scores: dict[str, float],
    *,
    rule: str = "",
    rows: Collection[str] | None = None,
    max_score: float = 1.0,
) -> RuleCeiling:
    """The most a further round could still win on this suite. The one number that can say STOP.

    Computed over the rows every arm already gets right plus the rows none of them do: the first
    group has nothing left to win and the second is what is still on the table. Read as a SCORE it is
    meaningless; read as a CEILING it bounds what another round can buy.

    ``None`` when it cannot be computed rather than 0.0, which would claim there is no headroom.
    A hole is absent, never zero.
    See .claude/decisions/2026-08-20-the-advisory-fronts.md.
    """
    finite = {row_id: value for row_id, value in row_scores.items() if math.isfinite(value)}
    # `is None`, never truthiness: an empty `rows` is a real, empty selection.
    selected = set(row_scores) if rows is None else set(rows)
    usable = sorted(selected & finite.keys())
    headroom = sum((max(0.0, max_score - finite[row_id]) for row_id in usable), 0.0)
    return RuleCeiling(
        rule=rule,
        headroom=headroom,
        ceiling=headroom / len(finite) if finite else 0.0,
        n_failing=len(usable),
        n_dropped=len(selected) - len(usable),
    )


class CostQualityPoint(NamedTuple):
    """One arm's position on the quality x cost plane.

    A NamedTuple rather than a Pydantic model because it is computed and rendered, never
    persisted — the same call the module already makes for ``ArmRowScores`` in the other direction
    (that one IS persisted, in ``RoundScores``, which is why it is a model).
    """

    variant_id: str
    score: float | None  # mean of the arm's per-row scores; None when it scored nothing
    cost_per_row: float | None  # median of the arm's per-row mean cost; None when nothing recorded
    # The rows BOTH coordinates are reduced over — the identities, not just how many. `_dominates`
    # gates domination on set COVERAGE, and a count cannot express that: two arms measured on four
    # rows each, disjoint, would each look entitled to dominate the other.
    row_ids: frozenset[str] = frozenset()

    @property
    def n_rows(self) -> int:
        """How many rows this arm was measured on — what the render shows."""
        return len(self.row_ids)


def cost_quality_points(
    *,
    run_dirs: Sequence[Path],
    variant_ids: Sequence[str],
    suite_id: str,
    criterion_index: int | None = None,
) -> list[CostQualityPoint]:
    """The (cost, quality) points :func:`cost_quality_front` ranks, per arm.

    Separated from the ranking so a caller can plot or report the raw pairs without inheriting the
    front's advisory-only reading. A hole is absent, never zero — an arm missing a measurement on a
    row contributes no point for it rather than a zero.
    See .claude/decisions/2026-08-20-the-advisory-fronts.md.
    """
    require_valid_criterion_index(criterion_index)
    arms = arm_row_scores(
        run_dirs=run_dirs, variant_ids=variant_ids, suite_id=suite_id, criterion_index=criterion_index
    )
    points: list[CostQualityPoint] = []
    for arm in arms:
        # CE053: reached through `arm_row_scores` above, which reconciles every arm in one sweep.
        # A second reconcile here would read every run.json twice per arm and warn twice about one
        # fault — measured, and the reason the suppression is the record rather than a second call.
        rows = load_arm_rows(run_dirs, arm.variant_id, suite_id)  # noqa: CE053
        scored_ids = sorted(arm.row_scores)
        clusters = [row_costs(rows.get(rid, [])) for rid in scored_ids]
        levels = row_cost_levels(clusters)
        # `row_cost_levels` drops a row whose cost is not finite, which would otherwise put this
        # coordinate on FEWER rows than the score coordinate while `row_ids` still claimed the full
        # set — and an arm could then dominate on a cost it under-measured. Shrinking `row_ids`
        # instead would be worse: those rows SCORED fine, and narrowing coverage forfeits the arm's
        # own dominance claim over a cost figure. So the honest answer is that this arm's cost per
        # row is unknown, which is the same `None` an arm recording no cost at all reports, and the
        # 2-D front already excludes a point with no cost rather than placing it at zero.
        unusable = len(levels) != len([c for c in clusters if c])
        points.append(
            CostQualityPoint(
                variant_id=arm.variant_id,
                score=mean(list(arm.row_scores.values())) if arm.row_scores else None,
                cost_per_row=None if unusable else median_or_none(levels),
                row_ids=frozenset(scored_ids),
            )
        )
    return points


def cost_quality_front(points: list[CostQualityPoint]) -> list[str]:
    """Rank arms by quality per unit cost. **ADVISORY ONLY — never a shortlist.**

    A ratio has no defensible threshold, which is exactly why this may not narrow a field. Presenting
    it as a shortlist invites promoting the cheapest arm that happens to score, and reading it as the
    Pareto front (a DISCARD rule) or the instance-best front (a MERGE shortlist) is the reading error
    this separation exists to prevent.

    A hole is absent, never zero.
    See .claude/decisions/2026-08-20-the-advisory-fronts.md.
    """
    # Narrowed to plain floats up front rather than suppressing the comparison's type error: the
    # filter IS the exclusion rule, so making it produce a non-optional shape is what keeps the
    # rule and the types saying the same thing.
    measured = [
        (p.variant_id, p.score, p.cost_per_row, p.row_ids)
        for p in points
        if p.score is not None
        and p.cost_per_row is not None
        and math.isfinite(p.score)
        and math.isfinite(p.cost_per_row)
    ]
    return [
        variant_id
        for i, (variant_id, score, cost, row_ids) in enumerate(measured)
        if not any(
            row_ids <= other_ids
            and other_score >= score
            and other_cost <= cost
            and (other_score > score or other_cost < cost)
            for j, (_o_id, other_score, other_cost, other_ids) in enumerate(measured)
            if i != j
        )
    ]
