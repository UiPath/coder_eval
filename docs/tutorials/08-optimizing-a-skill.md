# Optimizing a skill description

`check-skill` tells you whether a skill triggers. This tutorial covers the next question:
**can its description be made to trigger better, and how would you know?**

The honest answer is usually "measure first, and often the answer is don't change it." This
walkthrough is a real run against a real skill in this repository, reported with the numbers
it actually produced — including the part where the first attempt measured nothing at all.

**What you will do:** build an activation suite for `lint-tasks`, split it into tune and
holdout rows, baseline it, read the confusion matrix, and decide whether to spend anything.
For `lint-tasks` the answer turns out to be no — about 20 agent runs settle it. The suite
then points at a sibling that *does* have headroom, `analyze`, and the full three-stage A/B
runs against that: roughly 350 further runs, ending in a promotion that survives the
holdout.

All of it on Sonnet. Never Opus for a suite this size.

**Prerequisites:** the `coder-eval` CLI, the plugin installed, and credentials for the
`claude-code` agent. Start with [Your first evaluation](01-first-evaluation.md) if you have
not run anything yet.

## Why this skill

`lint-tasks` was picked for four reasons, and they are worth stealing as selection criteria:

1. **It is model-invokable.** Its description actually enters the activation decision. A
   skill with `disable-model-invocation: true` — here `init`, `ci`, and `optimize-skill`
   itself — can never be optimized this way, because its description never competes for
   anything. `optimize-skill` hard-stops on exactly that.
2. **It has a real boundary dispute with a sibling.** `lint-tasks` reviews existing tasks;
   `task` authors new ones. *"Are my evals any good?"* and *"add a task for X"* genuinely
   misroute between them.
3. **Its description had just been trimmed** by 66 characters to fit the plugin's shared
   listing budget. That trim was an unmeasured change to activation behaviour. This turns it
   into a measured one.
4. **It supplies its own distractors.** The description mentions auditing and gaps, so
   *"audit my dependencies"* is a natural false-positive probe.

## Step 1 — Build the suite

Do not hand-author it. Run the sibling skill:

```
/coder-eval:check-skill lint-tasks
```

That produces a task YAML plus a JSONL row file. The suite used here has **21 rows**,
counted by `expected_skill` — the field that actually decides a row's polarity:

| Kind | `expected_skill` | Total | tune | holdout |
| --- | --- | --- | --- | --- |
| Positive | `lint-tasks` | 8 | 5 | 3 |
| Distractor | `""` | 9 | 6 | 3 |
| Sibling-owned | `task` | 3 | 2 | 1 |
| Sibling-owned | `analyze` | 1 | 1 | 0 |

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

## Step 2 — Label the splits

Add a `split` to every row and point the dataset at the field:

```yaml
dataset:
  paths:
    - "lint-tasks-activation-rows.jsonl"
  split_field: "split"
```

```jsonl
{"id": "pos-1", "expected_skill": "lint-tasks", "split": "tune",    "prompt": "..."}
{"id": "pos-5", "expected_skill": "lint-tasks", "split": "holdout", "prompt": "Are my evals any good?"}
```

Then select one at run time with `--split tune` or `--split holdout`. The filter runs
**before** any sampling, so `--split tune --sample 8` is always drawn from tune rows alone.

Keep both polarities on both sides. A holdout of only positives measures recall and calls it
a result.

## Step 3 — Make the skill reachable, and check that it worked

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

!!! danger "The first baseline for this tutorial scored recall 0.0"

    It pointed one level too deep, at the bare skills directory. Nothing loaded, no `Skill`
    tool was ever offered, and every positive row failed. On an earlier, smaller draft of
    this suite — 10 tune rows rather than the 14 below — that produced:

    ```
    lint-tasks   recall.yes=0.000 precision.yes=0.000 f1.yes=0.000
                 confusion: [('no','no',4), ('yes','no',3)]
    ```

    Note the confusion matrix accounts for only 7 of the 10 rows; the missing 3 are the
    next section.

    A wrong path is only a **warning**, never an error. The run completes, the report
    renders, and the number it produces is indistinguishable from a skill whose description
    is hopeless. In a one-shot check that misleads you once; in an optimization loop it
    poisons everything — every candidate scores zero, nothing separates from the incumbent,
    and you conclude your rewrites are all bad when your wiring is broken.

    **So the first thing to check is never the description. It is whether recall is
    non-zero at all.**

Point it at the plugin root and the same suite goes to `recall.yes=1.000`. Nothing about the
descriptions changed.

## Step 4 — Cap the run, or pay for exploration

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

## Step 5 — Baseline on the tune split

```bash
coder-eval run tasks/skills/lint-tasks-activation.yaml \
  --split tune -D run_limits.stop_early=false
```

`stop_early: false` is insurance: if a suite arms early stop, a pass-stop can end a run
before a later sibling misfire is observable, and authoritative precision needs the full
trajectory. A stock `check-skill` suite arms nothing, so on that suite this flag changes
nothing.

