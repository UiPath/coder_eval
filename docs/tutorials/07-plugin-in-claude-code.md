---
description: >-
  Drive Coder Eval from inside Claude Code — install the plugin, scaffold a task
  directory, author and review a task, then read the run it produces.
---

# Tutorial 07 — Driving Coder Eval from Claude Code

By the end you'll have installed the Coder Eval plugin and driven a full loop from
slash commands: scaffold, author, review, run, analyze. ~15 minutes.

**Cost:** steps 1–2 are free; budget **one paid agent run** for steps 3–4. The
optional step 5 is one run *per row* — 16 for an 8/8 suite — so budget it
separately.

## Prerequisites

- Claude Code installed and working.
- The `coder-eval` CLI. **Installing the plugin does not install it** — a plugin
  ships skills, not packages. You can let the skills handle it (`init`, `task` and
  `check-skill` check for it and offer to install it, asking first), or do it now:

    ```bash
    uv tool install coder-eval    # or: pip install coder-eval
    coder-eval --version
    ```

- An API key for whichever agent your tasks use (`ANTHROPIC_API_KEY` for the
  default `claude-code` agent).

Steps 2 onward assume you are in **your own repository** — not the `coder_eval`
clone used by Tutorials 01 and 04.

## 1. Install the plugin

This repository is itself the marketplace. Both of these are typed at the Claude
Code prompt, not in your shell:

```
/plugin marketplace add UiPath/coder_eval
/plugin install coder-eval@coder-eval
```

Verify by typing `/coder-eval:`. You should see seven commands: `init`,
`check-skill`, `task`, `optimize-skill`, `lint-tasks`, `analyze`, `ci`. Four of them
— `init`, `check-skill`, `task` and `optimize-skill` — drive the same `coder-eval`
CLI you would type by hand, so what they write is a normal file you can commit, diff
and run in CI. The remaining three never invoke it: `analyze` reads a finished run
directory, `ci` writes a workflow, and `lint-tasks` only reads task files and reports.

Three of the seven are deliberately **not** model-invokable — `init`, `ci` and
`optimize-skill` carry `disable-model-invocation: true`, so you reach them by typing the
command rather than by describing the job. That matters most for `optimize-skill`: it
spends real money across a baseline and three A/B stages, and is never something to start
because a message happened to mention a skill's wording.

## 2. Scaffold a suite

```
/coder-eval:init
```

It scans for what is worth evaluating (Claude Code skills, an MCP server, a CLI),
reports what it found, then scaffolds a task directory with one runnable task.

**Note the directory it reports** — the layout varies by repository (`tasks/`,
`tests/tasks/`, …) and later steps need that path:

```bash
ls tasks/                       # or whichever path init reported
cat tasks/*.yaml | head -40
```

Read that task before moving on. It is the shape every later task here gets
modeled on, and step 3 is easier to follow once you have seen one.

## 3. Author a task, then review it

```
/coder-eval:task a task that checks the CLI can list processes as JSON
```

