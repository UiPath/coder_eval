# The optimize-skill method

The track-invariant half of `/coder-eval:optimize-skill`: what the three stages cost, what
each one does and does not bound, why the two gates use different machinery, and how to read
the paired-diff sign. The skill itself carries the *procedure* — which suite, which files,
which commands, in what order. This carries the *method*, because it is the same method on
both tracks and it is what has to be right for a verdict to mean anything.

Read it before Stage A, and again before stating any verdict.

## Three stages, and the gate

State the projected run count before each stage and ask. With N candidates, S survivors,
`M_train` train rows and `M_test` test rows:

| Spend | Runs |
| --- | --- |
| Step 6 baseline | `M_train` |
| Stage A — triage | `(N+1) × M_train` |
| Stage B — gate, activation track | `3 × (S+1) × M_train` |
| Stage B — gate, execution track | `6 × M_train` per candidate gated |
| Stage C — confirm | `6 × M_test` |

The two Stage B rows are the two gates below, priced. Activation runs `S+1` arms through
**three separate invocations**, so the survivor count multiplies. Execution runs exactly
**two** arms — incumbent plus one candidate — at `--repeats 3`, which is `2 × 3 = 6` runs per
train row *per candidate you choose to gate*; gating three candidates one after another is
three times that, not one pass with `S = 3`. Reading the activation formula on the execution
track overstates a single gate and understates a sequence of them.

**The baseline is a real line item, and it is not redundant with Stage A's incumbent arm.**
It looks like the same measurement on the same rows, and that is precisely its value: the
baseline runs through the *task's own* skill source, while Stage A's incumbent runs through a
*snapshot you built*. Agreement between them is the wiring check — it is what proves the
snapshot machinery did not change what is being measured. Quote both numbers when you
compare them.

**Every stage runs the suite through the experiment.** The suite is the positional
argument; the experiment carrying the arms is passed with `-e`. Passing the experiment file
positionally instead would treat it as a task, which resolves to a skipped task and a green
run of zero rows.

### Stage A — triage (cheap)

All candidates plus the incumbent — that is `round<N>-triage.yaml` — in **one** invocation,
`--split train`:

```bash
coder-eval run <suite> -e <experiment> --split train
```

Rank each variant from its `suite.json`, then discard anything at or below the incumbent:

- **Activation track** — the target label's F1, `metrics["f1.yes"]`.
- **Execution track** — `average_weighted_score`, or `pass_rate` when the criteria are all
  binary. Rank on the suite-level number, then look at which *criteria* moved: a candidate
  that gains on one criterion while losing another is not ahead, it has traded.

Check `completion_rate` on every arm before ranking anything. An arm that lost rows computed
its score over a different denominator and is not comparable — that is a re-run, not a
ranking.

It is **not** a top-level `suite.json` key: it sits in each entry of `criterion_aggregates`,
alongside that entry's `rows_total` and `rows_excluded`. A suite stacking several criteria
therefore has one per criterion. Read the one belonging to the criterion you are gating on,
and if two disagree, say so rather than picking — divergent denominators across criteria on
the same rows means something dropped mid-run.

This decides nothing — it only narrows. Replicate pooling is irrelevant here because
nothing is being gated on.

### Stage B — the gate (replicates)

**Activation track.** Incumbent plus survivors, `--split train`, **invoked three separate
times** — three `coder-eval run` commands, **not** `--repeats 3`:

```bash
coder-eval run <suite> -e <experiment> --split train --run-dir <runs>/round<N>-gate-1
coder-eval run <suite> -e <experiment> --split train --run-dir <runs>/round<N>-gate-2
coder-eval run <suite> -e <experiment> --split train --run-dir <runs>/round<N>-gate-3
```

**This is the instruction most likely to be "simplified" into a bug.** Suite rollups are
keyed on `(variant, suite)` only, so `--repeats 3` pools all three replicates into a single
`suite.json` with one confusion matrix — the per-replicate F1 this gate reads would not
exist in that file. Three invocations produce three run directories and three `suite.json`
files. The cost is identical.

Read `metrics["f1.yes"]` per invocation per arm. **Never recompute F1 from the confusion
counts** — the criterion layer owns that arithmetic including its division-by-zero
convention, and a re-derivation will disagree with the gate the run already applied.

Promote only when all of these hold:

- **`min(candidate F1) > max(incumbent F1)`** across the three invocations — the ranges do
  not overlap. A difference smaller than the run-to-run spread cannot clear it.
- **F1 improves, not just recall.** Widening recall while shedding precision is not an
  improvement, it is a different trade.
- **No sibling regression.** For every other `skill_triggered` criterion in the suite, its
  **`recall.yes`** must not drop. A candidate that wins by annexing a sibling's requests has
  moved the failure, not fixed it.

  Read `recall.yes`, not `precision.yes`, and the reason is worth keeping: annexation makes
  the sibling's criterion `expected=yes, observed=no` on that row — a false negative. That
  lowers recall. Precision is `tp/(tp+fp)`, and annexation lowers `tp` while leaving `fp`
  untouched, so on a suite where the sibling never misfires precision reads 1.0 however many
  requests are stolen — right up until the last one, where `tp` and `fp` are both 0 and the
  div-by-zero convention drops it to 0.0. Either way it is blind to the thing you are
  watching for: gating on precision here would be gating on a constant that finally moves
  only when the sibling has already lost everything.
