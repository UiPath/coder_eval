| row | **cand-broad** | **cand-r1** | **cand-r2** | cand-dominated | cand-crashed |
|---|---|---|---|---|---|
| r0 | — | 0.000 | 0.000 | — | — |
| r1 | 0.500 | 1.000 | 0.400 | 1.000 | — |
| r2 | 0.500 | 0.400 | 1.000 | 0.300 | — |

Pareto front (**bold**): cand-broad, cand-r1, cand-r2
Instance-best front (GEPA's, the merge shortlist): cand-r1, cand-r2, cand-dominated
The two fronts disagree, which is the interesting case rather than an inconsistency: on coverage without winning any row: cand-broad; wins a row despite being dominated overall: cand-dominated. Coverage is the set to DISCARD from; instance-best is the set to MERGE from.
Arms that scored no rows at all and are therefore NOT on the front: cand-crashed. That is a wiring or crash problem, not a result.
Rows missing from at least one arm, shown as — and excluded from the domination comparison rather than counted as 0.0: r0, r1, r2
Rows no arm scored above zero: r0. These contribute nothing to the front — usually a broken row or an unmet fixture precondition rather than N bad candidates.