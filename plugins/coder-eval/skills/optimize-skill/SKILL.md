---
description: Improve a skill description from activation-suite results — propose rewrites, A/B them as experiment variants, promote only what wins on a held-out split. Use when a skill triggers too rarely or fires on the wrong requests.
disable-model-invocation: true
allowed-tools: ["Read", "Glob", "Grep", "Write", "Bash"]
---

# Optimize a skill description

`/coder-eval:check-skill` answers *does this skill trigger?* This answers the next
question: **can the description be made to trigger better, and how would you know?**

The method is an A/B test, not a rewrite. Candidate descriptions become experiment
variants, every arm runs the same labelled suite, and a candidate is promoted only when it
beats the incumbent by more than the run-to-run noise — then survives rows it was never
tuned on. Most rounds promote nothing. That is a real result, not a failure.

The user's request is: `$ARGUMENTS`

**Every row is a full agent run.** This skill spends real money across three stages. State
the projected run count and ask before each one.

## Step 1 — Confirm coder-eval is installed

Run `coder-eval --version`. Every later step shells out to it.

If it is missing, follow `${CLAUDE_PLUGIN_ROOT}/reference/cli-setup.md`: offer the install,
**ask before running it**, and confirm afterwards. Never install unprompted. That reference
also covers whether this project pins a version and what to do when the installed one
disagrees.

## Step 2 — Locate the skill, and check it can be optimized at all

`$ARGUMENTS` may be a path to a `SKILL.md`, a path to the skill's directory, a skill name,
or empty. Empty: glob `.claude/skills/*/SKILL.md` and `**/skills/*/SKILL.md`; one match →
use it, several → ask, none → say so and stop.

The **bare skill name is the directory name** containing `SKILL.md`. Locate the eval tree
by following `${CLAUDE_PLUGIN_ROOT}/reference/repo-layout.md` — discover it, never assume a
path.

Two hard stops, both before anything is spent:

- **No frontmatter `description`.** That is the finding. A skill with no description can
  never be model-invoked, so there is nothing to optimize and a suite would score zero
  recall by construction.
- **`disable-model-invocation: true`.** The description never enters the activation
  decision for such a skill, so an activation suite measures nothing and a rewrite round is
  pure spend. Say so and stop. What *does* drive discovery for an explicit-invocation skill
  is its name, its body, and the documentation telling users the command exists — offer
  those instead.

Read the incumbent `description` and keep it in front of you. It is the only thing this
skill changes.

## Step 3 — Find its activation suite

Glob the eval tree for a task carrying a `skill_triggered` criterion naming this skill.

**No suite → stop and point at `/coder-eval:check-skill`.** Do not author one here; that
skill owns row design, and a suite written as a side effect of optimizing is a suite fitted
to the thing it is meant to judge.

**Suite adequacy is `check-skill`'s to define** — it owns the sizing rule, and this skill
does not restate its numbers. But state the part that belongs here: **a split halves each
side.** A suite sized for a single one-shot check is undersized for an optimization loop,
because you are now measuring a *difference* between arms rather than one level, and the
tune half is all you get to develop against. Roughly double what a one-shot check would
want. If the suite is too small to gate on, say so and hand back rather than producing a
confident number from four rows.

## Step 4 — Require split labels

The suite's rows must carry split labels, and the run selects one with `--split`.

Without a holdout, every reported improvement is fitted to the sample you tuned on: with
3–5 candidates scored on the same rows, the winner is partly whichever one happened to suit
those rows. The holdout is what separates a real gain from that.

- **Rows already labelled** → carry on.
- **Rows unlabelled and the suite is big enough to halve** → explain the discipline and
  offer to add `split_field` plus a `tune`/`holdout` label per row, keeping both polarities
  on both sides.
- **Rows unlabelled and the suite is too small to halve** → do not carve a token two-row
  holdout out of an already scarce sample; it confirms nothing and reads as if it did. The
  better trade is to keep every row in `tune` and author **fresh holdout rows at promotion
  time**, when you know which single candidate needs confirming. Offer that.

  If you take that route, note what follows: with every row labelled `tune`, a later
  `--split holdout` matches nothing, which is reported as a skipped task and a green run of
  zero rows. Write the fresh rows first, then run Stage C.

**Refuse to run a proposal round with neither.** Say why rather than proceeding quietly.

## Step 5 — Baseline

```bash
coder-eval run <suite> --split tune -D run_limits.stop_early=false
```

**Check the resolved row count, not just the exit code.** A mistyped split name (`--split
holdou`) is reported as a skipped task and the run still exits 0 — a green run of zero rows.
If the count is zero or lower than the tune rows you expect, that is a wiring problem, not a
result.

