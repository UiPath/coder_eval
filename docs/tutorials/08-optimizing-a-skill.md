# Optimizing a skill description

`check-skill` tells you whether a skill triggers. This tutorial covers the next question:
**can its description be made to trigger better, and how would you know?**

The honest answer is usually "measure first, and often the answer is don't change it." This
walkthrough is a real run against a real skill in this repository, reported with the numbers
it actually produced — including the part where the first attempt measured nothing at all.

**What you will do:** build an activation suite for `lint-tasks`, split it into tune and
holdout rows, baseline it, read the confusion matrix, and decide. About 20 agent runs on
Sonnet if the baseline settles the question, as it does here; several hundred if it does not
and you run the three A/B stages.

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
fired — apparently annexing setup work — dropping its precision to 0.667 on tune and 0.500
on holdout. It reproduced on **both splits**, which is normally the signal that a finding is
real.

It did not survive a third run. The table above shows `task` at precision 1.000, on
byte-identical prompts: `expected_skill` is a dataset label the criterion reads and the
agent never sees, so nothing about the rows changed between those runs.

**That is the whole reason the promotion gate demands non-overlapping replicate ranges.**
A single run — or even two agreeing runs — can show a clean, plausible, entirely
reproducible-looking effect that is just variance. Had this been a candidate description
rather than an incumbent's quirk, one run would have "proved" an improvement worth shipping.
Report what replicates; treat anything else as a hypothesis.

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

The holdout has no `analyze`-owned row, so that column is blank here rather than zero;
`analyze`'s recall gap lives in the tune split alone. Worth fixing in the suite before the
next round, since a finding you can only observe on the half you tune against is the half
you cannot confirm.

## What the three stages would have looked like

They did not run here, because the incumbent was already perfect. When your baseline *does*
show failures, the loop continues like this:

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

## One caveat about the sandbox

One distractor row engaged a skill named `review` — not from this plugin at all, but from
the operator's own installed skills. Activation is a competition for a shared listing
budget, so whatever else is installed on the machine running the suite is part of the
experiment. If you need a clean-room measurement, isolate the environment; if you want a
realistic one, note what was installed alongside your results.

## What to take away

- **Check that recall is non-zero before you believe any low score.** A misconfigured path
  looks exactly like a bad description and costs the same to produce.
- **A ceiling baseline means stop.** The most valuable thing this loop does is decline to
  spend money it cannot convert into information.
- **Report negative results plainly.** "Three candidates, none separated beyond run-to-run
  noise" is a real finding. So is "the trim was safe."
- **In a multi-skill repository, look at the sibling matrix.** That is where the headroom
  usually is — and a request that goes to the wrong skill tells you more than one that goes
  nowhere.

## Next

- [Comparing models](05-comparing-models.md) — the same experiment-variant machinery, applied to models instead of descriptions.
- [Bring Your Own Dataset](../DATASETS.md) — split labels, sampling precedence, and suite-level thresholds in full.
- [Claude Code plugin](../PLUGIN.md) — the other six skills.
