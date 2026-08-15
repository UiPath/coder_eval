### Activation gate — `candidate` vs `incumbent`

**CANNOT SEPARATE AT THIS SIZE — this suite cannot express a p below 0.0312 at 6 paired rows, and the Holm threshold for this rank in a family of 2 is 0.0250. This candidate could not have promoted however good it is — the bar sits below what the suite can measure — so this is NOT a negative result about it. Gate at most 1 survivor(s) at alpha=0.05, or raise the rows the two arms DISAGREE on from 3 to 4 at the current 6 paired rows — adding rows they agree on makes this floor worse, not better.**

- Suite `my-skill-activation`, criterion index 0 (position in `success_criteria`)
- Rows paired: 6 · discordant: 3 · excluded: 0
- f1.yes: incumbent 0.000 -> candidate 1.000
- Paired cluster bootstrap (candidate - incumbent): 1.000 95% CI [1.000, 1.000], p = 0.0380 over 2000 draws
- p floors: estimator 0.0010 at 2000 draws · this suite 0.0312
- Holm alpha: 0.050
- Interval excludes zero: True
- Range non-overlap (DIAGNOSTIC, not the gate): True
- Minimum detectable effect: 0.000
- **Guardrails:**
  - PASS · cost (USD/row): — -> — — no cost recorded on at least one arm — guardrail not evaluated
  - PASS · latency (seconds/row): 0.000 -> 0.000, diff CI [0.000, 0.000] vs floor 0.25 x incumbent — the incumbent measured zero, so a relative change is undefined
- **Notes:**
  - Holm applied across a family of 2 at alpha=0.05.