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
    """The arithmetic bound on what a candidate targeting ``rule`` could move the suite mean by.

    ::

        max_effect(R) = SUM over rows failing R of (max_score - score)  /  n_rows

    A candidate can only gain where the incumbent lost, so the rows failing ``R`` bound the whole
    effect — and the suite mean divides that by every row, including the ones already at ceiling.
    Measured on a real 15-row suite against a noise floor of 0.0255: R1 0.0300 (1.18x the floor),
    R6 0.0223 (0.87x), R7 0.0191 (0.75x), R8 0.0095 (0.37x). **Three of the four candidates written
    for that round were unpromotable by arithmetic**, and about $40 was spent gating them — on
    inputs (a baseline and a noise floor) that had already been paid for.

    **The denominator is the FULL row count, never the selected subset.** That is the whole point:
    a per-row lift divided by the rows that failed makes every rule look promotable, and the suite
    mean the gate actually compares is an average over all of them. Every row *passing* ``R``
    therefore makes ``R`` harder to promote, which is what the depth-over-breadth rule in
    ``/coder-eval:task`` is about.

    ONE function for both questions — the suite-level "can any candidate promote here?"
    (``rows=None``, every row) and the rule-level "can a candidate for R?" — because they are the
    same arithmetic over different subsets. Note that ``rows=None`` and ``rows=set()`` are
    DIFFERENT and the difference matters: a rule that failed nowhere is absent from
    :func:`~coder_eval.optimize.load.rule_row_map`, and passing its missing entry through as
    ``None`` would silently report the whole suite's ceiling under that rule's name.

    **It is advisory and never gates.** A "below the floor" verdict is arithmetically sound — the
    attribution behind it is an upper bound (see ``rule_row_map``) — but the attribution itself is
    AUTHORED, and a mistyped rule id moves rows between rules. A wrong annotation must not be able
    to block a real promotion.

    **The denominator is the rows this ARM produced, not the suite's declared row count**, because
    a hole is all ``row_scores`` can carry — an arm that lost 5 of 15 rows therefore reports
    ceilings computed over 10, which is the same intersection semantics the paired gate uses and
    is 1.5x larger than the suite-wide reading. Check the row matrix's holes before acting on a
    ceiling from a partly-crashed arm; ``n_dropped`` counts only ids in the SELECTED subset, so a
    hole outside it is invisible here.

    Per-row headroom is clamped at zero, so a mis-scaled score above ``max_score`` cannot cancel
    real headroom elsewhere. Non-finite scores are treated as ABSENT — :func:`_finite_scores`'
    convention, applied to a bare mapping — so a NaN cell neither poisons the sum nor inflates the
    denominator; it is counted in ``n_dropped``, which the render prints.
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
    """Each arm's mean row score against its median per-row cost.

    Quality reuses :func:`arm_row_scores`, so this view and the row matrix cannot disagree about
    what a row scored. Cost reuses :func:`row_cost_levels`, so it and the guardrail cannot
    disagree about what a row cost.

    **BOTH coordinates are reduced over the same rows: the ones the arm actually scored.** Cost is
    read only for those rows, not for every row the arm has on disk. Without that restriction an
    arm that CRASHED most of its rows is described by two different samples — its quality averaged
    over the handful it completed, its cost over all of them, holes included, because a crashed row
    still records a `total_cost_usd`. Reproduced before the fix: an arm completing 1 of 6 rows at a
    perfect score took the whole front and knocked the incumbent off it, rendered as two clean
    numbers with nothing to show the other five rows were missing. ``n_rows`` reports the count so
    the render can say which arms are standing on less evidence, exactly as `_dominates` requires
    row coverage and `render_row_matrix` prints `—`.

    ``cost_per_row`` is the **median** over those rows — the same reduction
    ``GuardrailCheck.incumbent`` reports, so the two surfaces print the same number. Parity is
    close but not exact by construction, and the two reasons are worth knowing: the guardrail
    balances replicate counts *pairwise between two arms* before reducing, and it reduces over the
    rows BOTH arms scored (or the explicit ``row_ids`` the gate hands it) rather than over one
    arm's own. An N-arm view can do neither. Where every arm scored every row with equal replicate
    counts — the ordinary case — they agree exactly.

    ``criterion_index=None`` reads each row's ``weighted_score`` (the execution track); an index
    reads that criterion's score (the activation track). The same switch ``arm_row_scores``
    already has, rather than a second track parameter.
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
        levels = row_cost_levels([row_costs(rows.get(rid, [])) for rid in scored_ids])
        points.append(
            CostQualityPoint(
                variant_id=arm.variant_id,
                score=mean(list(arm.row_scores.values())) if arm.row_scores else None,
                cost_per_row=median_or_none(levels),
                row_ids=frozenset(scored_ids),
            )
        )
    return points


def cost_quality_front(points: list[CostQualityPoint]) -> list[str]:
    """Variant ids no other arm beats on BOTH quality and cost.

    An arm is dominated when another scores at least as well AND costs at most as much, with at
    least one of the two strict — **and covers every row it was measured on.** Ties therefore all
    stay, matching :func:`pareto_front`.

    That last clause is the same coverage precondition :func:`_dominates` applies to the row
    vector, and it is load-bearing for the same reason. Without it an arm that CRASHED on five of
    six rows and scored 1.0 on the sixth dominates an incumbent that scored 0.9 on all six at the
    same cost — measured, and it knocked the incumbent off the front entirely. An arm standing on
    less evidence is not entitled to a claim about "everywhere"; it stays on the front itself,
    where :func:`render_cost_quality` names its row count, rather than displacing an arm that was
    actually measured.

    Coverage is a SET test, not a count. Two arms measured on four rows each, on disjoint rows,
    each have "at least as many" as the other and would each be entitled to dominate — while
    neither has any evidence about where the other was measured. Comparing the row ids is what
    makes the aggregate rule agree with the row-vector one it is modelled on.

    An arm missing either coordinate is **excluded**, mirroring how ``pareto_front`` treats an arm
    with an empty vector: a point with no cost is not a free point, it is an unmeasured one, and
    putting it on the front would render it indistinguishable from the genuinely cheapest arm.
    :func:`render_cost_quality` names the excluded arms rather than dropping them silently.

    A **zero** cost is a real coordinate, not a missing measurement — a free model is legitimately
    the cheapest arm there is. So the test is ``is not None``, never truthiness, the same rule
    ``register_pricing`` states for an all-zero rate.

    A **non-finite** coordinate is excluded for the opposite reason: every ``>=`` / ``<=`` against
    NaN is False, so a NaN arm is undominatable and would render in bold as a live trade.

    **All three fronts guard non-finite values**, and now agree about them: this one excludes the
    arm, :func:`instance_best_front` skips the cell when seeding a row's maximum, and
    :func:`pareto_front` treats it as a hole via :func:`_finite_scores`. The mechanisms differ
    because the three answer different questions; the outcome — a non-finite cell never wins
    anything and never makes its arm undominatable — is the same, and a parametrized test asserts
    it across all three rather than leaving this sentence to be believed.
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
