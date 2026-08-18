### Stage C confirm — `candidate` vs `incumbent`

**REVERSED — the effect points the other way on held-out rows. Do not promote.**

- Suite `my-skill-activation`, family of ONE (only the Stage B winner is confirmed)
- Train effect (candidate - incumbent): 0.300
- Test effect on the held-out split: -0.300
- Delta: -0.600 · confirm split's own MDE: 0.125
- Outcome: **REVERSED**
- **Notes:**
  - REVERSED: the train effect was +0.300 and the confirm run measured -0.300 — opposite signs. The effect the round was built on does not hold on held-out rows; do not promote on it.

The confirm gate's own block is carried on `test_verdict` — print it with `render_markdown` (activation) or `render_execution_markdown` (execution) beside this one.