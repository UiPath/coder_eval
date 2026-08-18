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
| Control arm — execution track, **once per suite** | `3 × M_train` (`6 × M_train` with the incumbent it is paired against) — also buys the execution preflight |
| Stage A — triage | `(N+1) × M_train` |
| Stage A — triage, halved (an abandon point, NOT a saving) | `(N+1) × M_train/2 + ceil((N+1)/2) × M_train` |
| Search loop — one round, rounds 2+ | `1 × M_train` |
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

**Do not double the baseline on the execution track.** There a row is a whole task run, so
doubling it is the most expensive thing in the skill — and the floor it would buy is a floor on
`f1.yes`, which the execution gate never reads. One baseline there. That track gets a floor too,
on its own metric and from data it was already going to pay for; see the second preflight below.

### The search loop — search, not a gate

Round 1 runs the multi-arm Stage A. From round 2 a cheaper thing is available first: a **single
lineage**. One candidate per round, run on the train split alone, accepted if its train score beats
the score the lineage head recorded and reverted otherwise. That is the whole loop, and the table
above prices one round of it at `1 × M_train` — a single arm, because the head's number is read
back from the previous round's record rather than re-measured.

**It bounds nothing.** The comparison is across invocations, unpaired and unbootstrapped, with no
replicates and no correction — precisely the arithmetic the gate exists to distrust. So a search
win is a *hypothesis to gate*, never a result. Nothing here promotes.

**A search accept is not a promotion, and the two artifacts are distinct.** The **lineage head** is
what the search loop works from, advanced by a search accept. The **incumbent** is what has passed
Stage B and Stage C, advanced only by a promotion, and it is what the user is shown a diff
against. Collapsing them would have the procedure recommending edits validated by an unpaired train
comparison alone. A round can therefore accept a search candidate and promote nothing, and the
incumbent must not move when it does.

**Round 1 still runs the multi-arm Stage A**, and rounds 2+ return to it periodically, because the
row matrix and the instance-best front are what a merge candidate is drawn from — a single-arm
round produces no matrix at all.

**And on the activation track the number it compares is not that gate's metric.** The comparison is
a mean over the recorded per-row score vectors, which on a binary activation criterion is
**accuracy** — while the gate compares `f1.yes`. Accuracy credits true negatives and F1 does not,
so an arm that only becomes more conservative can win the search and lose the gate. On the
execution track the two do coincide: per-row `weighted_score` is what the search loop and that
gate both read. Either way the search number narrows and `f1.yes` decides, which is one more
reason nothing here promotes.

Two means from two invocations are also not automatically over the same rows — holes, a halved
Stage A pass, an unpinned sample seed — and every way they diverge favours the candidate. So the
comparison is taken over the rows both arms scored, and a row the head scored and the candidate did
not is a hole rather than a win: the rule the coverage front already applies, applied here.

### Two preflights — price what the suite can see, on each track's own metric

Same machinery both times: run it against **one** arm, split that arm's data into two halves, treat
the halves as two arms, and bootstrap the difference. The true difference is zero by construction,
so the interval's half-width is the smallest difference this suite at this size can resolve — its
noise floor. What differs is the metric, the split axis, and where in the procedure it can be read.

| | activation track | execution track |
| --- | --- | --- |
| metric | `f1.yes` — what its gate compares | `weighted_score` — what its gate compares |
| split axis | invocations (Stage B runs three) | replicates within a row (`--repeats 3`) |
| read it | before any candidate spend | after the control arm, before Stage A |
| costs | the second baseline invocation | nothing — it reads the control run |

**The metric is not interchangeable, and using the wrong one fails quietly.** An `f1.yes` floor on
an outcome suite reads a confidently meaningless `0.000`: the engagement criterion is required to
be 1.0 on every row, so both halves score perfectly and the interval collapses. That number then
prices a gate that never reads F1.

**The positions differ because a floor needs replicated rows, and that is worth stating rather
than leaving to look like an oversight.** The activation preflight genuinely runs before any
candidate spend — a second one-turn baseline is cheap. The execution track has no such option: its
baseline is a single replicate per row, so a floor there is `None` by construction, and the
cheapest replicated data on that track is the control stage. So its preflight is a *post-control,
pre-Stage-A* check. The weaker claim is the honest one, and the hand-back rule still bites where it
matters: Stage A, Stage B and Stage C are all still unspent when it is read, and those are the
stages that multiply by candidate count.

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

