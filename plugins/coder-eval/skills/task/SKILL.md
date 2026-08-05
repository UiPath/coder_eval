---
description: Turn a natural-language description into one or more coder-eval task YAML files — minimal prompts, weighted success criteria that check output content, validated with `coder-eval plan`. Use when the user wants to write, add, or generate an evaluation task.
allowed-tools: ["Read", "Glob", "Grep", "Write", "Bash"]
---

# Author a coder-eval task

You are writing coder-eval task YAML. The user's request is: `$ARGUMENTS`

If `$ARGUMENTS` is empty, ask what the task should test. Do not invent a subject.

Good tasks use simple prompts: state the goal and the expected output, then let the
agent work out the approach. A single request can produce **several** task files —
"create tasks for all the registry subcommands" means one task per subcommand.

## Step 1 — Understand the request

- **What is being tested** — which tool, SDK, CLI, skill, or capability?
- **How many tasks** — one operation, or several?
- **Difficulty** — smoke, basic, or intermediate?
- **Dependencies** — network, packages, starter files, external services?

State any assumptions you make rather than silently picking.

## Step 2 — Look at what already exists

Find the repository's task directory by globbing for `*.yaml` files containing a
`task_id:` key (commonly `tasks/`). Read a couple of the existing tasks and follow
their conventions: naming, tags, criteria style. If a task already covers this ground,
say so and offer to modify it instead of adding a near-duplicate.

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
| The agent engaged a target skill | `skill_triggered` (see `/coder-eval:skill-check`) |
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

## Step 4 — Write the file(s)

One file per task, named after the task ID with underscores
(`registry-list-processes` → `registry_list_processes.yaml`), in the repository's task
directory.

```yaml
task_id: "<kebab-case-id>"
description: "<one line: what this task tests>"
initial_prompt: |
  <the natural-language request>
tags: [<difficulty>, <domain>]

sandbox:
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

## Step 5 — Could this pass for the wrong reason?

Now re-apply `${CLAUDE_PLUGIN_ROOT}/reference/task-rubric.md` to the files you just wrote.
Designing against it and checking against it are different acts: the first shapes your
choices, the second catches what you actually typed.

Answer the rubric's framing question **in writing** — *what is the cheapest thing an agent
could do that scores full marks?* — and if that cheapest path does not resemble the work
the task claims to test, fix the criteria before going further. Work the rubric's six
mechanical checks, and section 5 as well whenever the task touches state outside the
sandbox.

Fix what you find here rather than reporting it. Note which checks you applied; step 7 asks
for them.

## Step 6 — Validate

For each file written, run `coder-eval plan <path>` and fix everything it reports. It
validates through the real Pydantic models, so a mistyped field name or a missing
required key surfaces here rather than halfway through a paid run.

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
- **What the run showed**, if it happened — particularly if it scored 1.000 and what you
  concluded about that.
- Any assumptions you made.
- The command to re-run it: `coder-eval run <path>` (real tokens, real cost).
