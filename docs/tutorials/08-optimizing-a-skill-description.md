---
description: >-
  Measure whether a Claude Code skill's description can be improved — build an
  activation suite, split it into train and test rows, A/B candidate rewrites
  as experiment variants, and promote only what survives rows it was never trained
  on.
---

# Tutorial 08 — Optimizing a Skill Description

`check-skill` tells you whether a skill triggers. This tutorial covers the next question:
**can its description be made to trigger better, and how would you know?**

The honest answer is usually "measure first, and often the answer is don't change it." This
walkthrough is a real run against a real skill in this repository, reported with the numbers
it actually produced — including the part where the first attempt measured nothing at all.

> **This page walks the activation track.** `/coder-eval:optimize-skill` has two:
> **activation** — the frontmatter `description`, measured with an activation suite, asking
> *does the skill fire when it should?* — and **execution**, which improves the skill
> **body** instead, measured against an outcome suite with real success criteria, asking
> *having fired, does it do the job?*
>
> They are different instruments for different failures: `skill_triggered` scores engagement
> and is completely blind to the quality of the work that follows. If your skill fires
> reliably and then does the wrong thing, execution is the track you want.

**What you will do:** build an activation suite for `lint-tasks`, split it into train and
test rows, baseline it, read the confusion matrix, and decide whether to spend anything.
For `lint-tasks` the answer turns out to be no — about 20 agent runs settle it. The suite
then points at a sibling that *does* have headroom, `analyze`, and the full three-stage A/B
runs against that: **407 further runs** — the sum of the per-stage counts in the headings
below, three Stage C attempts included — ending in a promotion that survives the test.

All of it on Sonnet. Never Opus for a suite this size.

**Prerequisites:** the `coder-eval` CLI, the plugin installed, and credentials for the
`claude-code` agent. Start with [Your first evaluation](01-first-evaluation.md) if you have
not run anything yet.

## Why this skill

`lint-tasks` was picked for four reasons, and they are worth stealing as selection criteria:

1. **It is model-invokable.** Its description actually enters the activation decision. A
   skill with `disable-model-invocation: true` — here `init`, `ci`, and `optimize-skill`
   itself — can never be optimized this way, because its description never competes for
   anything — so `optimize-skill` closes the **activation** track for such a skill and
   routes it to the execution track instead, where the body is still measurable. (To
   exercise one in a sandbox, the task's `initial_prompt` must invoke it by slash command;
   asking in prose does not reach it.)
2. **It has a real boundary dispute with a sibling.** `lint-tasks` reviews existing tasks;
   `task` authors new ones. *"Are my evals any good?"* and *"add a task for X"* genuinely
   misroute between them.
3. **Its description had just been trimmed** by 66 characters to fit the plugin's shared
   listing budget. That trim was an unmeasured change to activation behaviour. This turns it
   into a measured one.
4. **It supplies its own distractors.** The description mentions auditing and gaps, so
   *"audit my dependencies"* is a natural false-positive probe.

## Part 1 — A ceiling result, and when to stop (`lint-tasks`)

### Step 1 — Build the suite

Do not hand-author it. Run the sibling skill:

```
/coder-eval:check-skill lint-tasks
```

That produces a task YAML plus a JSONL row file. The committed suite has **28 rows**,
counted by `expected_skill` — the field that actually decides a row's polarity:

| Kind | `expected_skill` | Total | train | test |
| --- | --- | --- | --- | --- |
| Positive | `lint-tasks` | 8 | 5 | 3 |
| Distractor | `""` | 9 | 6 | 3 |
| Sibling-owned | `task` | 3 | 2 | 1 |
| Sibling-owned | `analyze` | 8 | 4 | 4 |

