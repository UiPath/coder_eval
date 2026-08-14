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
| Step 6 baseline | `M_train`, or `2 × M_train` on the activation track |
| Control arm — execution track, **once per suite** | `3 × M_train` (`6 × M_train` with the incumbent it is paired against) |
| Stage A — triage | `(N+1) × M_train` |
| Stage A — triage, halved (an abandon point, NOT a saving) | `(N+1) × M_train/2 + ceil((N+1)/2) × M_train` |
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

**On the activation track the baseline is two invocations, not one, and the second one is what
buys the preflight below.** A single run measures a level; two measure the spread, and the
spread is the only thing that can tell you whether the gain you are hoping for is even visible
on this suite. It is the cheapest stage in the table, and it is the one that can stop you
spending the rest of it.

**Not on the execution track.** There a row is a whole task run, so doubling the baseline is the
most expensive thing in the skill — and the floor it would buy is a floor on `f1.yes`, which the
execution gate never reads. One baseline there.

### Before Stage A — price what the suite can see

**State the minimum detectable effect before spending.** This is an activation-track preflight —
it is computed on `f1.yes`, so it prices the activation gate and nothing else. Run the same
machinery against the incumbent alone: split its invocations into two halves, treat them as two arms, and bootstrap
the difference in `f1.yes` between them. The true difference there is zero by construction, so
the interval's half-width is the smallest F1 difference this suite at this size can resolve —
its noise floor.

Then say the quiet part out loud, before any money is spent: **if the gain you are hypothesising
is smaller than the minimum detectable effect, this suite cannot see it.** Hand back and say so —
the answer is more rows, not more rounds. Running a stage that cannot resolve the effect it is
looking for produces a non-result that reads exactly like a real one.

The second baseline invocation is what it costs, and the table above prices it. Nothing after
that is extra: at Stage B the MDE is recomputed from gate run directories that exist anyway, and
the gate reports it beside every verdict, so a difference smaller than the floor is flagged even when the
interval excludes zero. With only one invocation of the incumbent there is no null comparison and
the tool reports no MDE at all rather than inventing one — which is itself the signal that the
preflight did not happen.

**Every stage runs the suite through the experiment.** The suite is the positional
argument; the experiment carrying the arms is passed with `-e`. Passing the experiment file
positionally instead would treat it as a task, which resolves to a skipped task and a green
run of zero rows.

### Before optimizing a body, establish it is worth optimizing

**Execution track only, and once per suite rather than once per round.** Add a **control arm**
beside the incumbent: a snapshot identical in every respect except that the target skill's
`SKILL.md` **body is emptied**. Then ask the question every round after this one assumes the
answer to — *does the body do anything at all on this suite?*

**Empty the body; do not remove the skill.** Removing it changes the listing the model chooses
from, which changes activation, which means the control differs from the incumbent in two ways at
once and attributes neither. Keeping the frontmatter holds the listing constant and varies only
the instructions under test — which is the whole comparison. It also keeps `skill_triggered`
observing engagement normally, so a control row that scores badly is visibly a *body* failure
rather than a skill that never ran.

Gate it with the same machinery Stage B uses on this track — incumbent and control as a
two-variant experiment at `--repeats 3`, which is why the table prices the pair at `6 × M_train`
and the control's own share at `3 × M_train` — and apply the hard stop:

> **If the incumbent does not beat the control, with a confidence interval excluding zero, stop.**
> The body is not doing measurable work on this suite. Optimizing its wording is optimizing
> something the measurement cannot see, and every round after this one would be noise dressed as
> a result. Fix the skill's premise, or fix the suite, rather than its phrasing.

Two readings that are correct rather than bugs. A skill whose body is its entire value scores the
control near the floor — that is the finding, and it says the round is worth running. And on a
suite that also grades engagement, the control scores engagement `1.0` and the outcome criteria
near the floor: exactly right, and precisely the separation the emptied-body design buys.

Record the control's numbers once and reuse them. It is a property of the suite and the skill, not
of the round, so re-running it every round is pure spend.

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

#### Successive halving — narrow cheaply, then rank properly

Stage A pays full price for arms that were never going to survive. **Successive halving** runs it
in two passes instead:

1. All `N+1` arms on a **stratified half** of the train rows.
2. Keep the top `ceil((N+1)/2)`, and run *those* on the **full** train split.

**It does not save runs, and the arithmetic is worth doing rather than assuming.**
`(N+1) × M_train/2 + ceil((N+1)/2) × M_train` against the flat `(N+1) × M_train` is
`2.5 × M_train + 3 × M_train` versus `5 × M_train` at five arms — a **premium of half a train
split**, and it is exactly half a train split at every arm count, because
`ceil(A/2) ≥ A/2` for all `A`. Pass 2 re-measures the half that pass 1 already ran, and that
re-measurement is what eats the saving.

