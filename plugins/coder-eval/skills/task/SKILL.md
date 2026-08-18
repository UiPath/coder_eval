---
description: Turn a natural-language description into coder-eval task YAML — minimal prompts, weighted criteria that check output content, validated with `coder-eval plan`. Use when the user wants to write, add, or generate an evaluation task.
allowed-tools: ["Read", "Glob", "Grep", "Write", "Bash"]
---

# Author a coder-eval task

You are writing coder-eval task YAML. The user's request is: `$ARGUMENTS`

If `$ARGUMENTS` is empty, ask what the task should test. Do not invent a subject.

Good tasks use simple prompts: state the goal and the expected output, then let the
agent work out the approach. A single request can produce **several** task files —
"create tasks for all the registry subcommands" means one task per subcommand.

## Which mode

**Default mode** is everything below: one task file per thing being tested.

**Outcome-suite mode** produces a single dataset-backed suite that measures whether a *skill's
body* produces the right outcomes — the execution track's instrument. Take this branch when the
request names a **skill under test** and asks for an outcome, execution or A/B suite for it, or
when it arrives from `/coder-eval:optimize-skill`. If it names a skill but not a suite, or a suite
but not a skill, **ask which is meant** — the two modes differ in how many files you write, so a
wrong guess is not a detail the user can overlook.

Outcome mode **supersedes the one-file-per-task guidance in two places**, and this is the whole
structural difference between the modes:

- the paragraph directly above ("A single request can produce **several** task files"), and
- Step 4's "One file per task, named after the task ID"

become **one dataset-backed task, one ROW per scenario**. The reason is mechanical, not stylistic:
`suite.json` is written only for tasks the dataset expander touched (rollups group on `suite_id`,
which nothing but the expander sets), so a directory of separate task files produces **no rollup**
and an optimization round has nothing to rank on. And `--split` filters dataset **rows** — a task
with no `dataset:` block is untouched by it, so `--split test` over separate files silently re-runs
the train rows, at full price, with no error anywhere.

Every other step below applies in both modes. The outcome-mode additions are marked.

## Step 1 — Understand the request, and check the CLI is there

Run `coder-eval --version` first. Steps 6 and 7 both shell out to it, and finding that out
*after* writing several task files means the user gets a bare `command not found` with
nothing to act on. Installing this plugin did not install the CLI.

If it is missing, follow `${CLAUDE_PLUGIN_ROOT}/reference/cli-setup.md`: offer the install,
**ask before running it**, and confirm with `coder-eval --version` afterwards. Never install
unprompted, and do not write any task files if the user declines.

That reference also covers the other half of the version check — whether this project pins a
coder-eval version, and what to do when the installed one does not match it.

Then establish:

- **What is being tested** — which tool, SDK, CLI, skill, or capability?
- **How many tasks** — one operation, or several?
- **Difficulty** — smoke, basic, or intermediate?
- **Dependencies** — network, packages, starter files, external services?

State any assumptions you make rather than silently picking.

## Step 2 — Look at what already exists

Find the repository's task tree by following
`${CLAUDE_PLUGIN_ROOT}/reference/repo-layout.md`, and say what you resolved. If a task
already covers this ground, say so and offer to modify it instead of adding a
near-duplicate.

**Repo-local convention beats anything bundled with this plugin — where the two
disagree, the repo wins.** Before writing, read what the repository declares about task
authoring: its own contributor or convention documents, a task template if it ships one,
and a few neighbouring tasks. Adopt what you find — naming, tags, thresholds, weights,
where files go — and **say in your report which conventions you adopted**, so the choice
is visible rather than implied.

Two limits on that:

- **A repository that declares nothing** leaves the bundled rubric as the whole answer.
  Precedence is about deferring to a local rule that exists, not about doing nothing
  until one does.
- **Precedence covers style, not soundness.** If a local convention would produce a
  criterion that cannot fail, the rubric's correctness checks still bite — follow the
  convention where you can, say plainly where you did not and why.

## Step 2.5 — Rule inventory (outcome mode only)

An outcome suite measures whether a skill's **body** was followed, so the rows have to come from
what the body actually says. Read the target `SKILL.md` and everything under its `references/`,
and extract four things:

- **Components** — the testable units the skill teaches.
- **Workflow steps** — its `### Step N` headings, or whatever lifecycle it declares.
- **Critical rules** — a `## Critical Rules` section if it has one. Most skills do not: fall back to
  the rules scattered through the other sections, **mark those implicit**, and say so in Step 7's
  report, because an implicit rule is one you inferred and the user may disagree.