**Check the resolved row count, not just the exit code.** A mistyped split (`--split holdou`)
is reported as a skipped task and the run still **exits 0** — a green run over zero rows.

Result, 14 tune rows:

| Skill | recall.yes | precision.yes | f1.yes |
| --- | --- | --- | --- |
| `lint-tasks` | 1.000 | 1.000 | **1.000** |
| `task` | 1.000 | 1.000 | 1.000 |
| `analyze` | 0.000 | 0.000 | 0.000 |

## Step 6 — Read it, and decide whether to spend anything

**The target skill is at ceiling.** `lint-tasks` caught all five positives, including
*"My evals are a mess. Can you go through them and tell me what to fix?"*, and stayed off all
nine negatives, including *"Audit my dependencies for known vulnerabilities"* and *"Lint my
Python code"* — the two probes designed to catch it over-claiming.

**So the optimization loop stops here, and that is the correct outcome.** With F1 already at
1.0 there are no false negatives or false positives to build a hypothesis from, and the
promotion gate — `min(candidate F1) > max(incumbent F1)` — cannot be satisfied by any
candidate. Running the three A/B stages anyway, on this suite with three candidates and two
survivors, would have cost `(3+1)×14 + 3×(2+1)×14 + 6×7` = **224 further agent runs** to
chase a number that is not reachable.

The first finding is therefore about the change that prompted this: **the 66-character trim
was safe.** That is now measured rather than assumed.

The second finding is that **a ceiling result is as much a statement about the suite as about
the description.** Fourteen well-separated rows could not distinguish a good description from
a better one. If you need to tell those apart, the fix is more rows and harder ones — not a
looser gate.

### Where the headroom actually was

The sibling matrix is the part worth reading, and in a multi-skill plugin it usually is:

- **`analyze` under-claims.** *"Look at my evaluation results from last night's run and tell
  me what regressed"* engaged **nothing at all**, in every run. Its description names "why a
  run failed" but never regression, and nothing else claimed the request either.

This is what sibling-owned rows buy you. A plain distractor would have said only that
something misfired; these say **where the request went**, which is the difference between
"this description is vague" and "these two descriptions are fighting."

### The finding that evaporated

An earlier pair of runs showed something else: on both *"Set up coder-eval for this
repository"* and *"Get evaluations going in this project from scratch"*, the `task` skill
fired — apparently annexing setup work — dropping its precision to 0.667 and 0.500. It
reproduced on **both splits**, which is normally the signal that a finding is real.

Running the same tune split three more times settled it. The row fires **two times in
three**, on byte-identical prompts (`expected_skill` is a dataset label the criterion reads
and the agent never sees, so nothing about the rows changed):

```
hard-3   expected=(distractor)   ['task', 'task', '-']    <-- UNSTABLE
```

So the effect is real but intermittent, and the two runs that agreed were luck, not
evidence. **That is the whole reason the promotion gate demands non-overlapping replicate
ranges.** Had this been a candidate description rather than an incumbent's quirk, those two
agreeing runs would have "proved" an improvement worth shipping. Report what replicates;
treat anything else as a hypothesis.

Note what the same three runs said about `analyze`:

```
an-1     expected=analyze   ['analyze', 'analyze', 'analyze']
hard-2   expected=analyze   ['-', '-', '-']
```

Recall **0.500 in all three runs**, precision 1.000 in all three. Perfectly stable, one row
consistently missed, no over-claiming. That is a hypothesis worth spending on — which is
where the rest of this tutorial goes.

## Optimizing the skill that actually had headroom

`lint-tasks` had nothing to fix. `analyze` did, so the loop ran for real against it. The
hypothesis, phrased as a claim about specific rows: **the description never names
regression or comparison between runs**, so requests about things getting *worse* find
nothing to match.

Three candidates, one per hypothesis, each differing from the incumbent only in `analyze`'s
frontmatter `description`, each a full seven-skill snapshot:

| Candidate | Hypothesis |
| --- | --- |
| `a-regression` | Name regression directly: *"what regressed or got worse since a previous run"* |
| `b-results` | Users say "results" and "scores"; the description only says "run" |
| `c-symptom` | Lead the trigger clause with symptoms rather than the operation |

### Stage A — triage (68 runs)

| Arm | analyze recall | precision | F1 | completion |
| --- | --- | --- | --- | --- |
| incumbent | 0.500 | 1.000 | 0.667 | 1.000 |
| `a-regression` | 0.750 | 1.000 | **0.857** | 1.000 |
| `c-symptom` | 0.750 | 1.000 | **0.857** | 1.000 |
| `b-results` | 0.667 | 1.000 | 0.800 | **0.941** |

`b-results` looks competitive and **is not comparable**: it lost a row to an error, so its
recall was computed over 3 analyze rows instead of 4. Check `completion_rate` before ranking
anything. The two clean leaders went through to the gate.

### Stage B — the gate (153 runs, three separate invocations)

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

## Step 7 — Confirm on the holdout

Even with nothing to promote, the holdout is what turns "the trim was safe" from a claim
about the rows you looked at into a claim about rows you did not.