Give that invocation its own `--run-dir` and remember where it is: it is also the run directory the
execution preflight above reads, and it is the only replicated data on this track that the
procedure already pays for.

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

**One reading to keep straight once candidates may bundle SCRIPTS.** A candidate whose hypothesis is
"run `scripts/x.py` instead of following six prose steps" has a SHORT body, which superficially
resembles the control. It is not one: the control has no instructions at all, so nothing tells the
agent the script exists, while the candidate's short body is what points at it. The comparison the
control licenses is therefore unchanged — it still asks whether *instructions* do measurable work, and
a bundled script is an instruction with a different delivery mechanism. What does change is that
"short body" stops being a proxy for "no instruction", so read the directory rather than the line
count when deciding which arm is which.

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

**Two fronts are computed over the row matrix, and the project keeps both because they answer
different questions.** The **coverage** front is the arms no other arm dominates on the row vector
— the right rule for *discarding*, since an arm off it was matched or beaten on every row it was
measured on (and beaten on at least one), so it knows nothing the others do not. The **instance-best** front is the arms achieving the highest
score on at least one row — GEPA's definition, and the right rule for *merging*, because it
deliberately retains an arm that owns a single row while being dominated overall, which is exactly
the ingredient a merge is built from and exactly what a coverage rule drops.

Neither set contains the other, and an arm in one and not the other is the finding rather than a
contradiction: on the coverage front alone means never beaten and never a winner; on instance-best
alone means dominated overall yet uniquely good somewhere. Collapsing them into one list would
lose whichever question you were not asking.

#### Successive halving — narrow cheaply, then rank properly

Stage A pays full price for arms that were never going to survive. **Successive halving** runs it
in two passes instead:

1. All `N+1` arms on a **stratified half** of the train rows.
2. Keep the top `ceil((N+1)/2)`, and run *those* on the **full** train split.

**It never saves runs, and what it costs depends on the parity of the arm count — do the
arithmetic rather than assuming it.** `(N+1) × M_train/2 + ceil((N+1)/2) × M_train` against the
flat `(N+1) × M_train`, writing `A = N+1`:

| arms `A` | flat | halved | premium |
| --- | --- | --- | --- |
| 4 | `4 × M_train` | `4 × M_train` | **none** |
| 5 | `5 × M_train` | `5.5 × M_train` | `M_train/2` |
| 6 | `6 × M_train` | `6 × M_train` | **none** |

`ceil(A/2) − A/2` is zero for an even `A` and a half for an odd one, so halving is **free at an
even arm count** and costs half a train split at an odd one. Never a saving either way: pass 2
re-measures the half pass 1 already ran.

**So do it for the abandon point, and note that at an even arm count the option is free.** What
pass 1 buys is a cheap look at every arm before committing to any of them: if nothing separates
from the incumbent on the half, stop there having spent half a Stage A rather than a whole one.
Three or five candidates give `A = 4` or `A = 6` — free — and four give `A = 5`, where the option
costs half a split. If you intend to run pass 2 whatever pass 1 says, and `A` is odd, run the flat
Stage A instead.

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
  moved the failure, not fixed it. The siblings are **derived from the run**, not declared —
  every classification criterion other than the gated one is checked, because a guardrail
  somebody has to remember to arm is one the tool does not have. The block reports an
  **annexation rate** beside each sibling's recall: of that sibling's true-`yes` rows, the
  fraction this candidate turned into `no` that the incumbent did not. That is a reading, and
  the pass/fail is still the recall drop alone.

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
for *this* suite at *this* size. A hardcoded "≥ 0.1 F1" would be far too lax on a six-row
suite and needlessly strict on a forty-row one. **Do not introduce one.** (The figure here is
deliberately not alpha's: an F1 threshold and a significance level are different quantities, and
this file states one of them symbolically for exactly that reason.)

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

##### When the corrected threshold sits below what the suite can express

The correction tightens the bar as the family grows, and a small suite has a **floor** on how
small a p it can produce at all. Both are real, and they can cross.

The floor is a counting fact, not a property of the bootstrap's precision. A resample that
happens to draw **none** of the rows the two arms disagree on hands both arms a byte-identical
pool, so that draw's difference is exactly zero and it counts in *both* tails. With `M` paired
rows of which `R` differ, that happens often enough to bound the smallest p the suite can be
expected to report — and on a small suite with few disagreeing rows the bound is coarse. The tool
computes it, reports it beside the estimator's own floor, and compares it against the Holm
threshold for this candidate's rank.

