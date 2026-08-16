### Activation gate — `candidate` vs `incumbent`

**NOT A RESULT — the two arms recorded DIFFERENT row selections (splits: 'test', 'train') — 'incumbent' over <TMP>/inc-run-0, <TMP>/inc-run-1 and 'candidate' over <TMP>/cand-run-0, <TMP>/cand-run-1. They did not score the same rows, so their difference is not an effect. Re-run both arms under one --split before gating.**

- Suite `my-skill-activation`, criterion index 0 (position in `success_criteria`)
- Rows paired: 6 · discordant: — · excluded: 0
- f1.yes: incumbent — -> candidate —
- Paired cluster bootstrap (candidate - incumbent): — 95% CI [—, —], p = — over 400 draws
- p floors: estimator 0.0050 at 400 draws · this suite —
- Holm alpha: 0.050
- Interval excludes zero: False
- Range non-overlap (DIAGNOSTIC, not the gate): False
- Minimum detectable effect: —