On `stop_early`: a suite that arms it can pass-stop a run before a later sibling misfire is
observable, so authoritative precision/recall needs a full run. Say plainly that a stock
`check-skill` suite arms nothing, so on that suite the flag changes nothing — it is
insurance against an armed suite, not a fix for anything already there.

## Step 6 — Diagnose

Read `<run>/<variant>/<suite>/suite.json`; its shape is documented in
`${CLAUDE_PLUGIN_ROOT}/reference/run-layout.md`.

Take the aggregate whose criterion names **this** skill. `details.confusion` and
`details.per_label` give you the *counts* — how many false negatives (rows that should have
engaged it and did not) and how many false positives (rows that engaged it and should not
have).

**Counts are not enough here; you need the rows.** Neither field carries a row identity.
For that, read `failed_samples[]` in the same `suite.json` (each entry names its `row_id`,
though the list is capped) and, when a row you need is not in it, the per-row
`<run>/<variant>/<suite_id>/<row_id>/<NN>/task.json`. Pair each failing row with the prompt
that produced it before drawing any conclusion — a hypothesis about wording that cannot
name the requests it explains is not testable.

**Group failures into named hypotheses, each pointing at specific rows.** *"It misses
oblique requests because the description names the operation and never the symptom."*
*"It fires on 'audit my dependencies' because the description claims audit vocabulary
generally."* Not "improve the wording" — a hypothesis you cannot phrase as a claim about
specific rows is not one you can test.

**Sibling-owned rows.** A suite may carry rows whose `expected_skill` names a *different*
skill in the same repository, with one `skill_triggered` criterion stacked per skill —
which yields a per-skill confusion matrix from the same traces. That turns "this candidate
misfires" into "this candidate is stealing the other skill's requests", which is the
finding that actually tells you what to write. If the repository has sibling skills and the
suite has no sibling rows, say the sibling half of the gate cannot be evaluated, and offer
to hand back to `/coder-eval:check-skill` to add them.

## Step 7 — Propose 3–5 candidates

One candidate per hypothesis. Each candidate is a snapshot of **the whole skills
directory**, not of one skill:

```
.optimize-skill/<skill>/<round>-<slug>/
    <skill-name>/SKILL.md      <- the candidate: description rewritten
    <sibling-a>/SKILL.md       <- every sibling, copied unchanged
    <sibling-b>/SKILL.md
```

The round-slug directory is the arm's replacement for the user's `.claude/skills`, so it
must contain everything that directory contained. **Copy the siblings in unchanged.** Two
things break if you snapshot only the target skill, and both fail silently at full cost:

- Sibling `skill_triggered` criteria would observe `no` on every row in every arm, because
  the sibling is not in the sandbox at all. The sibling-precision half of the gate would
  then "pass" by measuring nothing.
- The listing the model chooses from would contain one skill instead of the real set.
  Activation is a *competition* between descriptions; a description tested alone is tested
  against a rival it will never face.

Each candidate differs from the incumbent **only in the target skill's frontmatter
`description`**. Body edits are out of scope here — say so if asked, and note the body does
not drive activation anyway, so changing it would not be measured by this suite.

Snapshot the incumbent the same way (`<round>-incumbent/`), siblings included, so every arm
is mounted by the identical mechanism and the comparison has no confound.

## Step 8 — Materialize as an experiment

One variant per candidate, plus `incumbent`. **Reachability uses `agent.plugins`, the same
mechanism the activation template itself uses** — `path` is the directory *containing* the
skill's directory, i.e. the round-slug directory, written **absolute**:

```yaml
experiment_id: "optimize-my-skill-round-1"
defaults:
  run_limits:
    stop_early: false
variants:
  - variant_id: incumbent
    agent:
      plugins:
        - type: "local"
          path: "/abs/path/to/.optimize-skill/<skill>/<round>-incumbent"
  - variant_id: cand-a-widen-vocabulary
    agent:
      plugins:
        - type: "local"
          path: "/abs/path/to/.optimize-skill/<skill>/<round>-a-widen-vocabulary"
```

`experiment_id` is required and must be kebab-case — an experiment without one fails to
load. Name it for the skill and the round.

Four facts that decide whether this measures anything:

- A variant's `plugins` block **replaces** the task's rather than stacking with it. That is
  what makes one-variant-per-candidate work — an arm never mounts two copies of the skill.
- **Because it replaces, the task's own block is gone in every arm.** Whatever that block
  exposed — typically the user's whole skills directory via `$SKILL_SOURCE_PATH` — is not
  mounted. That is exactly why step 7's snapshot has to carry the siblings: the variant
  path is the *only* skill source the arm gets.
- Plugin paths resolve against the **process working directory**, not the task file's
  directory. A relative path silently points elsewhere the moment the user runs from a
  subdirectory, so write absolute paths.