- **Anti-patterns** — a `## Anti-Patterns` / `## What NOT to Do` section (`SKILL.md` only).

Produce a numbered table, and keep it — Step 4 and Step 7 both read it:

| # | Rule | Source | How it could be checked mechanically |
|---|---|---|---|
| R1 | … | SKILL.md § Step 2 (explicit) | … |

**Every row you go on to write must name the rules it exercises.** That is what makes the suite
derived from the skill rather than invented beside it, and it is what lets you answer "what is
this suite actually covering?" without re-reading everything.

Two outcomes worth naming rather than working around:

- **A rule no mechanical check can reach** ("the output looks polished") — leave it out of the
  grader and list it as ungraded in the report. Do not reach for `llm_judge` to cover it; that adds
  variance to the very number an optimization round reads.
- **Fewer than about eight extractable rules** — say so before writing anything. A split halves each
  side, so a thin inventory yields a suite that confirms nothing while reading as if it did. Offer
  a thicker one or a stated-thin one; do not quietly produce four rows.

If the target skill sets `disable-model-invocation: true`, the Skill tool refuses the call and the
body is never loaded — every row would then measure the model's background knowledge. That is out
of scope here: `/coder-eval:optimize-skill` already treats it (the line is removed in every arm's
snapshot). Say it applies and carry on.

## Step 3 — Design the task

**Task ID** — lowercase kebab-case, unique, `<domain>-<action>` (e.g.
`registry-list-processes`).

**Initial prompt** — minimal. State the goal and the expected output; nothing else.

- Good: "Use the `foo` CLI to list the available processes and save the result to
  `processes.json`."
- Bad: a step-by-step recipe with the exact flags, or a restatement of what the
  criteria check.

**Key rule: prompts instruct, criteria validate.** Never leak criteria detail into the
prompt. If a criterion checks that the output contains a `count` field, the prompt must
not mention `count` — otherwise you are testing transcription, not capability.

The subtle version of this, and the easiest to write by accident: a criterion that
matches a literal the prompt already dictates. "Use `pypdf` to read the fields" in the
prompt plus a criterion grepping for `pypdf` is a criterion that cannot fail — the agent
was told the answer. Either the constraint is a real requirement (keep it in the prompt,
and score what the agent *did with it* instead) or it is the thing under test (drop it
from the prompt). Never both.

(The rubric below carries this same trap as a review-time check, and is the declaration a
reviewer applies. The paragraphs above are the authoring-time version: they exist to stop you
writing it in the first place.)

**Success criteria** — read `${CLAUDE_PLUGIN_ROOT}/reference/task-rubric.md` *before*
choosing them. It is what this work will be checked against in step 5, and a criterion set
designed against it is far cheaper than one repaired after the fact.

Pick by what actually needs verifying:

| What to check | Criterion type |
| --- | --- |
| File exists, has content, matches a pattern | `file_check` (prefer over `file_exists` + `file_contains`) |
| JSON structure or specific values | `json_check` (JSON Schema + JMESPath assertions) |
| A script runs, tests pass, or a scorer emits a float | `run_command` |
| Output resembles a reference solution | `reference_comparison` |
| Subjective or open-ended quality | `llm_judge` |
| A deep, tool-using verdict on the sandbox | `agent_judge` (expensive) |
| The agent used a specific tool | `command_executed` |
| Tool-call efficiency against a budget | `commands_efficiency` |
| The agent engaged a target skill | `skill_triggered` (see `/coder-eval:check-skill`) |
| A predicted label vs. ground truth | `classification_match` |

Read `${CLAUDE_PLUGIN_ROOT}/reference/criteria.md` for each type's exact fields — it is
generated from coder-eval's own models, so it is the authoritative field list.

Rules that matter:

- **Every task needs at least one criterion that checks output *content***, not just
  existence. A suite of `file_exists` checks passes when the agent writes an empty file.
- Use `command_executed` sparingly — only when it genuinely matters *how* the result was
  produced. Set `require_success: true` whenever the command's success is what you are
  grading; the permissive default (`false`) counts a crashed invocation as evidence the
  work was done, and survives only for a genuine exception — a probe whose failure is an
  acceptable outcome.
- When the prompt genuinely must name a literal — a flag like `--json`, an output
  filename — a criterion matching that literal is a **smoke check**, not evidence: it
  only proves the agent typed back what it was told. Keep it if you like, at a low
  weight, and put the weight on a criterion that checks the resulting *behaviour*.
- `weight` reflects importance: `0.5` nice-to-have, `1.0` standard, `1.5`–`2.0` critical.
  `weight: 0` makes a criterion informational (reported, but excluded from the score and
  the pass/fail gate).