**So do it for the abandon point, not for the price.** What pass 1 buys is a cheap look at every
arm before committing to any of them: if nothing separates from the incumbent on the half, you can
stop the round there and have spent half a Stage A rather than a whole one. That is a real option
and it is often the one worth having. If you already intend to run pass 2 whatever pass 1 says,
halving costs you and buys nothing — run the flat Stage A.

**The version that would save needs a mechanism this CLI does not have.** Pass 2 would have to
cover only the rows pass 1 did *not*, leaving each survivor a full train split pooled across two
run directories (`arm_row_scores` already takes a list of run dirs for exactly that reason). That
is `A × M_train/2 + ceil(A/2) × M_train/2` — a fifth saved at five arms, approaching a quarter — but
`--sample-per-stratum` draws a sample, it cannot draw a sample's complement. Written down so
nobody re-derives the saving from the shape of the procedure and believes it.

**The noise caveat is the other half of the trade, so state it before spending.** A ranking on half
the rows is a *noisier* ranking, and it can discard an arm that would have won on the full split.
The row matrix and Pareto front the skill prints at Stage A are how you check whether it did — an
arm dropped in the first pass that was winning rows no survivor won is exactly what that view makes
visible, and it is why it ships before this does. For a repository that wants a guarantee rather
than a check, **SySR** (paired successive rejects for best-arm identification) is the principled
version; it is **not** implemented here.

**Too small to halve?** Below `check-skill`'s un-doubled minimum on either polarity, skip halving
and run the full train split. Do not halve a six-row suite into three — the first pass would be
ranking on noise and discarding real arms on it.

**Stratify on the same field the suite already uses** (`expected_skill` by default), so both halves
carry both polarities. It is the rule Step 5 applies to the train/test split, applied again one
level down. The mechanism is `--sample-per-stratum`.

**Why that is arm-safe, which is not the reason intuition supplies.** Every arm in one invocation
sees the byte-identical row set **by construction, seed or no seed**: the dataset expander runs
once per task file, and the variant fan-out runs over its output. There is no per-arm sampling to
go out of step. "Pin the seed so the arms match" is plausible, wrong, and would leave you guarding
something that cannot happen.

**The real hazard is the mirror image, and it lands on Stage B.** Sampling is nondeterministic
**across invocations** when `dataset.sample_seed` is unset. Stage B runs three separate
invocations, and the gate pairs rows **by row id across them** — so a suite carrying
`dataset.sample_per_stratum` would draw three different row sets and the gate would find few or no
rows in common. It does not error. It reports a shrunken `rows_paired` and an interval that means
nothing. So: **pin `dataset.sample_seed` on any suite that samples at all**, and understand that
the rule is about Stage B rather than about halving. No shipped suite samples today, which is
exactly why this is worth writing down before one does.

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
`suite.json` with one confusion matrix — the per-replicate F1 you read and quote would not
exist in that file. Three invocations produce three run directories and three `suite.json`
files. The cost is identical.

**Be precise about what the gate needs, because the tempting simplification is now half-right
and that is worse than wrong.** The gate reads per-row `task.json`, not `suite.json`, and
`--repeats 3` does write one per replicate — so the *interval itself* could be computed from a
single run directory. Two things could not:

- **The minimum detectable effect**, which is a comparison between invocations. One run
  directory offers no null comparison, so the tool reports no floor and the round is unpriced.
- **The per-invocation diagnostic.** Range non-overlap is computed per run directory; with one,
  it collapses to comparing a pooled number against itself and reports something meaningless.

So the three invocations stay, and the replicates within each row are the *within-cluster* data
the resampling pools. What the rollup pooling still costs you is the human-readable per-arm F1
per invocation — the number the ledger quotes and Stage A ranks on.

Read `metrics["f1.yes"]` per invocation per arm **for the ledger and for Stage A's ranking** —
the gate computes its own from the same criterion layer, so this is corroboration, not input.
**Never recompute F1 from the confusion counts** — the criterion layer owns that arithmetic
including its division-by-zero convention, and a re-derivation will disagree with the gate the
run already applied. The gate satisfies that rule by construction: it does not re-derive
anything, it *calls* the criterion layer's own routine on every resample.

#### The statistic: a paired cluster bootstrap over rows

Both arms ran the **same rows**. The old rule threw that away by comparing three separate
numbers per arm, and at eight to twelve rows per polarity it had very little power. The
replacement keeps the pairing and resamples **rows, not observations**:

1. Draw `M` rows with replacement from the `M` train rows — *the same drawn rows for both arms*.
2. For each drawn row, take **all** of its replicates, for the incumbent and for the candidate.
3. Recompute each arm's `f1.yes` over its pooled resampled pairs, through the criterion layer.
4. Record `candidate − incumbent`.