- **Print per-invocation F1 and confusion counts for every arm.** The verdict is never
  reported without the numbers behind it.

Why replicates rather than a fixed threshold: each arm's spread measures the noise floor
for *this* suite at *this* size. A hardcoded "≥ 0.05 F1" would be far too lax on a six-row
suite and needlessly strict on a forty-row one. **Do not introduce one.**

**Be precise about what this bounds, because it is easy to claim more.** The replicate
spread measures agent stochasticity over a *fixed* set of rows. It does not measure
row-sampling variance, and it does not correct for the fact that the survivors were already
chosen on these same train rows in Stage A — so with S survivors each tested independently,
some separation by luck is expected. Stage B bounds run noise. **The test is what bounds
the fit**, and it is why Stage C is not optional. Report the gate as "separated beyond
run-to-run noise on the train rows", never as "proven better".

**Execution track — gate pairwise, with `--repeats 3`, and let the reporter do the
statistics.** Take the single best Stage A survivor against the incumbent as a **two-variant**
experiment:

```bash
coder-eval run <suite> -e <experiment> --split train --repeats 3
```

This is a deliberate departure from the activation gate above, for one reason: the
activation gate compares **F1**, which the pooled `suite.json` cannot report per replicate —
hence three invocations. The execution gate compares per-row **`weighted_score`**, which is
exactly what `paired_comparison` already computes, correctly, over replicates it averages
per row before pairing. So the same `## Paired Comparison` block that is only *corroboration*
on the activation track is the **primary instrument** here, with a mean difference,
a 95% confidence interval, Cohen's *d* and a paired *t*-test — tested code instead of
arithmetic done by hand.

That constrains the shape: it fires only for exactly two variants, so gate one candidate at
a time, in `round<N>-gate.yaml`. With expensive rows that is the right trade anyway — Stage A
already ranked them.

**Read the sign off the header, and never state a direction without it.** The block renders
as `**Paired mean diff (<first declared variant> - <second declared variant>)**`, subtracting
in **variant declaration order** — not incumbent-minus-candidate, and not
better-minus-worse. With `incumbent` declared first, as in the example above, **a candidate
win reads negative**. Quote the header verbatim next to the number, and resolve the direction
from it every time rather than from memory; a reversed reading promotes the arm that lost,
and every subsequent number in the ledger corroborates it.

Promote only when all of these hold:

- **The paired mean difference favours the candidate and its 95% CI excludes zero.**
- **The PREDECLARED primary and its guardrails hold.** Before Stage B runs, name **one**
  primary criterion (or the suite score) plus the specific guardrail criteria allowed to veto
  a win, and record them in the ledger — before the numbers exist, which is what makes it a
  predeclaration rather than a claim. Then evaluate only those. The reason to compare
  per-criterion aggregates at all is unchanged: a candidate that lifts the average while
  breaking a row that used to pass has traded, and on a body edit that trade is usually the
  thing you least want. But scanning *every* per-criterion aggregate post hoc for a
  regression is uncorrected multiple testing in the rejection direction — with enough
  criteria something always looks worse, so noisy criteria veto real wins and *which*
  criterion "regressed" is unstable from round to round.
- **`completion_rate` is equal across arms**, or the difference favours the incumbent. An
  eroded, asymmetric sample produces confident nonsense — a *p*-value computed over rows that
  vanished from one arm is not evidence.
- The skill **actually engaged** on every scored row; otherwise part of the sample measured
  the absence of the thing under test.

Print the paired block verbatim alongside the per-criterion table. A body change is a
behavioural change, and the numbers behind it are the whole argument.

### Stage C — confirm on the test split

Only the best candidate that already passed Stage B, as a **two-variant** experiment
(incumbent + that candidate) at `--split test --repeats 3`. That is `round<N>-confirm.yaml`,
a third file — re-passing the triage file here is the mistake Step 9 describes, and it costs
more while producing no paired block at all.

Here `--repeats` is correct and required. The experiment reporter renders a
`## Paired Comparison` block in `experiment.md` — mean difference, 95% confidence interval,
Cohen's *d*, and a paired-*t* p-value — but it fires **only** for exactly two variants, and
it averages replicates per row *before* pairing, which is precisely what pooling breaks in
`suite.json` and exactly what is wanted here.

**The same sign rule applies, and it is easiest to get wrong here** — this is the number the
promotion is reported on. It is restated rather than cross-referenced because each stage is
read on its own, and a lint sensor requires it in both. The header subtracts in
**variant declaration order**
(`<first declared> - <second declared>`), so with `incumbent` declared first **a candidate
win reads negative**. Quote the header verbatim beside the figure and read the direction off
it, every time.

Report that block verbatim alongside the test F1s, and state its limit honestly: it
pairs per-row `weighted_score` — row-level correctness, an accuracy-flavoured quantity —
**not** F1. F1 remains the promotion metric; the paired block corroborates the direction on
the paired rows, it does not re-test the promotion metric.

Require the F1 direction to reproduce on the test split. Do **not** require replicate separation
there — a test split is usually smaller and the separation usually weaker — and say that,
rather than implying a stronger result than was obtained.