**Where the floor exceeds that threshold, the tool REFUSES rather than reporting a negative
result**, and the rendered block says `CANNOT SEPARATE AT THIS SIZE`. Read that as what it is: no
candidate could have promoted here, however good it was. It is a statement about the suite, and
reporting it as "not promoted" would be a claim about the candidates that the data cannot support.

**A pair that never scored comparable rows is refused too, and it is a different kind of
refusal.** Before any bootstrap, the activation gate runs a row-selection preflight over each run
directory. It asks two questions, and a failure of either says `NOT A RESULT` with no statistic:

1. *Did the two arms record different `--split` values?* Arms that selected different row sets
   never scored the same rows, so their difference is not an effect. Remedy: re-run both arms
   under one `--split`.
2. *Does each `run.json` describe the tree it sits on?* `run.json` is written per INVOCATION while
   the tree beneath it is APPEND-ONLY, so re-using a `--run-dir` leaves an earlier call's rows —
   or, with a smaller `--repeats`, an earlier call's replicates — on disk while `row_selection` is
   rewritten to describe only the latest call. Question 1 then passes honestly and the gate pools
   both selections into each arm. Because both arms live under the same run dir the contamination
   is symmetric, so no `rows_excluded` bump and no unpaired-rows note appears. Remedy: re-run both
   arms into a fresh `--run-dir`.

Tell both apart from the sizing refusal below by the p: these never have one, because no
comparison was made. Missing provenance — a run directory written before the field existed, or one
recording no `task_results` — is a NOTE, not a refusal: old runs stay gatable, but the fault
cannot be ruled out for them. The execution track has no cross-split refusal: it takes one run
directory holding both arms, so they share one split by construction.

**Read which refusal you got, because there are two and their remedies are opposite.** The usual
one names the largest family size that could still promote, so the honest options are visible:
gate fewer survivors, or **add rows the two arms actually disagree on**. That second lever is
narrower than it looks, and the block states it as a number because getting it wrong costs money:
holding the discordant count fixed and adding rows makes the floor **worse**, not better, so a
user who buys agreeing rows after a refusal ends up further from promoting than before. The block
names the discordant count this suite has and the one that would clear its bar; act on that pair,
not on the row total. More rounds will not help either — the floor falls out of the row count and
the discordance rate, not out of luck, so re-running the same suite reproduces it.

The other says the two arms produced **identical labels on every scored row**. That is a finding
about the candidate rather than the suite: with nothing discordant the floor is 1.0 at any suite
size, so adding rows cannot change it. Check the candidate actually differs from the incumbent and
that each arm mounted the snapshot you think it did.

And because
the floor bounds the p's *expectation* rather than every draw, a refused verdict is forced to
`promoted = False`: a realized p that happened to dip below the floor is Monte-Carlo noise, not
evidence, and promoting on it would make the verdict depend on the seed.

Carry no number here. The floor is data-dependent and the resample count is owned by the module.

##### The execution track refuses too, on different conditions

The paired *t* is continuous, so it has no discreteness floor and none of the above applies to it.
It has its own degenerate states, and the tool refuses on them under a **different headline —
`NOT A RESULT`** — so that a ledger read back weeks later cannot confuse the two. Do not carry a
remedy across: nothing on this list is fixed by gating a smaller family, and nothing on the
activation list is fixed by adding replicates.

- **There is no comparison to make.** Both arms named the same variant; no experiment file, or one
  that could not be read or parsed; an experiment declaring other than exactly two variants; either
  variant id absent from the one it declares. Each of these is a wiring fault with its own message,
  and most of them used to render as `NOT PROMOTED` — an ordinary negative result about a candidate
  that was never actually compared. (Two did already refuse, by accident rather than by design: a
  mistyped variant id also empties that arm, and the zero-row cause fired first.)
- **An arm loaded ZERO rows.** This track's statistic comes from `experiment.json` while every
  guardrail and integrity check comes from the on-disk row tree, so a valid experiment file beside
  a wrong run directory computes a perfectly good p over rows that are not there. Fix the path.
- **Fewer than two rows paired.** No interval exists, so there is nothing to weigh; the row counts
  stay on the block so an eroded sample is visible as `paired 1 · excluded 2` rather than being
  flattened into the message.