- The default `pass_threshold: 0.9` is right for most criteria; use `1.0` only for binary
  checks.
- Omit the `agent:` block unless the task needs non-default settings. Agent config is
  resolved from the experiment layer, and hardcoding it in every task defeats
  experiment-level control such as A/B model comparisons.

**Tags** — keep them portable: a difficulty tag (`smoke`, `basic`, `intermediate`) plus
whatever domain vocabulary the repository's existing tasks already use.

**Outcome mode — start from the template, do not re-derive it.** Copy
`${CLAUDE_PLUGIN_ROOT}/reference/templates/outcome.yaml` and `outcome-rows.jsonl` to where the
suite belongs and fill in every `REPLACE:` marker. That file is the single source of truth for the
suite's fields, and it already carries the reasoning for the plugin mount, `disallowed_tools`, the
weighted engagement gate, the run limits and `split_field` — read it there rather than expecting it
here. Only two things are yours to decide, and neither is in the template:

- **Rows derive from the Step 2.5 table.** One row per scenario, each naming the rules (R1…Rn) it
  exercises. A scenario nobody can trace back to a rule is a scenario measuring taste.
- **The split is assigned deterministically and never re-rolled.** Sort the rows by id, then walk
  the sorted list repeating `train, train, train, test, test` — 60/40, reproducible by anyone
  holding the same rows. (Alternating would give 50/50; write out the pattern rather than
  describing a ratio, so the file cannot say one thing and the rows do another.) Re-drawing the
  split between rounds leaks the test half into development, which is the one failure that makes
  every later confirmation meaningless — and it is invisible, because the numbers keep looking
  fine.

**Outcome mode — three row-design rules, each with the failure it prevents.** A suite that
satisfies the template's schema can still be incapable of measuring anything, and none of these is
visible in a file that validates:

- **Declare a role per row**, in the rows JSONL, as `role: "discriminator"` or `role: "guard"`.
  A *discriminator* is a row the incumbent **fails** — it has headroom, so a better body can show
  up on it. A *guard* is a row the incumbent **passes**; it cannot show improvement, only
  regression. Both are useful and a suite needs both, but conflating them produces a suite that is
  mostly dead weight. Measured: rows sitting at a baseline of 1.000 discriminated between arms
  **12%** of the time; rows with headroom, **71%**.
  Nothing validates this field — it is rows-JSONL data, which coder-eval treats as opaque, so a
  mistyped `role: gaurd` is silent. That is why Step 7 prints the **role tally**: a typo shows up
  as counts that do not add up to the row count.
- **The temptation test.** A good discriminator is a scenario where *the path of least resistance
  violates the rule*. Before writing the row, write the laziest plausible implementation of it in
  your head; if that lazy version does not break the rule, the row will sit at the ceiling and
  measure nothing no matter how many arms you run through it.
- **Depth over breadth.** A suite meant to improve rule R should be mostly rows that **fail** R.
  One row per rule is the worst possible shape: it maximises the denominator every effect is
  divided by while minimising the headroom available on any single rule. The arithmetic is
  `/coder-eval:optimize-skill` Step 7's ceiling table — read it there rather than re-deriving it
  here, and note that every row *passing* R makes R harder to improve measurably.

## Step 4 — Write the file(s)

One file per task, named after the task ID with underscores
(`registry-list-processes` → `registry_list_processes.yaml`), in the repository's task
directory. **In outcome mode this is replaced** by the one-suite rule stated under "Which mode"
above, and by the five artifacts below.

<!-- lint-skip: doc-yaml -->
```yaml
task_id: "<kebab-case-id>"
description: "<one line: what this task tests>"
initial_prompt: |
  <the natural-language request>
tags: ["smoke", "your-domain"]      # a difficulty tag plus the repo's domain vocabulary

sandbox:
  # `tempdir` runs the agent's commands on THIS machine — it isolates the working
  # directory, not the host. For a task that fetches or executes third-party content,
  # use `driver: "docker"` instead; that is the real confinement boundary.
  driver: "tempdir"
  python: {}              # a venv with no extra packages; add env_packages if needed

success_criteria:
  - type: "<criterion_type>"
    description: "<what this checks>"
    # ... type-specific fields
    weight: 1.0
```

Add `template_sources` if the task needs starter files (a codebase to modify, a fixture
to read).

**Outcome mode writes five artifacts, not one:**

