### Seed stability

**UNSTABLE — would promote at 2/3 seeds. This is a coin flip, not a result: the decision is being made by the bootstrap draw rather than by the data. Do not report the majority's verdict as the verdict — raise n_resamples, or add rows, and gate again.**

- Seeds: 0, 1, 2
- p per seed: 0.0200, 0.0300, 0.0600
- p spread (max - min over the measured ones): 0.0400
- Cost: 3 bootstrap(s) over rows already loaded — CPU only, and **zero** extra agent runs.
- Decided at a **family of ONE** per seed, so `would promote` is NOT this round's decision if the round gated a shortlist: Holm's threshold there is rank-dependent and stricter. Compare the p spread above against that threshold instead.