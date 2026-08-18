### Execution gate — `candidate` vs `incumbent`

**PROMOTED**

- Suite `my-skill-activation`, per-row `weighted_score` through the reporter's paired comparison
- Rows paired: 4 · excluded: 0
- Paired mean difference (candidate - incumbent, sign resolved by the tool): 0.475 95% CI [0.337, 0.613]
- Cohen's d: 5.485 · p = 0.0016
- Holm alpha: 0.050
- Interval excludes zero: True
- Minimum detectable effect (weighted_score): 0.000
- Dead weight: UNKNOWN — see notes for why it could not be computed
- **Integrity checks:**
  - PASS · engagement recall.yes [criterion 0]: 0.375 -> 1.000
  - PASS · completion_rate: 1.000 -> 1.000 — 8/8 candidate replicate(s) scored against 8/8 incumbent
- **Guardrails:**
  - PASS · cost (USD/row): — -> — — no cost recorded on at least one arm — guardrail not evaluated
  - PASS · latency (seconds/row): 0.000 -> 0.000, diff CI [0.000, 0.000] vs floor 0.25 x incumbent — the incumbent measured zero, so a relative change is undefined
- **Notes:**
  - dead weight is UNKNOWN: this run predates `CriterionResult.weight`, so the blend behind `weighted_score` is not recorded in the artifact. Re-run to record it — the share is deliberately not reported as 0.0, which would claim no dilution.
  - this suite's minimum detectable effect came back 0.000, so the difference above was NOT checked against a noise floor. A null split measures zero only when every row's replicates agreed exactly — a deterministic suite, or one whose rows all failed the same way — so read it as 'the floor could not be priced', never as 'this suite can resolve anything'. Raise --repeats, and check the rows actually ran, before treating a small difference here as an effect.
  - Holm applied across a family of 1 at alpha=0.05.