The draws give the interval, at the resample count and confidence level the rendered block
reports — the tool owns both, so read them off the block rather than from here. **Promote when
that confidence interval excludes zero AND the Holm-corrected test below rejects.**

Both, not either, and the order matters: Holm is the stricter of the two on any family larger
than one, so an interval that excludes zero is **necessary and not sufficient**. A candidate
whose raw interval clears zero can still fail the corrected test — that is the correction doing
its job, not a contradiction to argue around.

Rows, not observations, because replicates within a row are not independent — they are the same
request asked again. Resampling them individually would understate the interval and manufacture
separation. A row scored on only one arm is dropped from both and counted: an errored or
timed-out row produces no criterion result, and comparing arms over different row sets favours
whichever arm failed to produce one.

**Range non-overlap is retained as a reported diagnostic, not as the gate.** `min(candidate F1)
> max(incumbent F1)` is still printed beside the interval, because it is a useful sanity check on
your intuition. It is not the decision, and restoring it as one would reintroduce exactly the
low-power rule this replaced.

Promote only when all of these hold:

- **The confidence interval of the paired difference excludes zero, and the Holm-corrected
  test rejects.** The tool applies both; the interval is the effect size you report, the
  corrected p-value is what decides.
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
- **Cost and latency have not materially regressed.** A description edit that doubles what a
  row costs for two points of F1 is a trade, not a win, and the reader of the ledger is
  entitled to know it was checked. See the guardrails below.
- **Print the interval, the p-value, the minimum detectable effect and the diagnostic for every
  arm.** The verdict is never reported without the numbers behind it.

Why replicates rather than a fixed threshold: each arm's spread measures the noise floor
for *this* suite at *this* size. A hardcoded "≥ 0.05 F1" would be far too lax on a six-row
suite and needlessly strict on a forty-row one. **Do not introduce one.**

#### The Holm correction, which is a property of the FAMILY

With `S` survivors each gated against the same incumbent on the same rows, the family-wise error
rate inflates: test enough candidates and one of them separates by luck. **Holm** fixes it —
order the `S` gates by p-value ascending and reject the `i`-th only while
`p_(i) ≤ alpha / (S − i + 1)` *and* every earlier one was rejected.

This replaces the caveat the old rule could only warn about and never handle.

**It cannot be applied inside a single gate, and the wrong version is indistinguishable from the
right one until someone checks the arithmetic.** Gate each survivor first, then pass **all** the
survivors' verdicts through one `holm_promote` call. Correcting per candidate as you go is not Holm:
with a family of one the step-down degenerates to plain `p ≤ alpha`, and "scaling alpha by the
survivor count" at each gate is Bonferroni — strictly more conservative, and not the test you
said you ran. A single-candidate round is the same call with a family of one.

#### Cost and latency guardrails

Both tracks carry them, and they are **derived from the measured spread, not from a percentage
threshold**. The same paired cluster bootstrap runs over each row's cost and each row's duration,
and a guardrail fails only when the *optimistic* end of that interval is still a material
increase — so an arm has to be reliably more expensive, not merely more expensive in this sample.

**Do not write a percentage into this file.** A fixed tolerance is precisely what the measured
run-to-run spread rules out: at the per-row variability these suites actually show, a
tight-sounding rule fires on noise a meaningful fraction of the time, which is the same
"noisy criteria veto real wins" failure the predeclaration rule below exists to prevent. The
tool owns the one materiality floor it uses, and quoting its value here would be a second
declaration of it that goes stale the moment the tool's own value changes.

The block reports **median cost per row** and **median latency per row** as the level, over the
rows the F1 comparison actually used. Read what fires it carefully, because the two numbers are
not the same one: the interval is on the *mean* difference between arms, and the floor it is
compared against is scaled by the incumbent's *median*. The median is the level; the interval is
the evidence. A guardrail with nothing to measure — no turn reported a
cost — passes with a stated reason rather than silently, because a missing measurement must never
read as a pass on the merits.

**Be precise about what this bounds, because it is easy to claim more.** The interval now bounds
*row-sampling variation and run noise together* — resampling rows is what adds the first, and
pooling each drawn row's replicates is what keeps the second. What it still does not do is
correct for the survivors having been chosen on these same train rows in Stage A. Holm bounds the
multiplicity across the survivors tested here; it does not undo the selection that produced them.
Stage B bounds noise and multiplicity. **The test is what bounds the fit**, and it is why Stage C
is not optional. Report the gate as "separated beyond run-to-run and row-sampling noise on the
train rows", never as "proven better".

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
- **Cost and latency have not materially regressed**, by the same bootstrap-derived guardrails
  described above. They matter more here than on the activation track, not less: an outcome row
  is a whole task run, so a body edit that sends the agent down a longer path moves real money.
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