- **The paired differences carry ZERO variance** — the two arms differed by an identical amount on
  every row. For a non-zero amount the *t* reports `p = 0.0000` over a zero-width interval, so Holm
  rejects, the difference favours the candidate and the interval excludes zero, all at once, on a
  sample that separated nothing. Add rows the arms disagree on, or add replicates. If the amount
  was exactly **zero** the arithmetic is different — `p = 1.0000`, nothing rejects — and so is the
  finding: it is about the candidate, which behaved identically to the incumbent, and no number of
  rows changes that.
- **The difference is below the suite's own minimum detectable effect *and the interval still
  excludes zero*.** The MDE is the half-width of a null comparison — the incumbent's replicates
  split against each other, where the true difference is zero by construction — so it is this
  suite's run-to-run noise. A confident claim about an effect under that floor is not a result.
  Lower the floor with more replicates or more rows, or find rows where the effect is larger.
  **The second half of that condition is load-bearing.** A candidate that simply does not help is
  also below the floor — under a true null nearly every one is — but its interval CONTAINS zero,
  and that is the ordinary negative result this whole method is usually looking for. Refusing those
  too would retire `NOT PROMOTED` almost entirely and send you to buy replicates for a candidate
  whose only problem is that it does not work. Measured over 40 true-null candidates: 37 read as
  `NOT PROMOTED`, 2 refused, 1 promoted.

That last refusal needs a floor to compare against, and sometimes there is not one. A null split
reduces to **exactly 0.000** whenever every row's replicates agreed — a deterministic suite, or one
whose rows all failed the same way — and at two replicates on homogeneous rows that is the ordinary
outcome, not an exotic one. The block says so instead of skipping the check silently, because
"minimum detectable effect: 0.000" printed alone reads as *this suite can resolve anything*, which
is the exact opposite of what an unpriced floor means. Raise `--repeats` and check the rows ran.

One more state is reported but **not** refused, and the difference matters. Where the interval's
half-width is below the MDE while the difference itself is above it, the block says so and lets the
decision stand. The *t*'s interval comes from the between-row spread of the differences, which is
tiny whenever the arms differ by a similar amount on every row, while the MDE measures within-row
noise the *t* never sees — so a real, large, consistent win reports an absurdly small p. What is
wrong there is the reported precision, not the verdict: refusing it would throw away genuine wins.
Read the difference against the floor, and do not quote such a p as confidence.

#### Cost and latency guardrails

Both tracks carry them, both tracks **gate** on them, and they are **derived from the measured
spread, not from a percentage threshold**. The same paired cluster bootstrap runs over each row's
cost and each row's duration, and a guardrail fails only when the *optimistic* end of that interval
is still a material increase — so an arm has to be reliably more expensive, not merely more
expensive in this sample.

**Do not write a percentage into this file.** A fixed tolerance is precisely what the measured
run-to-run spread rules out: at the per-row variability these suites actually show, a
tight-sounding rule fires on noise a meaningful fraction of the time, which is the same
"noisy criteria veto real wins" failure the predeclaration rule below exists to prevent. The
tool owns the one materiality floor it uses, and quoting its value here would be a second
declaration of it that goes stale the moment the tool's own value changes.

The block reports **median cost per row** and **median latency per row** as the level, over the
rows the F1 comparison actually used. Read what fires it carefully, because the two numbers are
not the same one. The reported LEVEL is the incumbent's *median* cost per row, because a median is
the robust thing to read on a right-skewed distribution. What the guardrail FIRES on is a
different pair: the interval is on the difference of *means*, and the floor it is compared against
is scaled by the incumbent's *mean* — matching units, since scaling a mean-difference by a median
measured a uniform 10% increase as a breach of a much larger floor. The median is the level; the
mean is what is being tested. A guardrail with nothing to measure — no turn reported a
cost — passes with a stated reason rather than silently, because a missing measurement must never
read as a pass on the merits.

**Cost is a veto here and an objective elsewhere, and the two must not be confused.** The
guardrail above **gates**, on both tracks and in the tool rather than in this file: a candidate
that materially increases what a row costs does not promote, whatever its F1 did, and the gate's
own `promoted` field carries that veto so a caller reading it cannot ship what the rendered block
calls BLOCKED. Stage A additionally renders a quality × cost front, which is
**advisory**: it is the second axis of a shortlist, it shows which trades exist, and it decides
nothing. Adding the second did not weaken the first — the promote-only-when list below is
unchanged, and a cheaper arm on that front is a trade to offer the user, **never a promotion** the
tool makes. Read that front with the arms you are actually choosing between: any arm that is cheap
because it does less — an emptied-body control among them, if you put one in the comparison — sits
there by construction, since nothing dominates an arm nobody is trying to beat on cost.

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

