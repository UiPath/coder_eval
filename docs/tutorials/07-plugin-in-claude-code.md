---
description: >-
  Drive Coder Eval from inside Claude Code — install the plugin, scaffold a task
  directory, author and adversarially review a task, measure whether your own
  skill triggers, and read a finished run, without leaving the agent.
---

# Tutorial 07 — Coder Eval inside Claude Code

By the end you'll have installed the Coder Eval plugin, scaffolded a task
directory, authored a task and had it reviewed against the shared quality rubric,
and read a finished run — all from inside Claude Code. ~15 minutes, plus one paid
agent run.

The plugin is a set of six slash commands. It is not a different product: each one
drives the same `coder-eval` CLI you would type by hand, so anything it produces
is a normal file you can commit, diff, and run in CI.

## Prerequisites

- Claude Code installed and working.
- The `coder-eval` CLI — the plugin drives it but does not bundle it:

    ```bash
    uv tool install coder-eval    # or: pip install coder-eval
    ```

- An API key for whichever agent your tasks use (`ANTHROPIC_API_KEY` for the
  default `claude-code` agent). Steps 1–3 cost nothing; step 4 spends tokens.

## 1. Install the plugin

This repository *is* the marketplace, so both commands are one-liners inside
Claude Code:

```
/plugin marketplace add UiPath/coder_eval
/plugin install coder-eval@coder-eval
```

The first registers the repository as a plugin marketplace; the second installs
the one plugin it hosts. They share the name `coder-eval`, which is why the target
reads `coder-eval@coder-eval`.

Confirm the commands are there by typing `/coder-eval:` — you should see six:
`init`, `skill-check`, `task`, `lint-tasks`, `analyze` and `ci`.

## 2. Scaffold a suite

Run this in a repository you actually want to evaluate:

```
/coder-eval:init
```

It scans for what is worth evaluating — Claude Code skills, an MCP server, a CLI —
reports what it found, and scaffolds a task directory containing **one real task**
rather than a placeholder. Read that task before moving on: it is the shape every
later task in the repository gets modelled on.

`init` and `ci` are the two explicit-invocation-only skills — scaffolding a
directory or writing a workflow is never something to do unprompted. The other
four can also be reached by the agent on its own when a request clearly calls for
them.

## 3. Author a task, then attack it

Describe what you want tested in plain language:

```
/coder-eval:task a task that checks the CLI can list processes as JSON
```

Two things about this step are worth watching, because they are where task suites
usually go wrong.

**It designs against a rubric, then re-checks against it.** Before choosing
criteria the skill reads the bundled
[task-quality rubric](../PLUGIN.md#what-ships-with-the-plugin), and after writing
the files it asks the rubric's framing question out loud: *what is the cheapest
thing an agent could do that scores full marks?* If the cheapest path does not
resemble the work the task claims to test, the criteria get fixed before you ever
spend a token.

**A task nobody has run is not finished.** `coder-eval plan` proves the YAML is
well formed; it says nothing about whether the criteria can be satisfied, or
whether they can be satisfied too easily. So the skill offers a run and asks
first — and then interprets it, which is the part that matters:

- a first run scoring **1.000 is suspicious, not a success** — more often a task
  grading something trivial than a task that happened to be perfect;
- a failing run is a *layer* diagnosis before it is a prompt edit: would a real
  user have said the missing thing (fix the prompt), or should the skill or tool
  have supplied it (fix that, and leave the task failing until it is fixed)?

To review task files you already have — including ones written long before the
plugin — point the read-only linter at them:

```
/coder-eval:lint-tasks tasks/
```

It applies the same rubric to files on disk and reports, per task, a severity and
a concrete fix: criteria that cannot fail, prompts that give away the answer,
fixtures with no cleanup, near-duplicates. Gameability findings quote the weight
at risk (*"a single `--file` call satisfies 14.0 of 33.0 weight"*) rather than an
adjective, because that number is computable from the YAML alone. It never edits a
file — it scores test design only, and it deliberately does not validate schema,
so it ends by suggesting `coder-eval plan` for that half.

## 4. Run it and read the result

Run the suite (this is the step that costs money):

```bash
coder-eval run tasks/*.yaml
```

Then hand the finished run directory back to the agent:

```
/coder-eval:analyze runs/latest
```

It writes `analysis.md` next to the run: failures clustered into systemic
patterns rather than repeated per task, then per-task findings and concrete fixes,
ranked by estimated score recovery. Every number it reports is computed from the
run's own JSON, not eyeballed.

## 5. Optional — does your own skill actually trigger?

If the repository has Claude Code skills, the plugin can measure whether the model
reaches for one at the right moment — a question that is otherwise invisible until
a user complains:

```
/coder-eval:skill-check .claude/skills/pdf-forms
```

It builds a labelled suite of requests — positives the skill should win,
distractors it should decline — runs a real agent against each, and reports recall,
precision and F1.

**One prerequisite it cannot infer:** the evaluated agent runs in a fresh sandbox
holding none of your files, so it is offered no skills unless the task says where
they live. Export that location first — the directory *containing* the skill's own
directory:

```bash
export SKILL_SOURCE_PATH="$(pwd)/.claude/skills"
```

Leave it unset and every positive row scores 0, which reads exactly like a broken
skill. Each row is a full agent run, so an 8-positive/8-distractor suite is 16
runs — the skill states the count and asks before starting.

If recall comes back low, the description is only one of three possible causes;
truncation and listing-budget eviction produce an identical-looking number.
[The plugin page](../PLUGIN.md#a-low-recall-result-has-three-causes-not-one)
covers how to tell them apart with `/doctor` and `/context` before you rewrite
anything.

## 6. Wire it into CI

```
/coder-eval:ci
```

This emits a GitHub Actions workflow that runs the suite as a gate, or on a
schedule to catch model and skill drift. The emitted workflow is least-privilege
by default (`permissions: contents: read`, and checkout without persisted
credentials) because evaluated tasks execute agent-generated code.

For the Action's inputs, JUnit output and score floor in full, see
[Tutorial 02](02-ci-pipeline.md) and [CI Gate & GitHub Action](../CI_GATE.md).

## What you learned

- The plugin drives the same CLI you would use by hand, so everything it writes is
  a committable file — nothing is locked inside the agent.
- Authoring and reviewing are separate skills on purpose: `task` holds `Write`,
  `lint-tasks` must not, and tool policy is declared per skill.
- Both share one bundled rubric, so a task is checked against the same standard
  whether it was written a minute ago or a year ago.
- A run is part of "done", and a perfect first score is a reason to re-read the
  criteria rather than to celebrate.

## Next steps

- [Claude Code plugin](../PLUGIN.md) — the reference page: every skill, what ships
  in the plugin, and the activation-budget mechanics in full.
- [Writing a task](04-writing-a-task.md) — the same authoring loop by hand, which
  is worth doing once to see what the skill is producing.
- [Task Definition Guide](../TASK_DEFINITION_GUIDE.md) — the complete task schema.