```bash
coder-eval run tasks/skills/lint-tasks-activation.yaml \
  --split holdout -D run_limits.stop_early=false
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

### Stage C, and a holdout that could not answer the question

The winner went to holdout as a **two-variant** experiment at `--repeats 3`, which is the one
place `--repeats` is correct: `paired_comparison` fires only for exactly two variants and
averages replicates per row before pairing.

Both arms scored `analyze` F1 **1.000**, and the paired block was a flat tie:

```
**Paired mean diff (incumbent - a-regression)**: +0.000 [95% CI +0.000, +0.000], p = 1.000
```

That is not a confirmation. It is an **uninformative holdout**: its `analyze` rows happened
to be ones the incumbent already caught, because every regression-phrased row had been put
in the tune half. A holdout can only confirm a fix for a failure mode it actually contains.

The remedy is the one `optimize-skill` prescribes — author **fresh holdout rows** that
exercise the failure mode, at promotion time, when you know what needs confirming:

```jsonl
{"id": "an-6", "expected_skill": "analyze", "split": "holdout", "prompt": "Which of my tasks got worse after I switched the model?"}
{"id": "an-7", "expected_skill": "analyze", "split": "holdout", "prompt": "Something in the suite degraded this week. Find out what."}
```

Write them as requests a real user would send, and commit to whatever they say — rows
authored to flatter a candidate confirm nothing.

### And then the infrastructure lied to us

The re-run returned a result that looked publishable and was worthless:

```
**Paired mean diff (incumbent - a-regression)**: +0.162 [95% CI +0.011, +0.312], d = 0.72, p = 0.038
```

Read the sign: that says the **incumbent** was better, significantly. It is an artifact.
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

### Stage C, run properly

With budget restored, the same experiment ran again over the 11-row holdout. Erosion this
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

**The direction reproduces on rows the candidate was never tuned against**, which is what
Stage C is required to show. One row separates them — and it is one of the *fresh* rows
authored at promotion time:

```
an-6  "Which of my tasks got worse after I switched the model?"
        incumbent      ['analyze', '-', '-']              1 of 3
        a-regression   ['analyze', 'analyze', 'analyze']  3 of 3
```

Every other holdout row is identical across the arms. The incumbent also shows the
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

**Verdict: promote.** Gated on tune (1.000 vs 0.667, non-overlapping, three invocations),
confirmed in direction on holdout (1.000 vs 0.909), no sibling regression anywhere, and
precision never off 1.000 in any run of either stage.

## The three stages, and when each applies

They did not run for `lint-tasks`, because that incumbent was already perfect. They did run
for `analyze`, above. In summary:

| Stage | What it does | Replicates |
| --- | --- | --- |
| **A — triage** | All candidates + incumbent, one invocation, `--split tune`. Rank by F1, discard anything at or below the incumbent. Decides nothing. | one run |
| **B — gate** | Survivors + incumbent, `--split tune`, **three separate `coder-eval run` invocations**. Promote only on `min(candidate) > max(incumbent)`. | three runs |
| **C — confirm** | Best survivor vs. incumbent only, `--split holdout --repeats 3`. Read the rendered `## Paired Comparison` block. | `--repeats 3` |

**Stage B must be three invocations, not `--repeats 3`.** Suite rollups are keyed on
`(variant, suite)` alone, so `--repeats` pools every replicate into a single `suite.json`
with one confusion matrix — the per-replicate F1 the gate compares would not exist. The cost
is identical either way.

**Stage C is the one place `--repeats` is correct.** `paired_comparison` fires only for
exactly two variants and averages replicates per row *before* pairing, which is what makes
the paired *t*-test valid. It pairs per-row `weighted_score` — row correctness, not F1 — so
it corroborates the direction; it does not re-test the promotion metric.

And be precise about what Stage B proves. The replicate spread measures agent stochasticity
over a **fixed** set of rows. It does not correct for the survivors having been chosen on
those same rows in Stage A. Stage B bounds run noise; **the holdout is what bounds the fit.**

## Two caveats about what else is in the sandbox

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
- **Two agreeing runs are not evidence.** The `task` misfire reproduced on both splits and
  still turned out to be 2-in-3 variance. Only replicates told the difference.
- **A holdout only confirms failure modes it contains.** The first one here was a flat tie
  because every regression-phrased row sat in the tune half. Fresh rows, authored to test the
  hypothesis rather than to flatter the candidate, are the fix.
- **A holdout confirms direction, not significance.** `a-regression` shipped on F1 1.000 vs
  0.909 on unseen rows while the paired *t*-test read exactly zero. At eleven pairs, across
  three criteria, that block cannot resolve a one-criterion gain — which is a fact about the
  test, not about the change. Report both and say which one you promoted on.

## Next

- [Comparing models](05-comparing-models.md) — the same experiment-variant machinery, applied to models instead of descriptions.
- [Bring Your Own Dataset](../DATASETS.md) — split labels, sampling precedence, and suite-level thresholds in full.
- [Claude Code plugin](../PLUGIN.md) — the other six skills.
