**Regression corpus check** — a row scoring below 1.000 is a row an earlier promotion was built on and this arm gives back.

- `incumbent` — clears the corpus.
- `cand-a` — 1 measured loss(es), 1 hole(s):
    - **lost** `pos-3` at 0.667 (promoted in round 1; promoted on it)
    - **hole** `pos-7` — this arm has NO score for it (promoted in round 2; promoted on it)

A **hole** is not a loss and not a pass. Two causes the corpus cannot distinguish: the row errored in this run, or it belongs to this skill's OTHER suite — the corpus is per skill, and a skill may carry both an activation and an outcome suite. Check which before reporting it, and do not count it as a regression until you have.

A measured loss is a regression however good the arm's aggregate looks, which is exactly what an aggregate cannot show. Shortlist against this, not around it.