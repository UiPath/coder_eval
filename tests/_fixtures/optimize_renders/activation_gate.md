### Activation gate — `candidate` vs `incumbent`

**NOT PROMOTED**

- Suite `my-skill-activation`, criterion index 0 (position in `success_criteria`)
- Rows paired: 8 · discordant: 4 · excluded: 0
- f1.yes: incumbent 0.000 -> candidate 1.000
- Paired cluster bootstrap (candidate - incumbent): 1.000 95% CI [1.000, 1.000], p = 0.0100 over 400 draws
- p floors: estimator 0.0050 at 400 draws · this suite 0.0078
- Holm alpha: 0.050
- Interval excludes zero: True
- Range non-overlap (DIAGNOSTIC, not the gate): True
- Minimum detectable effect: 0.000
- **Sibling checks:**
  - FAIL · sibling recall.yes [criterion 1]: 1.000 -> 0.875, rate 0.125
- **Guardrails:**
  - PASS · cost (USD/row): — -> — — no cost recorded on at least one arm — guardrail not evaluated
  - PASS · latency (seconds/row): 0.000 -> 0.000, diff CI [0.000, 0.000] vs floor 0.25 x incumbent — the incumbent measured zero, so a relative change is undefined
- **Notes:**
  - not promoted: the interval separates but a sibling's recall.yes dropped — this candidate moved the failure rather than fixing it.
  - p = 0.0100 is at or near this bootstrap's resolution floor (0.0050 at 400 draws), and the Holm threshold for this rank is 0.0500. Where the threshold approaches the floor the decision is being made by the resample count rather than by the data — re-run the gate with a larger n_resamples before believing either answer. A small suite has its own coarser floor: with few positive rows the smallest achievable p is bounded well above the estimator's.
  - Holm applied across a family of 1 at alpha=0.05.