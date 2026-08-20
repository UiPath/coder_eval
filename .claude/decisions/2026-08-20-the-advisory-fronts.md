# The three fronts, and why only one of them is a shortlist

Subject: `optimize/fronts.py::arm_row_scores`, `pareto_front`, `instance_best_front`,
`cost_quality_front`, `cost_quality_points`, `headroom_ceiling`.

## Three fronts, three different jobs

- **Pareto** is a DISCARD rule. An arm dominated on every axis cannot be the answer, so dropping it
  costs nothing.
- **Instance-best** is a MERGE shortlist: the arms that win at least one row. A candidate that wins
  nowhere has nothing to contribute to a merge.
- **Cost/quality is ADVISORY only.** It ranks by a ratio, and a ratio has no defensible threshold —
  which is exactly why it may not narrow a field. Presenting it as a shortlist invites promoting the
  cheapest arm that happens to score.

Reading the wrong front as the others is the failure this separation exists to prevent, and it is a
reading error rather than an arithmetic one, so the names carry the distinction.

## A hole is absent, never zero — on all three

An arm with no measurement on a row is missing there. Folding the hole to 0.0 makes an arm that
failed to produce a number rank below one that produced a bad number, which inverts the front rather
than biasing it.

## `headroom_ceiling` bounds what is left to win, not what was won

It answers "is another round worth paying for?" — so it is computed over the rows every arm already
gets right plus the rows none of them do. Read as a score it is meaningless; read as a ceiling it is
the only number in the family that can say *stop*.

## A contaminated tree WARNS here rather than refusing

The return types are vectors and fronts with nowhere to put a refusal, and the caller is a human
reading a Stage A table rather than a gate deciding a promotion. So `arm_row_scores` reconciles and
logs. That is a deliberate asymmetry with the two gates, and CE053 is what keeps the reconciliation
from being dropped altogether: measured, without it `arm_row_scores` returned a stale row in its
vector and all three fronts were computed over it.
