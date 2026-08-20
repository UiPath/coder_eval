# The three fronts, and why only one of them is a shortlist

Subject: `optimize/fronts.py::arm_row_scores`, `pareto_front`, `instance_best_front`,
`cost_quality_front`, `cost_quality_points`, `headroom_ceiling`.

## Three fronts, three different jobs

- **Pareto** is a DISCARD rule. An arm dominated on every axis cannot be the answer, so dropping it
  costs nothing.
- **Instance-best** is a MERGE shortlist: the arms that win at least one row. A candidate that wins
  nowhere has nothing to contribute to a merge.
- **Cost/quality is ADVISORY only.** It is a 2-D Pareto filter over (quality, cost) — the arms
  nothing beats on both — returned in INPUT order, with no ranking inside the front. It is NOT a
  ratio: a ratio has no defensible threshold, and inventing an order here is exactly what would turn
  an advisory into a shortlist, inviting promotion of the cheapest arm that happens to score.

Reading the wrong front as the others is the failure this separation exists to prevent, and it is a
reading error rather than an arithmetic one, so the names carry the distinction.

## A hole is absent, never zero — on all three

An arm with no measurement on a row is missing there. Folding the hole to 0.0 makes an arm that
failed to produce a number rank below one that produced a bad number, which inverts the front rather
than biasing it. `is not None` rather than truthiness, because a free model is legitimately the
cheapest arm and `0.0` is a real cost.

## Domination is gated on row-set COVERAGE, and that conjunct is load-bearing

Without `row_ids <= other_ids`, an arm measured on a SUBSET can dominate one measured on more rows.
Measured: an arm that crashed 5 of 6 rows and scored 1.0 on the sixth knocked the incumbent off the
front. A count cannot express this — two arms on four disjoint rows each would both look entitled to
dominate the other.

## `headroom_ceiling` bounds what is left to win, not what was won

It answers "is another round worth paying for?" for ONE arm. Read as a score it is meaningless; read
as a ceiling it is the only number in the family that can say *stop*.

Two things about it were deleted once by an over-eager docstring trim and are the reason this section
exists. The ceiling's denominator is the arm's FULL finite row count and never the selected subset —
a rule failing 3 of 15 rows has a ceiling of 0.1, and dividing by the subset overstates it 5x, which
makes every rule look promotable. And `rows=None` (every row) is a different question from
`rows=set()` (an empty selection), which matters because `rule_row_map` OMITS a rule that failed
nowhere: passing that missing entry as `None` reports the whole suite's ceiling under that rule's
name.

## A contaminated tree WARNS here rather than refusing

The return types are vectors and fronts with nowhere to put a refusal, and the caller is a human
reading a Stage A table rather than a gate deciding a promotion. So `arm_row_scores` reconciles and
logs. That is a deliberate asymmetry with the two gates, and CE053 is what keeps the reconciliation
from being dropped altogether: measured, without it `arm_row_scores` returned a stale row in its
vector and all three fronts were computed over it.