It designs criteria against the bundled
[task-quality rubric](https://github.com/UiPath/coder_eval/blob/main/plugins/coder-eval/reference/task-rubric.md),
then re-checks the files it wrote against the rubric's framing question: *what is
the cheapest thing an agent could do that scores full marks?*

Then it validates with `coder-eval plan` and **offers to run the task, asking
first**. Take the offer — step 4 needs a run. Read the score the way the skill
does: a **1.000 on a first attempt means re-read the criteria**, not celebrate, and
a failure is a question about *which layer* is wrong (the prompt, or the skill or
tool it depends on) before it is a prompt edit.

Now review what you already have. Point the read-only linter at the directory from
step 2:

```
/coder-eval:lint-tasks tasks/
```

Same rubric, applied to files on disk. Per task you get a severity, a line
reference and a concrete fix, covering criteria that cannot fail, prompts that give
away the answer, fixtures with no cleanup and near-duplicates. Gameability findings
name the weight at risk, e.g. *"A single `--file` call satisfies 14.0 of 33.0
weight"*. It never edits a file, and it scores test design only, so it closes by
suggesting `coder-eval plan` for the schema half. Watch for `⚠` notices there: an
unknown top-level key warns rather than fails.

## 4. Read the result

If you accepted the run in step 3, you already have a run directory: the skill
invoked the CLI through Bash on your behalf. By hand it is the same command, which
is the whole point of the plugin being a driver rather than a separate product:

```bash
coder-eval run                  # discovers tasks recursively; or pass explicit paths
ls runs/latest/                 # the run that was just written
```

Hand it back to the agent:

```
/coder-eval:analyze runs/latest
```

It writes `analysis.md` **into** the run directory, containing:

- a TL;DR and a score breakdown,
- per-task findings with concrete fixes, ranked by estimated score recovery,
- on suites over 20 tasks, failures clustered into systemic patterns instead of
  repeated per task.

## 5. Check whether your own skill triggers (optional)

If this repository has Claude Code skills, the plugin can measure whether the model
reaches for one at the right moment.

**Export the skill location first** — the evaluated agent runs in a fresh sandbox
holding none of your files, so it is offered no skills unless the task says where
they live. Point at a **plugin root** — a directory holding a `skills/` subdirectory, so the
skill sits at `<path>/skills/<name>/SKILL.md`. For `.claude/skills/pdf-forms/SKILL.md` that
root is `.claude`, **not** `.claude/skills`:

```bash
export SKILL_SOURCE_PATH="$(pwd)/.claude"
```

```
/coder-eval:check-skill pdf-forms
```

You get recall, precision and F1 over a labeled suite of requests the skill should
win plus distractors it should decline, gated by `suite_thresholds` on
`recall.yes` / `precision.yes`. Each row is a full agent run, so the skill states
the count and asks before starting.

Low recall has three possible causes, not one: truncation and listing-budget
eviction look identical to bad wording.
[The plugin page](../PLUGIN.md#a-low-recall-result-has-three-causes-not-one)
covers telling them apart with `/doctor` and `/context`.

## If something goes wrong

| Symptom | Cause |
| --- | --- |
| No `/coder-eval:` commands after installing | Check `/plugin`; re-run the install |
| A skill offers to install the CLI, or Bash reports `command not found` | The CLI isn't installed or isn't on `PATH` — accept the offer, or install it yourself |
| `coder-eval run` matches nothing | Wrong directory — use the path `init` reported in step 2 |
| Every positive row in step 5 scores 0 | `SKILL_SOURCE_PATH` is unset **or one level too deep** — it must name a plugin root holding `skills/`, e.g. `.claude`, not `.claude/skills`. Either way the skill was never offered |

To update after the marketplace moves, `/plugin marketplace update coder-eval`; to
remove it, `/plugin uninstall`.

## Next steps

- `/coder-eval:ci` emits the CI workflow from [Tutorial 02](02-ci-pipeline.md) for
  you — least-privilege by default, and it provides the agent runtime (Node plus
  the Claude CLI) that the Action deliberately does not install.
- `/coder-eval:optimize-skill` is the follow-on from step 5. `check-skill` tells you
  *whether* a skill fires; this A/B-tests edits to its description — or to its body — as
  experiment variants, and promotes one only when it beats run-to-run noise on rows it was
  never fitted to. It is the expensive one: a baseline plus three stages of agent runs, so
  it states the projected count and asks before each. Walked end to end in
  [Tutorial 08](08-optimizing-a-skill.md) (description) and
  [Tutorial 09](09-optimizing-a-skill-body.md) (body).
- [Claude Code plugin](../PLUGIN.md) — the reference page: every skill, what ships
  in the plugin, and the activation-budget mechanics in full.
- [Writing a task](04-writing-a-task.md) — the same authoring loop by hand, worth
  doing once to see what the skill is producing.
- [Task Definition Guide](../TASK_DEFINITION_GUIDE.md) — the complete task schema.