| # | Artifact | Notes |
|---|---|---|
| 1 | the suite YAML | from `outcome.yaml`; one dataset-backed task |
| 2 | the rows JSONL | one row per scenario, `train`/`test` labelled, naming its rules |
| 3 | the fixture directory | the one starting repository every row works from |
| 4 | the grader script | from `${CLAUDE_PLUGIN_ROOT}/reference/templates/outcome-grader/verify.py` |
| 5 | its per-row expectations | one JSON file per row id, beside the grader |

**4 and 5 live OUTSIDE the fixture directory** — put them beside the suite YAML, in
`outcome-grader/`, and address them as `$TASK_DIR/outcome-grader/verify.py` as the template does.
Not a hardcoded absolute path: `$TASK_DIR` survives another machine, CI, and `driver: docker`
(which mounts the task directory but not an arbitrary host path), and all three of those failures
look identical — 0.0 on every row of every arm. The reason they are outside the fixture at all is
stated in full in `outcome.yaml`, next to the mount it constrains: everything under the fixture is
copied into every sandbox, so expectations placed there hand the agent exactly what it is being
marked against, and the run still looks completely normal.

The fixture's *contents* are yours to design and this skill does not author them: state what it
must satisfy — `outcome.yaml`'s `sandbox:` comment declares the constraints, including the one that
ties every arm to the floor when it is missed — then build it or ask for it.

## Step 5 — Could this pass for the wrong reason?

Now re-apply `${CLAUDE_PLUGIN_ROOT}/reference/task-rubric.md` to the files you just wrote.
Designing against it and checking against it are different acts: the first shapes your
choices, the second catches what you actually typed.

Answer the rubric's framing question **in writing** — *what is the cheapest thing an agent
could do that scores full marks?* — and if that cheapest path does not resemble the work
the task claims to test, fix the criteria before going further. Work every section of the
rubric, including its fixture-lifecycle section whenever the task touches state outside the
sandbox.

Fix what you find here rather than reporting it. Note which checks you applied; step 7 asks
for them.

## Step 6 — Validate

For each file written, run `coder-eval plan <path>` and fix everything it reports. It
validates through the real Pydantic models, so a mistyped field name or a missing
required key surfaces here rather than halfway through a paid run.

**Outcome mode: plan each split too.** Run `coder-eval plan <suite> --split train` and
`coder-eval plan <suite> --split test`. Both must exit 0 — **and then READ THE ROW COUNTS**, because
exit 0 is not the whole answer here: a partly-labelled dataset plans clean. `--split` keeps the
matching rows and silently DROPS the unlabelled ones, so the counts, not the exit code, are what
tell you every later metric would be computed over a smaller suite than the file suggests. `plan`
prints a ⚠ when it happens; the counts are how you confirm it did not.

**Outcome mode: rows ↔ expectations parity, BOTH directions.** Every row id in the JSONL must have
an `outcome-grader/expectations/<row id>.json`, and every expectations file must have a row. List
both sets and diff them; do not eyeball it. Each direction fails differently and neither is loud:

- a **missing expectations file** scores that row a hard `0.0000` on every arm — indistinguishable
  from a catastrophically bad body, and it cost a full 15-row run to find once;
- an **orphan expectations file** means a row was renamed, so something you wrote a marking scheme
  for is now silently ungraded.

A suite with no expectations directory at all (one scored only by `file_check`, say) has nothing to
compare, so this check is a no-op there rather than a failure.

**Outcome mode: the applicable-check floor.** Report the **min / mean / max** number of checks
declared per row, and require a minimum of **4**. Below four a row's score takes at most five
values and behaves like a binary grader — which is how the execution gate's zero-variance refusal
gets manufactured: two arms of genuinely different quality both land on the same handful of values,
every paired difference is zero, and the gate correctly reports it cannot separate them.

Two honest caveats to state rather than paper over:

- The floor is on **declared** checks, because applicability is only knowable after a run: a check
  that returns N/A leaves the denominator, so the applicable count on a real row can be lower than
  the declared one. Say the declared count is an upper bound, never the applicable count.
- More checks is more chances to write an **unfair** one. That trade is real, and Step 6.5 is what
  catches the unfair check — the floor and the discrimination gate are two halves of one argument,
  not competing advice.
- A **guard** row deliberately sitting at 1.000 is not an error and must not read as one; its
  `role` is what says so, which is why Step 7 prints the tally.

Then re-read your own work and check:

- every criterion refers to a file or command the prompt actually leads the agent to
  produce;
- the prompt leaks no criteria detail;
- at least one criterion inspects content.

**A task nobody has ever run is not finished.** `plan` proves the YAML is well formed; it
says nothing about whether the criteria can be satisfied, or whether they can be satisfied
too easily. Only a run answers that, so once `plan` exits 0:

