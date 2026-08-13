---
description: >-
  Measure whether a Claude Code skill's body can be improved — build an outcome suite whose
  rows are scenarios, hold activation constant by invoking the skill explicitly, and check
  the instrument works before spending on an A/B. This run stopped at that check.
---

# Tutorial 09 — Optimizing a Skill Body

Tutorial 08 improves a skill's **description**: does it fire when it should? This one asks
the other question. Given that the skill fired, **does it do the job** — and can its body be
made to do it better?

The two are different instruments and the difference is load-bearing. `skill_triggered` is a
binary, cheap, one-turn probe that says nothing whatsoever about the quality of the work that
follows. An outcome suite scores real artifacts and costs a whole task per row.

This walkthrough is a real run against `/coder-eval:ci` in this repository, and it reports
what actually happened: the round promoted nothing, and the first two explanations of *why*
were both wrong. The skill under test was never loaded at all — and the criterion built to
catch exactly that reported success on every row. Reading how that stayed hidden through 24
paid runs is more useful than reading a promotion would have been.

> **Cost.** About 55 agent runs and ~$20 on Sonnet — baselines, a prompt-form calibration, a
> four-arm Stage A, and the probe that finally explained all of it. Stages B and C were never
> reached, and correctly so.

**What you will do:** build an outcome suite whose rows are scenarios against one fixture,
run a baseline, check three things before reading any score, and decide. Here the decision is
no-go, on evidence.

## Why the body track needs a different suite

An activation suite is the **wrong instrument** and must not be reused. It scores engagement.
A skill can engage perfectly and still give terrible instructions, and `skill_triggered`
cannot see the difference.

What the execution track needs is an ordinary coder-eval suite — **one dataset-backed task,
one row per scenario**. That shape is a hard constraint, not a preference: a directory of
separate task files gives the round no rollup to rank on, and makes a later `--split test`
silently re-run the train rows. `/coder-eval:optimize-skill`'s Step 4 carries the mechanism
behind both (which tasks get a `suite.json`, and what `--split` actually filters) and is the
one place it is written down.

