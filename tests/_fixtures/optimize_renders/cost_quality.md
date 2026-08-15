| arm | rows | mean row score | median cost/row (USD) |
|---|---|---|---|
| **incumbent** | 4 | 0.900 | 1.0000 |
| **cand-cheap** | 4 | 0.880 | 0.6000 |
| **cand-thin** | 2 | 0.950 | 0.5000 |
| cand-costless | 4 | 0.950 | — |

Cost/quality front (**bold**): incumbent, cand-cheap, cand-thin
Arms scored on fewer rows than the best-covered arm: cand-thin (2/4). Both of their coordinates are averages over that smaller sample, so a favourable position here may be the missing rows rather than a real trade — check the row matrix before reading it.
Arms missing a coordinate and therefore NOT on the front: cand-costless. An unmeasured cost is not a free one, so they are excluded rather than placed at zero.

This front is advisory. Promotion is unchanged: the primary statistic must separate and every guardrail must hold, so a cheaper arm here is a trade to offer the user, never a promotion this tool makes. Read it with the arms you are actually choosing between: any arm that is cheap because it does less — an emptied-body control, say — sits on this front by construction, since nothing dominates an arm nobody is trying to beat on cost.