**The gate resolves the sign; a reader never should.** `execution_gate` knows which arm is the
incumbent, so the difference it reports is always *candidate minus incumbent* and its interval
bounds are ordered to match — whichever order the experiment file declared its variants in. That
single behaviour is why the gate exists: a reversed reading promotes the arm that lost, and every
subsequent number in the ledger corroborates it.

The warning still applies to the **reporter's own** block, which a user may read directly. It
renders as `**Paired mean diff (<first declared variant> - <second declared variant>)**`,
subtracting in **variant declaration order** — not incumbent-minus-candidate, and not
better-minus-worse. With `incumbent` declared first, **a candidate win reads negative** there.
Quote that header verbatim next to any number taken from it, or take the number from the gate.

**Holm applies here too, and the family lives across run directories.** Each execution gate is its
own two-variant invocation with its own run dir, so a round gating three candidates has three of
them — but they are still three tests of the same incumbent on the same train rows, which is a
family. Correct it **once**, with the same step-down the activation track uses, by passing every
verdict through `holm_promote_execution` in a single call. A round gating one candidate passes a
family of one, which is the same call and costs nothing.

Promote only when all of these hold. Each bullet says whether the rendered block reports it — most
do, and the one that does not is the one you still have to check yourself:

- **The paired mean difference favours the candidate and its 95% CI excludes zero.** The block
  reports this, with the sign already resolved as candidate minus incumbent.
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

  **This is the one bullet the block does NOT report.** `execution_gate` carries no per-criterion
  aggregates, precisely because which criteria to read is a predeclaration you made before the
  numbers existed — the tool cannot know it, and a tool that scanned them all would be doing the
  uncorrected multiple testing this bullet forbids. Read your predeclared criteria out of
  `suite.json` yourself, and quote the predeclaration from the ledger beside them.
- **`completion_rate` is equal across arms**, or the difference favours the incumbent. An
  eroded, asymmetric sample produces confident nonsense — a *p*-value computed over rows that
  vanished from one arm is not evidence. The block reports this as an integrity check, derived
  from the rows themselves rather than from `suite.json`'s `criterion_aggregates` — that list is
  *filtered*, so a position in it is not a position in `success_criteria`.
- **Cost and latency have not materially regressed**, by the same bootstrap-derived guardrails
  described above; the block reports both. They matter more here than on the activation track, not
  less: an outcome row is a whole task run, so a body edit that sends the agent down a longer path
  moves real money.
- The skill **actually engaged** on every scored row; otherwise part of the sample measured
  the absence of the thing under test. The block reports this too, as the engagement
  `recall.yes` integrity check.

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

#### The acceptance criterion, per track

**Stage C has a computed verdict now, and the criterion is a different quantity on each track.**
The paragraph above is the ACTIVATION track's, and stating only that one instructed an execution
reader to check a number their track never produces — the paired block IS their result there, not a
corroboration of one.

- **Activation track.** The metric is `f1.yes`. Require its DIRECTION to reproduce on the test
  split. Do **not** require replicate separation there — a test split is usually smaller and the
  separation usually weaker — and say that, rather than implying a stronger result than was
  obtained.
- **Execution track.** The metric is per-row `weighted_score`, and it is the same quantity Stage B
  decided on, so the criterion is stronger and simpler: require the paired mean difference to keep
  its SIGN and to stay within the confirm split's own minimum detectable effect of the train
  figure. That MDE is the margin because it is what the confirm split can actually resolve; a
  shortfall larger than it is a real shrinkage rather than noise.

Both are computed by `confirm_gate` (activation) / `confirm_gate_execution` (execution), which
classify the train→test delta as **REPRODUCED**, **SHRANK**, **REVERSED** or **UNDECIDED** through
one shared rule. Print the returned block; do not re-derive the comparison by eye. A REVERSED
outcome is a headline: the effect the round was built on points the other way on held-out rows.

UNDECIDED is a common and honest answer, not a failure of the tooling — most often the confirm
split's floor could not be priced, which needs a higher `--repeats` on the confirm run rather than
a different reading of the same numbers.