> **The table describes the file as committed; Part 1's runs did not use it.** Part 1 was
> run against an earlier 14-train-row revision, and the `analyze` rows were added later, in
> Part 2, before the file was committed. Every result block on this page names the revision
> it was computed at, which is what reconciles Step 5's `analyze` recall of 0.000 (one row,
> unlucky) with Step 6's two `analyze` train rows sitting at a stable 0.500.
>
> Re-running Part 1 today gives different numbers for a second and more interesting reason:
> the description Part 2 promotes is already committed, so a re-run measures the *improved*
> skill. [What you get running Part 1 today](#what-you-get-running-part-1-today)
> has the measured comparison.

**Label sibling rows by what should *fire*, not by whose territory it is.** Two rows here
ask for project setup — work that belongs to `init`, which sets
`disable-model-invocation: true` and therefore *cannot* be engaged by the model. Labelling
them `expected_skill: "init"` would assert something unsatisfiable by construction: those
rows could never pass, the suite could never go green, and `init`'s recall would be pinned
at 0.0 while telling you nothing. They are labelled `""` instead, which asks the question
that actually has an answer — *does some other skill wrongly claim this?*

**Sizing, and why this suite is at the low end.** `check-skill` asks for 8–12 rows of each
polarity. A split **halves each side**, so a suite you intend to optimize wants roughly
16–24 of each. This one is smaller than that, deliberately, so the tutorial stays affordable
— and you will see the cost of that in the result.

### Step 2 — Label the splits

Add a `split` to every row and point the dataset at the field:

```yaml
dataset:
  paths:
    - "lint-tasks-activation-rows.jsonl"
  split_field: "split"
```

```jsonl
{"id": "pos-1", "expected_skill": "lint-tasks", "split": "train",    "prompt": "..."}
{"id": "pos-5", "expected_skill": "lint-tasks", "split": "test", "prompt": "Are my evals any good?"}
```

Then select one at run time with `--split train` or `--split test`. The filter runs
**before** any sampling, so `--split train --sample 8` is always drawn from train rows alone.

Keep both polarities on both sides. A test of only positives measures recall and calls it
a result.

### Step 3 — Make the skill reachable, and check that it worked

This is the step that decides whether anything downstream means anything, so it gets a
section of its own.

```yaml
agent:
  type: "claude-code"
  plugins:
    - type: "local"
      path: "$SKILL_SOURCE_PATH"
  setting_sources: []
```

**`path` must be a plugin root** — a directory holding a `skills/` subdirectory, so the
skill sits at `<path>/skills/<name>/SKILL.md`. A `.claude-plugin/plugin.json` is optional;
without one the namespace defaults to the directory's own name.

The intuitive path is the wrong one. For `.claude/skills/my-skill/SKILL.md` the root is
**`.claude`**, not `.claude/skills`.

```bash
export SKILL_SOURCE_PATH="$(pwd)/plugins/coder-eval"   # the plugin root
```

> **The first baseline for this tutorial scored recall 0.0.** It pointed one level too deep,
> at the bare skills directory. Nothing loaded, no `Skill` tool was ever offered, and every
> positive row failed. On an earlier, smaller draft of this suite — 10 train rows rather than
> the 14 below — that produced:
>
> ```
> lint-tasks   recall.yes=0.000 precision.yes=0.000 f1.yes=0.000
>              confusion: [('no','no',4), ('yes','no',3)]
> ```
>
> Note the confusion matrix accounts for only 7 of the 10 rows; the missing 3 are the next
> section.
>
> A wrong path is only a **warning**, never an error. The run completes, the report renders,
> and the number it produces is indistinguishable from a skill whose description is hopeless.
> In a one-shot check that misleads you once; in an optimization loop it poisons everything —
> every candidate scores zero, nothing separates from the incumbent, and you conclude your
> rewrites are all bad when your wiring is broken.
>
> **So the first thing to check is never the description. It is whether recall is non-zero
> at all.**

Point it at the plugin root and the same suite goes to `recall.yes=1.000`. Nothing about the
descriptions changed.

### Step 4 — Cap the run, or pay for exploration

Activation is decided in the first assistant turn. Without caps the agent explores a sandbox
that deliberately contains no eval files, and rows time out after five minutes each:

```
pos-1  ERROR  Agent turn timed out after 300s (iteration 1)
```

A timed-out row is **excluded from the confusion matrix**, not scored — so the metrics are
computed over fewer rows than the dataset holds. In that first baseline 3 of 10 rows
vanished this way, which is why its matrix summed to 7.

The rollup does tell you, if you look. `suite.json` carries `rows_total`, `rows_excluded`
and a `completion_rate` metric, and `suite.md` spells it out:

```
**Rows**: 14 total — 13 passed, 1 failed, 0 errored
_Denominator: 14/14 rows (0 excluded)_
```

Because `completion_rate` is an ordinary metric, you can gate on it and stop trusting
eroded runs by hand:

```yaml
suite_thresholds:
  completion_rate: 1.0
```

```yaml
run_limits:
  max_turns: 2
  turn_timeout: 120
  task_timeout: 300
```

That buys signal, not just cost.

### Step 5 — Baseline on the train split

```bash
coder-eval run tasks/skills/lint-tasks-activation.yaml \
  --split train -D run_limits.stop_early=false
```

`stop_early: false` is insurance: if a suite arms early stop, a pass-stop can end a run
before a later sibling misfire is observable, and authoritative precision needs the full
trajectory. A stock `check-skill` suite arms nothing, so on that suite this flag changes
nothing.

**Check the resolved row count.** A mistyped split (`--split holdou`) now aborts the run with an
error naming the splits that exist, so it cannot pass silently — but a *partial* row loss still
can, and only the count shows it.

Result (**14 train rows — the revision these runs used**; the committed file has 17):

| Skill | recall.yes | precision.yes | f1.yes |
| --- | --- | --- | --- |
| `lint-tasks` | 1.000 | 1.000 | **1.000** |
| `task` | 1.000 | 1.000 | 1.000 |
| `analyze` | 0.000 | 0.000 | 0.000 |

### Step 6 — Read it, and decide whether to spend anything

**The target skill is at ceiling.** `lint-tasks` caught all five positives, including
*"My evals are a mess. Can you go through them and tell me what to fix?"*, and stayed off all
nine negatives, including *"Audit my dependencies for known vulnerabilities"* and *"Lint my
Python code"* — the two probes designed to catch it over-claiming.

**So the optimization loop stops here, and that is the correct outcome.** With F1 already at
1.0 there are no false negatives or false positives to build a hypothesis from, and the
promotion gate these rounds ran under — `min(candidate F1) > max(incumbent F1)` — cannot be
satisfied by any candidate. (The gate has since been replaced by a paired cluster-bootstrap
confidence interval on the difference, Holm-corrected across the survivors; the conclusion on
this page is unchanged, because an arm whose best case is a tie cannot separate from the
incumbent under either rule. The rounds below are reported as they were run.) Running the three A/B stages anyway, on this suite with three candidates and two
survivors, would have cost `(3+1)×14 + 3×(2+1)×14 + 6×7` = **224 further agent runs** to
chase a number that is not reachable. (Arithmetic at the 14-train / 7-test revision these
runs used — every stage count on this page is computed at the revision that stage actually
ran against, which is why they do not all divide the same way.)

The first finding is therefore about the change that prompted this: **the 66-character trim
was safe.** That is now measured rather than assumed.

The second finding is that **a ceiling result is as much a statement about the suite as about
the description.** Those fourteen well-separated rows could not distinguish a good description from
a better one. If you need to tell those apart, the fix is more rows and harder ones — not a
looser gate.

#### Where the headroom actually was

The sibling matrix is the part worth reading, and in a multi-skill plugin it usually is:

- **`analyze` under-claims.** *"Look at my evaluation results from last night's run and tell
  me what regressed"* engaged **nothing at all**, in every run. Its description names "why a
  run failed" but never regression, and nothing else claimed the request either.

This is what sibling-owned rows buy you. A plain distractor would have said only that
something misfired; these say **where the request went**, which is the difference between
"this description is vague" and "these two descriptions are fighting."

#### The finding that evaporated

An earlier pair of runs showed something else: on both *"Set up coder-eval for this
repository"* and *"Get evaluations going in this project from scratch"*, the `task` skill
fired — apparently annexing setup work — dropping its precision to 0.667 and 0.500. It
reproduced on **both splits**, which is normally the signal that a finding is real.

Running the same train split three more times settled it. The row fires **two times in
three**, on byte-identical prompts (`expected_skill` is a dataset label the criterion reads
and the agent never sees, so nothing about the rows changed):

```
hard-3   expected=(distractor)   ['task', 'task', '-']    <-- UNSTABLE
```

So the effect is real but intermittent, and the two runs that agreed were luck, not
evidence. **That is the whole reason the promotion gate demands replicates at all** — the rule
has since become an interval on the paired difference rather than a comparison of ranges, but
what the replicates are for is exactly this. Had this been a candidate description rather than an incumbent's quirk, those two
agreeing runs would have "proved" an improvement worth shipping. Report what replicates;
treat anything else as a hypothesis.

Note what the same three runs said about `analyze` (still the 14-train-row revision — two
`analyze` rows then, four in the committed file, which is why re-running today gives a
different baseline):

```
an-1     expected=analyze   ['analyze', 'analyze', 'analyze']
hard-2   expected=analyze   ['-', '-', '-']
```

Recall **0.500 in all three runs**, precision 1.000 in all three. Perfectly stable, one row
consistently missed, no over-claiming. That is a hypothesis worth spending on — which is
where the rest of this tutorial goes.

### Step 7 — Confirm on the test split

Even with nothing to promote, the test split is what turns "the trim was safe" from a claim
about the rows you looked at into a claim about rows you did not.

```bash
coder-eval run tasks/skills/lint-tasks-activation.yaml \
  --split test -D run_limits.stop_early=false
```

| Skill | recall.yes | precision.yes | f1.yes |
| --- | --- | --- | --- |
| `lint-tasks` | 1.000 | 1.000 | **1.000** |
| `task` | 1.000 | 1.000 | 1.000 |

`lint-tasks` reproduces at ceiling on rows it was never checked against, including the
oblique *"Are my evals any good?"*. Across every run in this tutorial — two wiring states,
two splits, three sittings — it never missed a positive and never took a distractor. That is
about as much as a suite this size can say, and it is enough to conclude the trim did no
harm.

### What you get running Part 1 today

Every number above was measured **before** the change Part 2 ends with. Re-running Part 1
against the committed suite therefore does *not* reproduce them, and the reason is the
point of the whole page rather than a defect in it.

Re-run, three times, on the committed 17-row train split — 51 agent runs, minutes on Sonnet:

```bash
export SKILL_SOURCE_PATH="$(pwd)/plugins/coder-eval"
coder-eval run tasks/skills/lint-tasks-activation.yaml \
  --split train -D run_limits.stop_early=false
```

| Skill | recall.yes | precision.yes | f1.yes (3 runs) |
| --- | --- | --- | --- |
| `lint-tasks` | 1.000 | 1.000 | **1.000, 1.000, 1.000** |
| `analyze` | 1.000 | 1.000 | **1.000, 1.000, 1.000** |
| `task` | 1.000 | 0.667 / 0.667 / 1.000 | 0.800, 0.800, 1.000 |

`completion_rate` is 1.000 on all three; no row was excluded.

Three things to read off it:

- **`lint-tasks` is still at ceiling.** Part 1's finding holds on a suite 3 train rows
  larger than the one that produced it, which is the strongest form the claim can take here.
- **`analyze`'s headroom is gone — because Part 2 closed it.** The description this page
  promotes is committed (`4c7481c`), so the skill a re-run measures is the *improved* one.
  Recall on `analyze` reads 1.000 where Part 1 recorded 0.000. The suite also grew from 2
  `analyze` train rows to 4, so both the instrument and the subject changed; what is not in
  doubt is the direction, and that the fix shipped.
- **`hard-3` is still unstable, at exactly the rate Step 6 measured.** The `task` sibling
  takes that distractor in **2 of 3** runs — the same two-in-three the earlier sitting
  found, months later and on a different revision of the suite. A single run would have
  shown precision 1.000 or 0.667 and either would have looked like a fact.

That last one is the page's own lesson arriving unprompted: the instability is a property
of the row, not of the sitting that first caught it. It is also why the suite gate reports
a failure on two of the three runs — `precision.yes` dips under its 0.7 floor — and why the
honest baseline for this suite is a range, not a number.

The **test** split (now 11 rows, up from 7), run once, tells the same story: `lint-tasks` 1.000 /
1.000 / **1.000**, `analyze` likewise, and `task` at precision 0.500 on one misfire. So
Step 7's conclusion — that `lint-tasks` holds at ceiling on rows it was never checked
against — survives a suite half again as large, and the `task` misfire is visible on both
halves, which is what makes it a property of the skill rather than of one split.

---

## Part 2 — A full A/B that promotes (`analyze`)

### Optimizing the skill that actually had headroom

`lint-tasks` had nothing to fix. `analyze` did, so the loop ran for real against it. The
hypothesis, phrased as a claim about specific rows: **the description never names
regression or comparison between runs**, so requests about things getting *worse* find
nothing to match.

> As in Part 1, every number below was measured **before** the change this part ends with,
> and for the same reason: the winner is committed, so it is the incumbent now.
> [What you get running Part 2 today](#what-you-get-running-part-2-today) re-runs Stage A
> against the committed suite and reports where it stops.

Three candidates, one per hypothesis, each differing from the incumbent only in `analyze`'s
frontmatter `description`, each a full seven-skill snapshot:

| Candidate | Hypothesis |
| --- | --- |
| `a-regression` | Name regression directly: *"what regressed or got worse since a previous run"* |
| `b-results` | Users say "results" and "scores"; the description only says "run" |
| `c-symptom` | Lead the trigger clause with symptoms rather than the operation |

#### Wiring the arms: snapshots, then one experiment file per stage

This is the part the numbers below cannot show, and the part most easily got wrong.

Each candidate is a snapshot of the **whole plugin root**, not of one skill:

```
.optimize-skill/analyze/1-a-regression/
    .claude-plugin/plugin.json   <- copy it if the source had one
    skills/
        analyze/SKILL.md         <- the arm's ONE varying part: its description
        lint-tasks/SKILL.md      <- every sibling, copied unchanged
        task/SKILL.md
        ...                      <- all seven
    reference/                   <- and everything else the root held
```

Both halves of that matter and both fail silently. **`plugin.json`**: with no manifest the
namespace defaults to the *directory name*, so the arms would compete as `1-incumbent:analyze`
against `1-a-regression:analyze` — differing in the name shown in the listing as well as in the
description under test, on the very track where activation *is* a competition between listings.
**The siblings**: a variant's `plugins` block *replaces* the task's, so this directory is the
only skill source the arm gets. Snapshot one skill and every sibling criterion observes `no` in
every arm, and the sibling half of the gate "passes" by measuring nothing.

Add `.optimize-skill/` to `.gitignore` first — a round writes several copies of the whole
plugin per arm.

The experiment mounts each snapshot by absolute path, using the same `agent.plugins` mechanism
the suite itself uses:

```yaml
experiment_id: "optimize-analyze-round-1"
defaults:
  run_limits:
    stop_early: false
variants:
  - variant_id: "incumbent"
    agent:
      plugins:
        - type: "local"
          path: "/abs/path/to/.optimize-skill/analyze/1-incumbent"
  - variant_id: "a-regression"
    agent:
      plugins:
        - type: "local"
          path: "/abs/path/to/.optimize-skill/analyze/1-a-regression"
```

Paths resolve against the **process working directory**, not the task file's, so write them
absolute. And each `path` must be a plugin root holding `skills/` — the same trap as Step 3.

**There is no flag that selects a subset of an experiment's variants**, so the arm set changes
by writing another file. Three per round:

| Stage | File | Arms |
| --- | --- | --- |
| A — triage | `round1-triage.yaml` | incumbent + all three candidates |
| B — gate | `round1-gate.yaml` | incumbent + the two clean survivors |
| C — confirm | `round1-confirm.yaml` | incumbent + `a-regression` only |

Copying the triage file and deleting the arms that did not survive is one edit; reusing it
instead costs several times the budgeted runs and, at Stage C, renders no
`## Paired Comparison` block at all, because that block fires only for exactly two variants.

#### Stage A — triage (68 runs)

```bash
coder-eval run tasks/skills/lint-tasks-activation.yaml \
  -e .optimize-skill/analyze/round1-triage.yaml --split train
```

| Arm | analyze recall | precision | F1 | completion |
| --- | --- | --- | --- | --- |
| incumbent | 0.500 | 1.000 | 0.667 | 1.000 |
| `a-regression` | 0.750 | 1.000 | **0.857** | 1.000 |
| `c-symptom` | 0.750 | 1.000 | **0.857** | 1.000 |
| `b-results` | 0.667 | 1.000 | 0.800 | **0.941** |

`b-results` looks competitive and **is not comparable**: it lost a row to an error, so its
recall was computed over 3 analyze rows instead of 4. Check `completion_rate` before ranking
anything. The two clean leaders went through to the gate.

#### Stage B — the gate (153 runs, three separate invocations)

Three `coder-eval run` commands, **not** `--repeats 3` — see the Reference section for why:

```bash
coder-eval run tasks/skills/lint-tasks-activation.yaml \
  -e .optimize-skill/analyze/round1-gate.yaml --split train --run-dir runs/round1-gate-1
coder-eval run tasks/skills/lint-tasks-activation.yaml \
  -e .optimize-skill/analyze/round1-gate.yaml --split train --run-dir runs/round1-gate-2
coder-eval run tasks/skills/lint-tasks-activation.yaml \
  -e .optimize-skill/analyze/round1-gate.yaml --split train --run-dir runs/round1-gate-3
```

| Arm | run 1 | run 2 | run 3 | Range |
| --- | --- | --- | --- | --- |
| incumbent | 0.667 | *(row excluded)* | 0.667 | [0.667, 0.667] |
| `c-symptom` | 0.857 | 0.857 | 0.857 | [0.857, 0.857] |
| **`a-regression`** | **1.000** | **1.000** | **1.000** | **[1.000, 1.000]** |

Both candidates clear `min(candidate) > max(incumbent)` with no overlap, and every arm held
`lint-tasks` and `task` at recall 1.000 — no sibling regression. `a-regression` wins
outright: it turns the one consistently-missed row into a consistent hit without touching
precision.

One incumbent invocation dropped a row and was excluded from the gate rather than averaged
in. A gate computed over a shifting denominator is not a gate.

#### Stage C, and a test split that could not answer the question (54 runs)

The winner went to test as a **two-variant** experiment at `--repeats 3`, which is the one
place `--repeats` is correct. This attempt ran against the **9-row** test half — before the
two fresh rows below were written — so 2 arms × 9 rows × 3 replicates = 54 runs:

```bash
coder-eval run tasks/skills/lint-tasks-activation.yaml \
  -e .optimize-skill/analyze/round1-confirm.yaml --split test --repeats 3
```

`paired_comparison` fires only for exactly two variants and averages replicates per row
before pairing.

Both arms scored `analyze` F1 **1.000**, and the paired block was a flat tie:

```
**Paired mean diff (incumbent - a-regression)**: +0.000 [95% CI +0.000, +0.000], p = 1.000
```

That is not a confirmation. It is an **uninformative test**: its `analyze` rows happened
to be ones the incumbent already caught, because every regression-phrased row had been put
in the train half. A test can only confirm a fix for a failure mode it actually contains.

The remedy is the one `optimize-skill` prescribes — author **fresh test rows** that
exercise the failure mode, at promotion time, when you know what needs confirming:

```jsonl
{"id": "an-6", "expected_skill": "analyze", "split": "test", "prompt": "Which of my tasks got worse after I switched the model?"}
{"id": "an-7", "expected_skill": "analyze", "split": "test", "prompt": "Something in the suite degraded this week. Find out what."}
```

Write them as requests a real user would send, and commit to whatever they say — rows
authored to flatter a candidate confirm nothing.

#### And then the infrastructure lied to us (66 runs)

The re-run returned a result that looked publishable and was worthless:

```
**Paired mean diff (incumbent - a-regression)**: +0.162 [95% CI +0.011, +0.312], d = 0.72, p = 0.038
```

Read the sign: that says the **incumbent** was better, significantly. (The header subtracts
in variant declaration order, not better-minus-worse — with `incumbent` declared first, a
candidate win reads negative. `/coder-eval:optimize-skill`'s Stage B and Stage C state the
full rule; never resolve a direction from memory.) It is an artifact.
`completion_rate` gives it away — `a-regression` lost 11 of 33 rows, the incumbent 6:

```
a-regression   rows_total=33 rows_excluded=11 completion=0.667
incumbent      rows_total=33 rows_excluded= 6 completion=0.818
```

The excluded rows were not timeouts. They carried:

```
Communication with agent failed: Claude Code returned an error result
Details: You've hit your org's monthly spend limit
```

A billing limit reached mid-run removed rows from one arm more than the other, and the
paired test faithfully reported the resulting difference at *p* = 0.038. **A p-value
computed over an eroded, asymmetric sample is not evidence of anything.** This is why
`completion_rate` is a gateable metric, and why the first thing to read in a rollup is the
denominator, not the effect. The run was discarded, not interpreted.

#### Stage C, run properly (66 runs)

With budget restored, the same experiment ran again over the 11-row test. Erosion this
time was one row against `a-regression` and none against the incumbent — near-symmetric, and
pointing *against* the candidate, so any positive result is conservative:

```
a-regression   rows_total=33 rows_excluded=1 completion=0.970
incumbent      rows_total=33 rows_excluded=0 completion=1.000
```

| Arm | analyze recall | precision | **F1** | siblings |
| --- | --- | --- | --- | --- |
| incumbent | 0.833 | 1.000 | **0.909** | `task` precision 0.750 |
| **`a-regression`** | **1.000** | **1.000** | **1.000** | all 1.000 |

**The direction reproduces on rows the candidate was never trained against**, which is what
Stage C is required to show. One row separates them — and it is one of the *fresh* rows
authored at promotion time:

```
an-6  "Which of my tasks got worse after I switched the model?"
        incumbent      ['analyze', '-', '-']              1 of 3
        a-regression   ['analyze', 'analyze', 'analyze']  3 of 3
```

Every other test row is identical across the arms. The incumbent also shows the
intermittent `task` misfire on the setup row once more (precision 0.750), consistent with
the 2-in-3 rate measured earlier.

**And the paired comparison says nothing:**

```
**Paired mean diff (incumbent - a-regression)**: +0.000 [95% CI -0.088, +0.088], d = 0.00, p = 1.000
```

That is not a contradiction — it is the documented limit of that block. It pairs per-row
`weighted_score`, which averages **all three** criteria on every row, so a gain confined to
one criterion on one row out of eleven is diluted below what 11 pairs can resolve. F1 is the
promotion metric; the paired block corroborates direction when it can and, at this suite
size, it cannot. Report both, and do not let a null paired result overturn a metric it was
never measuring.

**Verdict: promote.** Gated on train (1.000 vs 0.667, non-overlapping, three invocations),
confirmed in direction on the test split (1.000 vs 0.909), no sibling regression anywhere, and
precision never off 1.000 in any run of either stage.

### What you get running Part 2 today

Re-running Stage A against the committed suite measures a **different incumbent** — the one
this page promotes — and the answer it returns is Part 1's stop condition, arriving on the
skill that used to be the counter-example to it.

The re-run is the same shape as the original Stage A: four arms over the same 17-row train
split, **68 agent runs**, $3.84 and 12 minutes on Sonnet. Only the arms' contents changed.
`a-regression` won round 1 and is committed, so it *is* the incumbent; the candidate set is
therefore fresh — two of round 1's hypotheses re-expressed against the new incumbent, plus
one the first round had no reason to ask:

| Arm | Hypothesis | chars |
| --- | --- | --- |
| `incumbent` | round 1's promoted `a-regression`, as committed at `4c7481c` | 269 |
| `a-results` | users say "results" and "scores"; the description says "run" (round 1's `b-results`) | 281 |
| `b-symptom` | lead the trigger clause with symptoms, not the operation (round 1's `c-symptom`) | 242 |
| `c-compact` | the length is not load-bearing — same trigger tokens, 53 fewer characters | 216 |

```bash
export SKILL_SOURCE_PATH="$(pwd)/plugins/coder-eval"
coder-eval run tasks/skills/lint-tasks-activation.yaml \
  -e .optimize-skill/analyze/round2-triage.yaml \
  --split train -D run_limits.stop_early=false
```

| Arm | analyze recall | precision | F1 | completion | `task` precision |
| --- | --- | --- | --- | --- | --- |
| **incumbent** | 1.000 | 1.000 | **1.000** | 1.000 | 0.667 |
| `c-compact` | 1.000 | 1.000 | **1.000** | 1.000 | 1.000 |
| `a-results` | 0.750 | 1.000 | 0.857 | 1.000 | 1.000 |
| `b-symptom` | 0.750 | 1.000 | 0.857 | 1.000 | 0.667 |

`lint-tasks` held recall and precision at 1.000 in every arm, and every arm scored all 17
rows — so unlike the original Stage A, nothing here was ranked against a shifting
denominator.

**The round stops at Stage A, and that is the result.** With the incumbent at F1 1.000, the
gate cannot be satisfied by anything: the best a candidate can do is tie, and a tie separates
from the incumbent under neither this round's rule (`min(candidate F1) > max(incumbent F1)`)
nor the interval that replaced it. Stage B and Stage C were not run. Establishing that cost 68 runs against the
`(3+1)×17 + 3×(2+1)×17 + 2×11×3` = **287** the full three stages would have — which is what
Stage A is for.

Three things to read off it:

- **The headroom Part 2 found is closed, measured by the instrument that found it.** Part 1
  caught `analyze` missing "what regressed" in every run; the promoted description takes all
  four `analyze` train rows here, in the same suite, at precision 1.000. Part 1's re-run says
  this from the other side; this says it against three live challengers.
- **`c-compact` ties at 1.000 with 53 fewer characters, and a tie does not promote.** On F1
  the gate is indifferent to it. It is still a real finding about a *different* budget: these
  seven descriptions total 1,577 of the 1,600-character listing cap, and `c-compact` hands 53
  back. Acting on that means gating on length with F1 held constant — a different comparison
  with its own replicates, not this one's leftovers. As it stands it is a single run, which
  is exactly the evidence Step 6 says not to ship on.
- **One row separates every arm that differs, and it is `an-4`** — *"Compare last week's eval
  results with this week's and tell me what got worse."* Both losing candidates drop it and
  nothing else; all sixteen other rows are identical across all four arms. The suite's entire
  discriminating power sits in one prompt, which is the small-suite cost Step 1 warned about
  and the reason a 0.857 here should not be read as a candidate being meaningfully worse.

And the page's own instability turned up again, in a form that settles it further. `hard-3`
engaged `task` in the `incumbent` and `b-symptom` arms and not in `a-results` or `c-compact`
— arms whose `task` description is **byte-identical**, since only `analyze`'s description
varies. A difference across arms that cannot differ in the relevant text is not an effect of
the arm. Two in four, where Part 1 measured two in three, and this time with the confound
ruled out by construction rather than by repetition.

---

## Reference

### The three stages, and when each applies

They did not run for `lint-tasks`, because that incumbent was already perfect. They did run
for `analyze`, above. In summary:

| Stage | What it does | Replicates |
| --- | --- | --- |
| **A — triage** | All candidates + incumbent, one invocation, `--split train`. Rank by F1, discard anything at or below the incumbent. Promotes nothing on its own — but when every candidate lands at or below, it ends the round there, which against a ceiling incumbent it always will. | one run |
| **B — gate** | Survivors + incumbent, `--split train`, **three separate `coder-eval run` invocations**. Promote only when the paired cluster-bootstrap interval on the difference **excludes zero** *and* the **Holm-corrected** test rejects across the survivors. | three runs |
| **C — confirm** | Best survivor vs. incumbent only, `--split test --repeats 3`. Read the rendered `## Paired Comparison` block. | `--repeats 3` |

**The Stage B rule above is not the one these rounds ran under.** They used
`min(candidate F1) > max(incumbent F1)` — range non-overlap — which threw away the pairing
(both arms run the *same* rows) and had very little power at 8–12 rows per polarity. That rule
is now retained only as a **reported diagnostic**, printed beside the interval and never
consulted in the decision. The narrative above is reported as it was run, and its conclusions
are unchanged: an arm whose best case is a tie cannot separate under either rule.

`coder-eval` computes the current verdict for you — the skill drives
`optimize_gate.activation_gate` and then one `holm_promote` call over **all** the survivors at
once, because Holm corrects a *family*: correcting one candidate at a time degenerates to an
uncorrected `p ≤ alpha` while still looking like a correction.

**Stage B must be three invocations, not `--repeats 3`.** Suite rollups are keyed on
`(variant, suite)` alone, so `--repeats` pools every replicate into a single `suite.json`
with one confusion matrix — the per-replicate F1 the gate compares would not exist. The cost
is identical either way.

**Stage C is the one place `--repeats` is correct.** `paired_comparison` fires only for
exactly two variants and averages replicates per row *before* pairing, which is what makes
the paired *t*-test valid. It pairs per-row `weighted_score` — row correctness, not F1 — so
it corroborates the direction; it does not re-test the promotion metric.

And be precise about what Stage B proves. Resampling *rows* is what makes the interval bound
row-sampling variation, and pooling each drawn row's replicates is what keeps run noise in it
too — so it bounds both, which the old fixed-row rule did not. Holm bounds the multiplicity
across the survivors tested. What none of it corrects for is the survivors having been *chosen*
on those same rows at Stage A. Stage B bounds noise and multiplicity; **the test split is what
bounds the fit** — which is why Stage C is not optional. Report a gate result as "separated
beyond run-to-run and row-sampling noise on the train rows", never as "proven better".

### Two caveats about what else is in the sandbox

**The machine's other skills are part of the experiment.** One distractor row engaged a
skill named `review` — not from this plugin at all, but from the operator's own install.
Activation is a competition for a shared listing budget, so whatever else is installed
competes too. If you need a clean-room measurement, isolate the environment; if you want a
realistic one, record what was installed alongside your results.

**And `skill_name` matching is by bare name, which can collide.** The criterion strips any
`plugin:` prefix before comparing, so `coder-eval:init` and a differently-owned `init` are
indistinguishable to it. That is not hypothetical here: Claude Code ships its own unscoped
`init` skill, and the setup rows in this suite engage *that* one. A criterion written as

```yaml
- type: "skill_triggered"
  skill_name: "init"
```

would have quietly scored a different skill's activation as though it were the plugin's.

Before trusting a `skill_triggered` result, check that the skill's bare name is unique among
everything installed — `/context` and `/doctor` both list the active set. A colliding name
does not error; it just measures the wrong thing. Where a collision exists, the honest
options are to rename the skill, or to measure it somewhere the collision is absent.

## What to take away

- **Check that recall is non-zero before you believe any low score.** A misconfigured path
  looks exactly like a bad description and costs the same to produce.
- **Read the denominator before the effect.** `completion_rate` invalidated two separate
  comparisons here — one candidate ranked on 3 rows instead of 4, and a *p* = 0.038 result
  that was a billing limit eroding one arm harder than the other. An effect measured over a
  sample that moved is not an effect.
- **A ceiling baseline means stop.** `lint-tasks` was perfect, so the loop declined to spend
  224 runs chasing a number the gate makes unreachable. Then it found the real headroom next
  door.
- **A tie is not a promotion — and a promoted change puts its own skill at that ceiling.**
  Re-running Part 2's Stage A today measures the description this page shipped: F1 1.000, so
  the best of three fresh candidates could only match it and the round ended at Stage A for
  68 runs of the 287 all three stages would have cost. The gate is `>`, not `≥`, exactly so
  that a coin-flip tie cannot ship a change — including one that is genuinely better on some
  other axis, as `c-compact` is on length.
- **Two agreeing runs are not evidence.** The `task` misfire reproduced on both splits and
  still turned out to be 2-in-3 variance. Only replicates told the difference — and it is
  still 2-in-3 today, on a later revision of the suite, which is about as clean a
  demonstration as this page could ask for that the instability belongs to the row rather
  than to the sitting that caught it.
- **A promoted change makes its own baseline unreproducible, and that is success.** Re-run
  Part 1 against this repository now and `analyze` reads 1.000 where it read 0.000, because
  the description this page promotes is committed. Numbers in a walkthrough date the moment
  the walkthrough works; say which revision each was measured at rather than quietly
  refreshing them.
- **A test only confirms failure modes it contains.** The first one here was a flat tie
  because every regression-phrased row sat in the train half. Fresh rows, authored to test the
  hypothesis rather than to flatter the candidate, are the fix.
- **A test confirms direction, not significance.** `a-regression` shipped on F1 1.000 vs
  0.909 on unseen rows while the paired *t*-test read exactly zero. At eleven pairs, across
  three criteria, that block cannot resolve a one-criterion gain — which is a fact about the
  test, not about the change. Report both and say which one you promoted on.

## Next

- [Comparing models](05-comparing-models.md) — the same experiment-variant machinery, applied to models instead of descriptions.
- [Bring Your Own Dataset](../DATASETS.md) — split labels, sampling precedence, and suite-level thresholds in full.
- [Tutorial 09 — Optimizing a Skill Body](09-optimizing-a-skill-body.md) — the execution
  track: same method, different instrument, and another honest stop.
- [Claude Code plugin](../PLUGIN.md) — all seven skills.