- A wrong `path` leaves the skill unreachable and **every arm scores recall 0.0**. In a
  one-shot check that reads as "broken skill"; in an optimization loop it reads as "all my
  candidates are bad", which is far more convincing and completely wrong. An unset
  environment variable in a path is only a **warning**, so a silent zero is the expected
  symptom of this mistake.

**The wiring check: confirm the `incumbent` arm reproduces the baseline's F1 before
trusting any comparison.** If it does not, stop — the mounting is wrong and nothing
downstream means anything.

## Step 9 — Three stages, and the gate

State the projected run count before each stage and ask. With N candidates, S survivors,
M<sub>tune</sub> tune rows and M<sub>holdout</sub> holdout rows: Stage A is
`(N+1) × M_tune` runs, Stage B is `3 × (S+1) × M_tune`, Stage C is `6 × M_holdout`.

**Every stage runs the suite through the experiment.** The suite is the positional
argument; the experiment carrying the arms is passed with `-e`. Passing the experiment file
positionally instead would treat it as a task, which resolves to a skipped task and a green
run of zero rows.

### Stage A — triage (cheap)

All candidates plus the incumbent, **one** invocation, `--split tune`:

```bash
coder-eval run <suite> -e <experiment> --split tune
```

Rank by the target label's F1 from each variant's `suite.json`; discard anything at or
below the incumbent.

This decides nothing — it only narrows. Replicate pooling is irrelevant here because
nothing is being gated on.

### Stage B — the gate (replicates)

Incumbent plus survivors, `--split tune`, **invoked three separate times** — three
`coder-eval run` commands, **not** `--repeats 3`:

```bash
coder-eval run <suite> -e <experiment> --split tune --run-dir <runs>/round<N>-gate-1
coder-eval run <suite> -e <experiment> --split tune --run-dir <runs>/round<N>-gate-2
coder-eval run <suite> -e <experiment> --split tune --run-dir <runs>/round<N>-gate-3
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
  untouched, so on a suite where the sibling never misfires precision stays pinned at
  exactly 1.0 no matter how many requests are stolen. Gating on precision here would be
  gating on a constant.
- **Print per-invocation F1 and confusion counts for every arm.** The verdict is never
  reported without the numbers behind it.

Why replicates rather than a fixed threshold: each arm's spread measures the noise floor
for *this* suite at *this* size. A hardcoded "≥ 0.05 F1" would be far too lax on a six-row
suite and needlessly strict on a forty-row one. **Do not introduce one.**

**Be precise about what this bounds, because it is easy to claim more.** The replicate
spread measures agent stochasticity over a *fixed* set of rows. It does not measure
row-sampling variance, and it does not correct for the fact that the survivors were already
chosen on these same tune rows in Stage A — so with S survivors each tested independently,
some separation by luck is expected. Stage B bounds run noise. **The holdout is what bounds
the fit**, and it is why Stage C is not optional. Report the gate as "separated beyond
run-to-run noise on the tune rows", never as "proven better".

### Stage C — confirm on holdout

Only the best candidate that already passed Stage B, as a **two-variant** experiment
(incumbent + that candidate) at `--split holdout --repeats 3`.

Here `--repeats` is correct and required. The experiment reporter renders a
`## Paired Comparison` block in `experiment.md` — mean difference, 95% confidence interval,
Cohen's *d*, and a paired-*t* p-value — but it fires **only** for exactly two variants, and
it averages replicates per row *before* pairing, which is precisely what pooling breaks in
`suite.json` and exactly what is wanted here.

Report that block verbatim alongside the holdout F1s, and state its limit honestly: it
pairs per-row `weighted_score` — row-level correctness, an accuracy-flavoured quantity —
**not** F1. F1 remains the promotion metric; the paired block corroborates the direction on
the paired rows, it does not re-test the promotion metric.

Require the F1 direction to reproduce on holdout. Do **not** require replicate separation
there — a holdout is usually smaller and the separation usually weaker — and say that,
rather than implying a stronger result than was obtained.

## Step 10 — Ledger

Append to `.optimize-skill/<skill>/history.json`: round, candidate slug, hypothesis,
per-invocation tune F1, holdout F1, the paired-comparison line, sibling precisions, verdict,
and the run ids. Append-only — never rewrite an earlier round. An existing directory means
read `history.json` first and continue the numbering.

## Step 11 — Present a diff; do not apply it

Show the incumbent description against the promoted one and let the user apply it. This
skill writes candidate snapshots, never the live `SKILL.md`.

**Report a negative result plainly when nothing promotes.** That is the common outcome. The
honest version — "three candidates, none separated from the incumbent beyond run-to-run
noise, here are the numbers" — is worth more than a promotion that will not reproduce.

## Step 12 — Stop rule

Stop after two consecutive rounds that promote nothing. Continuing past that is fitting to
the tune set, and the holdout will eventually stop catching it.