The suite used here is [`tasks/skills/ci-outcome.yaml`](https://github.com/UiPath/coder_eval/blob/main/tasks/skills/ci-outcome.yaml),
10 rows split 6 train / 4 test. The labelling is a field on each row plus one line naming it:

```yaml
dataset:
  paths:
    - "ci-outcome-rows.jsonl"
  split_field: "split"
```

```json
{"id": "pr-gate", "split": "train", "expected_skill": "ci", "scenario": "...", "expected_path": ".github/workflows/evals.yml", "expected_snippet": "minimum-task-score"}
```

Label every row or none. A *partly* labelled dataset is the one genuinely bad state: `--split`
keeps the rows that match and silently drops the unlabelled ones, so the run succeeds and every
metric is computed over a smaller suite than the file suggests. `CE035` fails the build on it.

### One fixture, and the variation lives in the prompt

Row substitution reaches `initial_prompt` and `success_criteria` only. It **never** reaches
`sandbox.template_sources`, which is a task-level field. So every row in a suite starts from
the identical repository, and each scenario is a different *request* against that one repo.

This is the constraint that most often has to be designed around. A suite whose scenarios need
different repo shapes — "a workflow already exists" versus "there is none" — is two suites,
not one.

It also means the fixture has to clear whatever precondition the skill checks before it will
act at all. `ci` stops outright on a repository with no `.github/` directory, so the fixture
carries an unrelated `lint.yml`. Without it every row of every arm returns a refusal, the
round ties at the floor, and the result reads exactly like "all my candidates are bad".

The fixture is not neutral scenery — each part of it makes one load-bearing instruction in
`ci`'s body *observable*:

```
templates/ci-outcome-fixture/
  evals/
    hello-world.yaml         <- discovery must find `evals/`, not a guessed `tasks/`
    activation.yaml          <- interpolates $SKILL_SOURCE_PATH
    suite/json-shape.yaml    <- a SECOND depth, so a `**` glob is detectably wrong
    experiments/default.yaml <- makes the extra-args passthrough reachable
  .github/workflows/lint.yml <- REQUIRED, and must not mention coder_eval
  pyproject.toml             <- pins a version, making the `version:` input reachable
```

With tasks at one depth only, a candidate that ignores the "never use `**`" rule would score
identically to one that follows it.

### Criteria are copied to every row

`expand_dataset` copies the *same* `success_criteria` onto every row, substituting
`${row.<field>}` into every string leaf. There is no per-scenario criterion list, so
per-scenario assertions are **parameterized by row fields**:

```yaml
success_criteria:
  - type: "skill_triggered"
    description: "ci engaged for row ${row.id}"
    skill_name: "ci"
    expected_skill: "${row.expected_skill}"

  - type: "file_check"
    description: "the workflow row ${row.id} asked for"
    path: "${row.expected_path}"
    includes:
      - "${row.expected_snippet}"
    suite_thresholds:
      mean: 0.7
      completion_rate: 1.0
```

Keep anything **constant** out of the gated criterion. `file_check` scores `found / total`
over its `includes`, so folding a universal check into it puts a fixed contribution in every
row of every arm, compressing the variance the gate reads. This suite asserts the action
reference as its own separate criterion for that reason.

### Making the skill reachable at all

The evaluated agent runs in a fresh sandbox holding none of your files, so it is offered no
skills unless the task says where they live. The suite mounts them through `agent.plugins`,
with the location in an environment variable so the committed file stays portable:

```bash
export SKILL_SOURCE_PATH="$(pwd)/plugins/coder-eval"
```

That path must be a **plugin root** — a directory holding a `skills/` subdirectory, so the
skill sits at `skills/<name>/SKILL.md`. For `.claude/skills/my-skill/SKILL.md` the root is
`.claude`, **not** `` `.claude/skills` ``. Pointing one level too deep loads nothing at all,
silently.

An unset or wrong path is only a **warning**. The skill is simply absent, every row scores
zero, and the output is indistinguishable from a skill whose body is terrible — which on this
track is the reading you are least equipped to doubt. Confirm the engagement criterion passes
before trusting any low score.

## The skill was never loaded, and the suite said it was

This is the finding the round actually produced, and it invalidates every number above it
that looked like a measurement of `ci`'s body.

`ci` sets `disable-model-invocation: true`. That flag keeps a skill out of the listing the
model chooses from — but it also makes the `Skill` tool **refuse the call outright**:

```
<tool_use_error>Skill coder-eval:ci cannot be used with Skill tool
due to disable-model-invocation</tool_use_error>
```

**24 of 24 Skill calls across the four A/B arms failed exactly this way**, `result_status:
"error"` on every one, and no row read the `SKILL.md` off disk either. The body never entered
the context. What the agent produced came from its own knowledge of GitHub Actions — plausible
enough that no criterion downstream looked wrong.

And the criterion built to catch precisely this reported `yes` every time. `skill_triggered`
read the call's *parameters* and never its *result*, so an attempt the tool had refused scored
as engagement. That one omission is why four arms differing only in `ci`'s body tied
**exactly** on every criterion: none of them had ever seen the body they differed in.

The tell was visible in the output the whole time:

```
body NOT loaded:  uses: anthropics/coder-eval-action@v1   <- does not exist
body loaded:    - uses: UiPath/coder_eval@v0             <- what the body specifies
```

Remove that one frontmatter line from the arm's snapshot and the call succeeds, the body
loads, and the same rows score 1.000 — with the action reference right.

### What to do about it

**Fix it in the snapshot, not the suite.** The arms are already modified copies of the plugin,
so delete the `disable-model-invocation:` line from the target skill in **every** arm,
incumbent included. That reproduces what a real user gets — typing the slash command *does*
inject the body — while keeping the arms identical in everything but the text under test.

**And do not trust a slash command to invoke anything.** Nothing in coder-eval expands one; it
arrives as plain text the model may act on. Measured across the six train rows, by how often
the model even *attempted* the call: slash form alone 3/6, a prose instruction alone 5/6, slash
plus an explicit imperative 6/6. Pair them:

```yaml
initial_prompt: |
  Use the `coder-eval:ci` skill to handle this request. Invoke it with the Skill
  tool and follow it before writing anything.

  ${row.scenario}
```

That makes the model attempt the call every time. Whether the call *succeeds* is the separate
question above — and the two failing independently is exactly why the engagement criterion has
to be read before anything else, and has to be one that checks the result.

## The tool policy still matters — but read this one carefully

Before the loading bug was understood, this looked like the round's big finding: with
sub-agent delegation available every row scored 0.333, and denying it moved rows to 1.000.
The numbers are real; the causal story was wrong. **The body was not loading in either
case** — what changed was whether the main agent did the work itself, and on the
then-leaky rows the prompt supplied much of the answer.

The guidance survives on its own merits, which are about *observability* rather than score:

```yaml
agent:
  allowed_tools: ["Read", "Glob", "Grep", "Write", "Edit", "Bash", "Skill"]
  disallowed_tools: ["Agent", "Task"]
```

Left available, the model sometimes answers by dispatching a sub-agent that engages the skill
in the *child*, where the parent's trajectory never records it — so the engagement criterion
reads `no` for a row where the skill genuinely ran. That is the mirror image of the bug above,
and both corrupt the same signal. **An allowlist cannot express this**: `Agent` and `Task`
remain available whatever `allowed_tools` says, so denial is the only lever.

`Skill` in the allowlist is documentation rather than effect — the tool is available either
way. It is listed so a reader can see what the mechanism under test requires.

## Check four things before reading any score

```bash
export SKILL_SOURCE_PATH="$(pwd)/plugins/coder-eval"
coder-eval run tasks/skills/ci-outcome.yaml --split train -D run_limits.stop_early=false
```

In order, because each one makes every number below it meaningless if it fails:

1. **The resolved row count is what you expect.** A mistyped split is reported as a skipped
   task and the run still exits 0 — a green run of zero rows.
2. **Engagement passes on every row.** Not most rows. A row where the skill never ran measures
   nothing, and Stage B's own promotion rule requires the skill to have engaged on every
   scored row.
3. **The Skill calls actually succeeded.** This is the one this round learned, and it is not
   the same as check 2. Grep a `task.json` for the tool's `result_status`; an errored call
   means the body never loaded, whatever the criterion says. On an older criterion that
   distinction was invisible, so it is worth confirming directly the first time you run a
   suite:

   ```bash
   jq -r '.iterations[].commands[] | select(.tool_name=="Skill")
          | "\(.parameters.skill) -> \(.result_status)"' <run>/<variant>/<suite>/<row>/00/task.json
   ```

4. **`completion_rate` is 1.0.** An errored row is *excluded* from the aggregate rather than
   scored, so it never appears as a low number — only as a denominator that shrank. A 300s
   `turn_timeout` did exactly this here; rows commonly run 100–200s, and one exceeded it.

Run against the plugin as shipped, this suite fails check 3 on every row — and, with the
criterion fixed, check 2 as well. Mount a snapshot with the target skill's
`disable-model-invocation:` line removed and all four pass, at which point the numbers mean
what they appear to mean.

## What an arm looks like

Stage A ran — four arms, 24 rows — and told us nothing, for the reason above. The snapshot
shape is still worth having, because it is the part most easily got wrong. Each arm is a copy of the **whole plugin root**, not of one skill:

```
.optimize-skill/ci/1-incumbent/
    .claude-plugin/plugin.json   <- copy it if the source had one
    skills/
        ci/SKILL.md              <- the arm's ONE varying part
        analyze/SKILL.md         <- every sibling, copied unchanged
        check-skill/SKILL.md
        ...                      <- all seven
    reference/                   <- and everything else the root held
```

`plugin.json` is the trap. Without a manifest the namespace defaults to the *directory name*,
so a manifest-less arm is namespaced after its own slug — `1-incumbent:ci` competing against
`1-a-widen-vocabulary:ci` — and the arms then differ in their command name as well as in the
text under test. The siblings matter for the same reason: a variant's `plugins` block
*replaces* the task's, so this directory is the only skill source the arm gets.

Add `.optimize-skill/` to `.gitignore` before writing any of it; a round is several copies of
the whole plugin per arm.

And since the arm is already a modified copy, this is where the reachability fix belongs:
delete the target skill's `disable-model-invocation:` line here, in every arm.

## The go/no-go, and what it actually turned on

The baseline costs 6 runs; the three A/B stages cost ~84 more. Decide here, not after.

The first reading of this round was that engagement was flaky at 50–80% and that the rows
where `ci` did engage were at a ceiling — two independent reasons not to spend. Both were
artifacts of the criterion bug. Engagement was never 50–80%; it was **zero**. The varying
figure came from counting refused calls plus the occasional row that genuinely read a
`SKILL.md` off disk.

What survives is the conclusion, arrived at properly: **nothing to promote — this is a
ceiling.** With the body loaded, all six train rows score 1.000 on all three criteria. `ci`
emits the per-depth globs with the reason attached, the real action, the version pin, the
experiment passthrough, both runtime prerequisite steps, and both hardening lines:

```yaml
      - uses: actions/checkout@v6
        with:
          persist-credentials: false

      # The action installs no coding-agent runtime — provide it here.
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
      - run: npm install -g @anthropic-ai/claude-code

      - uses: UiPath/coder_eval@v0
        with:
          version: "0.9.6"
          # Two explicit per-depth globs, with the reason kept next to them.
          tasks: evals/*.yaml evals/suite/*.yaml
          extra-args: "-e evals/experiments/default.yaml"
          minimum-task-score: "0.7"
```

So the three candidates were solving a problem that did not exist. The hallucinated action,
the missing runtime steps, the dropped hardening lines — every defect that looked like
headroom was the model working without the body, not the body instructing it badly.

A ceiling is a real answer, and the method's response is to stop rather than spend 84 runs
chasing a number the gate cannot reach. To optimize `ci` from here the honest next move is
**harder rows** — scenarios these six do not reach — not a looser gate and not another round
of candidates.

### The number this round never got to read

Had the baseline been trustworthy, the gate would have been a two-variant experiment at
`--repeats 3`, and the verdict would have come from the `## Paired Comparison` block the
experiment reporter renders — mean difference, 95% confidence interval, Cohen's *d*, a paired
*t*-test.

**Read its sign off the header, every time.** The block renders as
`**Paired mean diff (<first declared variant> - <second declared variant>)**` and subtracts in
**variant declaration order** — not incumbent-minus-candidate, and not better-minus-worse. With
`incumbent` declared first, as it usually is, **a candidate win reads negative**. Quote the
header next to the figure rather than resolving the direction from memory: read backwards, it
promotes the arm that lost, and every later number in the ledger then agrees with the mistake.

The block also fires **only** for exactly two variants. Since no flag filters an experiment's
arms, each stage needs its own file — `round1-triage.yaml` for the wide triage,
`round1-gate.yaml` and `round1-confirm.yaml` for the two-arm stages. Re-passing the triage file
at the gate costs several times the budgeted runs and renders no paired block at all.

The method offers three responses to a ceiling, in order of preference. **Harden the rows** —
already done once here (two weak snippets strengthened, a universal action-reference criterion
added, one scenario reworded); engaged rows still score 1.000, so going further means probing
behaviour the body does not spell out, which is a suite redesign. **Switch subject** — a
repeat of the whole fixture-and-suite phase, and it would not help, because the engagement
instability is a property of explicitly-invoked skills rather than of `ci`. **Report the null
and stop** — taken.

Spending anyway to produce a nicer tutorial would be exactly the fitting-to-a-narrative the
whole method exists to prevent.

## What to take away

- **Read the engagement criterion before anything else, and make sure it checks the tool
  result.** The version used here counted a *refused* Skill call as engagement. Everything
  downstream — the scores, the arm comparison, the diagnosis, two rounds of conclusions — was
  built on that one unchecked field.
- **A `disable-model-invocation: true` skill is unreachable in a run.** The Skill tool refuses
  it and nothing expands a slash command, so the body never loads. Delete that line in every
  arm's snapshot; it reproduces what a real user's slash command does.
- **An agent given a plausible request and no skill produces plausible output.** That is what
  makes this failure so quiet: nothing errors, nothing looks empty, and the artifact is wrong
  only in the details the missing body would have supplied — here, an action reference that
  does not exist.
- **Four arms tying exactly is a bug report, not a result.** Bodies that differ should produce
  *some* variance. Perfect agreement means they are not being distinguished.
- **The rest of the method held up.** One dataset-backed task, one row per scenario, one
  fixture clearing the skill's preconditions, criteria parameterized by row, the tool policy
  pinned — all of that was sound. It was the reachability underneath it that was not.
- **Scenarios describe the situation; the body supplies the method.** A row naming the thing it
  scores is measuring the prompt. `CE036` now fails the build on the verbatim form.
- **A null result is still a result — once you know which null you have.** "The candidates did
  not beat the incumbent" and "the skill never ran" look identical in the numbers and mean
  opposite things.

## Next

- [Tutorial 08 — Optimizing a Skill Description](08-optimizing-a-skill.md) — the activation
  track, and another honest stop.
- [Plugin guide](../PLUGIN.md) — every skill the plugin ships.