State the task count, the agent and model the tasks resolve to, and that **a run costs real
tokens** — then **offer to run it and ask**. Never run unprompted.

```bash
coder-eval run <path>
```

Then interpret the result rather than reporting it:

- **A first run scoring 1.000 is suspicious, not a success.** A task written and passed on
  the first attempt is more often a task that grades something trivial than a task that
  happened to be perfect. Go back to the framing question in step 5 and re-answer it against
  the trajectory you now have: what did the agent actually do, and would the cheapest path
  have scored the same?
- **A failing run is a diagnosis, not a prompt edit.** Decide first *which layer* is wrong:
  something a real user would plausibly have said (fix the prompt), or something the skill
  or the underlying tool should have supplied (fix that instead, and leave the task failing
  until it exists). Patching the prompt to route around a missing capability makes the score
  green and changes nothing for users.
- **Never ship a task that cannot pass yet.** A task that always fails is noise: it trains
  everyone reading the suite to ignore a red result. Either withdraw it, or say plainly what
  has to exist before it is worth scheduling.

If the user declines the run, that is a fine outcome — record it as declined in the report
rather than implying the task is validated.

## Step 6.5 — Prove the instrument discriminates (outcome mode only)

The grader is the measurement. Before anyone pays for a stage, establish that it can tell a good
artifact from a bad one:

1. **Build a known-good artifact** that satisfies every rule the row grades, and a **known-bad**
   one that violates most of them. Grade both with the grader, by hand, and **report the separation
   margin** — the two scores and the gap between them. A grader that scores them alike measures
   nothing, and every number after it is decoration. (If a rule is about *absence* — "must not use
   X" — invert: the known-bad artifact is the one that does the forbidden thing.)
2. **Work the grader-fairness questions in `${CLAUDE_PLUGIN_ROOT}/reference/task-rubric.md` §
   "Grader fairness"** against what you just saw. They are declared there, not here — this step
   asks them of a result, the way Step 5 asks the rest of the rubric of a file. Designing against a
   check and watching it score are different acts.
3. **This is the only place these errors can be caught.** A wrong check biases every arm of an A/B
   **equally**, so the ranking, the paired test and the confirmation all agree with each other and
   are all wrong together. No comparison downstream can surface it.

Two results that mean "fix it now", not "note it":

- **The known-good artifact scores below 1.0.** Either the grader is over-strict or the rules are
  unsatisfiable. Fix that rather than lowering the bar; a ceiling below 1.0 shrinks every effect
  the suite can measure.
- **A row scores 0.0 on the known-good artifact with `0/0 applicable`.** That row declares only
  checks that do not apply to it, so it measures nothing — and it announces itself here rather than
  quietly dragging an arm's mean down later.

A narrow but non-zero margin is a number to report, not a failure: suites differ, and there is no
threshold worth stating. Report the margin either way.

## Step 7 — Report

Summarize what you wrote:

| File | Task ID | Criteria | Tags | Run verdict |
| --- | --- | --- | --- | --- |

The **run verdict** is the score from step 6, or an explicit `not run` **with the reason**
(the user declined, no credentials, a dependency does not exist yet). An empty cell reads as
a pass to everyone who sees the table later.

Then:

- **Your answer to the framing question** — the cheapest path to full marks, and why the
  criteria do not accept it. One or two sentences, not a restatement of the rubric.
- **Which rubric checks you applied**, and what any of them changed.
- **Outcome mode:** the Step 2.5 rule table, marking which rules were **implicit** (inferred rather
  than declared by the skill) and which are **ungraded** because no mechanical check reaches them.
  Both are judgements the user may disagree with, and neither is visible in the files you wrote.
- **Outcome mode:** the **role tally** — how many rows are `discriminator`, how many are `guard`,
  and how many carry neither. The third count is what surfaces a typo in a field nothing validates,
  and a suite that is nearly all guards is one that can only report regressions.
- **Outcome mode:** the **per-row check counts** (min / mean / max) from Step 6, and the projected
  cost of a full round. Do not restate the stage arithmetic here: take the formulas from
  `${CLAUDE_PLUGIN_ROOT}/reference/optimize-method.md`'s cost table, substitute the row count you
  just wrote, and print the **run counts per stage**. Per-row cost stays a variable the user fills
  in from their first measured row — a hand-computed projection that guessed it projected $109 for
  a round that spent about $143. Give run counts, name the unknown, and let the measured row close
  it.
- **What the run showed**, if it happened — particularly if it scored 1.000 and what you
  concluded about that.
- Any assumptions you made.
- The command to re-run it: `coder-eval run <path>` (real tokens, real cost).
