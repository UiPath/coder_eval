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

This walkthrough is a real run against `/coder-eval:ci` in this repository. It reports what
actually happened, which is that **the A/B never ran**. The baseline could not be trusted, and
the method says that is a stop rather than a starting point. Reading why is more useful than
reading a promotion would have been.

> **Cost.** 24 agent runs and $7.47 on Sonnet, all of it baselines. The A/B stages the round
> did not spend would have been ~84 more runs and ~$36.

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

## The slash form, and what it does not guarantee

`ci` sets `disable-model-invocation: true`, so it is never offered to the model. Asking in
prose returns "no such skill is available" and the row measures nothing. The **slash form** is
the only mechanism that reaches it:

```yaml
initial_prompt: |
  /coder-eval:ci

  ${row.scenario}
```

**It is not, however, reliable — and this is what stopped the round.** Measured across four
runs of the 6-row train split, engagement landed at 4/6, 4/5, 3/6 and 4/6, failing on
*different* rows each time. Three ways it slips, all observed and all silent:

- the model answers the command by **dispatching a sub-agent**, which reads the skill in the
  child, so no `Skill` call ever reaches the parent stream;
- it **ignores the command** and simply does the work itself, emitting no `Skill` call;
- the scenario's own wording **routes it to a sibling skill**. One row asked for a schedule
  "so we find out if a skill quietly stops triggering" — that is `check-skill`'s subject
  matter, and the phrasing beat the explicit command.

One subtlety when reading the result: `skill_triggered` counts **reading the skill's
`SKILL.md`** as engagement, not only a `Skill` call. A row here reported engaged while the
command it actually issued named a different skill. Treat the criterion as necessary, not
sufficient.

## The tool policy is not a detail — it decided the result

The first baseline scored **0.333 on every single row** — engagement only, nothing else.
It looked like a uniformly bad skill body. It was not.

With sub-agent delegation available, the model dispatched an `Agent` that did the work
*without* following the skill. The workflows it produced were plausible and wrong in four
ways at once:

- `uses: anthropics/coder-eval-action@v1` — an action that does not exist;
- a recursive `**` glob in the `tasks:` input, which the body explicitly forbids because the
  action expands that value with `globstar` off, so it degrades to one level and silently
  drops every task above it;
- `min-score:` where the real input is `minimum-task-score:` — GitHub ignores unknown inputs,
  so the score floor would simply never apply;
- no Node or Claude CLI prerequisite steps, and no `persist-credentials: false`.

Denying delegation changed the same suite's engaged rows from 0.333 to **1.000**:

```yaml
agent:
  allowed_tools: ["Read", "Glob", "Grep", "Write", "Edit", "Bash", "Skill"]
  disallowed_tools: ["Agent", "Task"]
```

Two things worth taking from that. First, `Skill` was missing from the original `allowed_tools`
even though it is the mechanism under test — it worked anyway, because `Skill` is available
regardless, which is exactly why the omission was invisible. Second, **an allowlist cannot
suppress delegation at all**: `Agent` and `Task` remain available whatever `allowed_tools`
says, so `disallowed_tools` is the only lever.

## Check three things before reading any score

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
3. **`completion_rate` is 1.0.** An errored row is *excluded* from the aggregate rather than
   scored, so it never appears as a low number — only as a denominator that shrank. A 300s
   `turn_timeout` did exactly this here; rows commonly run 100–200s, and one exceeded it.

The final baseline:

| Row | Engaged | Snippet | Action ref | Weighted |
| --- | --- | --- | --- | --- |
| `pr-gate` | no | 0.0 | 0.0 | 0.000 |
| `regression-least-privilege` | yes | 1.0 | 1.0 | 1.000 |
| `regression-node-runtime` | yes | 1.0 | 1.0 | 1.000 |
| `schedule-weekly` | yes | 1.0 | 1.0 | 1.000 |
| `skill-source-path` | no | 1.0 | 0.0 | 0.333 |
| `tasks-two-depths` | no | 1.0 | 0.0 | 0.333 |

`completion_rate` 1.0, `average_weighted_score` 0.611, engagement **3/6**.

## What an arm would have looked like

The round stopped before Stage A, but the incumbent snapshot was built, and its shape is the
part most easily got wrong. Each arm is a copy of the **whole plugin root**, not of one skill:

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

## The go/no-go, and why this one is no-go

The baseline costs 6 runs; the three A/B stages cost ~84 more. Decide here, not after.

**Both no-go conditions held at once.**

*The instrument is unreliable.* At 50–80% engagement, a fifth to a half of every arm's sample
measures the absence of the thing under test. Replicates do not fix that — they average a
mixture more precisely.

*And there is no headroom where it is reliable.* On rows where `ci` did engage, the score is
1.000. The emitted workflow carries the per-depth globs *with the reason attached*, the real
action, the version pin, the experiment passthrough, both runtime prerequisite steps, and both
hardening lines (`persist-credentials: false` below, and a `permissions: contents: read` block
the excerpt omits):

```yaml
# Abridged: junit-path, step-summary, the env: passthrough and the job's
# `permissions: contents: read` block are omitted. Every value shown is verbatim.
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

That is the same skill, on the same suite, that scored 0.333 everywhere one setting earlier.

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

- **The body track needs an outcome suite: one dataset-backed task, one row per scenario.**
  Separate task files produce no rollup to rank and make `--split test` silently re-run train.
- **One fixture serves every row.** Variation lives in the prompt, and the fixture must clear
  the skill's own preconditions or every arm ties at the floor.
- **Hold the tool policy constant, and size it deliberately.** Here it was the difference
  between 0.333 and 1.000 on identical rows. Denying sub-agent delegation needs
  `disallowed_tools`; an allowlist cannot express it.
- **Engagement is a gate on the baseline, not a diagnostic.** Below 1.0 on every row, stop and
  fix the suite. And it is necessary, not sufficient — reading the skill's file counts.
- **Read `completion_rate` before any effect.** An errored row vanishes from the denominator
  rather than scoring low.
- **A null result is a result.** $7.47 of baselines bought a firm answer and avoided ~$36 of
  numbers that would have meant nothing.

## Next

- [Tutorial 08 — Optimizing a Skill Description](08-optimizing-a-skill.md) — the activation
  track, and another honest stop.
- [Plugin guide](../PLUGIN.md) — every skill the plugin ships.
