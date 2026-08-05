---
description: >-
  Install the Coder Eval plugin for Claude Code — five slash commands to scaffold,
  author, run and analyze evaluation suites, including an activation suite that
  measures whether your own Claude Code skills actually trigger.
---

# Claude Code plugin

Coder Eval ships as a **Claude Code plugin**, so the whole loop — scaffold a
suite, author a task, check whether a skill triggers, read the results, wire it
into CI — happens inside the agent instead of in a separate terminal.

The `UiPath/coder_eval` repository is itself the plugin marketplace:

```
/plugin marketplace add UiPath/coder_eval
/plugin install coder-eval@coder-eval
```

The first command registers the repository as a marketplace; the second installs
the one plugin it hosts. Marketplace and plugin share the name `coder-eval`,
which is why the install target reads `coder-eval@coder-eval`.

## Prerequisite

The plugin drives the `coder-eval` CLI; it does not bundle it. Install it once:

```bash
uv tool install coder-eval    # or: pip install coder-eval
```

Every skill checks `coder-eval --version` before doing anything and stops with
this hint if it is missing. Running a suite additionally needs credentials for
whichever agent the tasks use — `ANTHROPIC_API_KEY` for the default
`claude-code` agent.

## The five skills

| Command | What it does |
| --- | --- |
| `/coder-eval:init` | Scans the repository for what is worth evaluating (Claude Code skills, an MCP server, a CLI), reports the findings, then scaffolds a task directory with one real task. |
| `/coder-eval:skill-check` | Builds and runs an activation suite for one of your skills — does the agent engage it when it should, and leave it alone when it shouldn't? |
| `/coder-eval:task` | Turns a natural-language description into task YAML with criteria that check output *content*, validated through `coder-eval plan`. |
| `/coder-eval:analyze` | Reads a finished run directory and writes `analysis.md`: systemic failure patterns, per-task findings, and concrete fixes. |
| `/coder-eval:ci` | Emits a GitHub Actions workflow that runs the suite as a gate, or on a schedule to catch skill drift. |

`init` and `ci` are explicit-invocation only — scaffolding a directory or writing
a workflow is never something to do unprompted. The other three can also be
reached by the agent on its own when a request clearly calls for them.

## Worked example: does my skill actually trigger?

A skill is selected almost entirely from its frontmatter `description`. Whether
that description wins the requests it should — and loses the ones it shouldn't —
is invisible until a user complains. `skill-check` turns it into a number.

Point it at a skill:

```
/coder-eval:skill-check .claude/skills/pdf-forms
```

It then:

1. reads the skill's frontmatter `description` — the string the model actually
   matches on;
2. designs **positive** rows (requests the description claims to cover,
   paraphrased — never lifted from the description, which would test string
   overlap rather than activation) and **distractor** rows (adjacent requests the
   skill should decline, especially ones sharing its vocabulary);
3. copies the bundled activation template into your task directory as a
   [dataset-backed task](DATASETS.md) — one row per request, each scored by the
   [`skill_triggered`](TASK_DEFINITION_GUIDE.md) criterion;
4. validates with `coder-eval plan`, tells you the row count and the cost
   implication, and asks before running;
5. reports recall, precision, F1 and the confusion matrix — then interprets them:
   low recall means the description under-claims, low precision means it
   over-claims and is stealing adjacent requests.

The suite is a normal task file, so it stays in your repository and can be re-run
after every description edit — which is the point. Editing skill wording without
a suite is guesswork; with one, the change either moves recall or it doesn't.

Because the suite is gated on `suite_thresholds` (`recall.yes`, `precision.yes`),
it also works as a CI gate: run it on a schedule and a skill that quietly stops
triggering — because the model changed, or someone reworded the description —
fails a build instead of surprising a user.

## What ships with the plugin

An installed plugin is copied to `~/.claude/plugins/cache/` without its parent
directories, so every file a skill reads travels with it under `reference/`:

- `criteria.md` — every criterion type and its fields, **generated** from Coder
  Eval's own `SuccessCriterion` model union (`make plugin-reference`), so it
  cannot drift from the schema the CLI validates against.
- `task-rubric.md` — the adversarial task-quality checklist `task` applies to work it
  writes and `lint-tasks` applies to task files already on disk: could this task pass for
  the wrong reason, does it grade behaviour or a self-report, do its fixtures reset and
  clean up.
- `run-layout.md` — the on-disk run-directory contract `analyze` reads.
- `templates/` — the canonical activation suite `skill-check` copies.

## Related

- The plugin's own README lives at
  [`plugins/coder-eval/README.md`](https://github.com/UiPath/coder_eval/blob/main/plugins/coder-eval/README.md).
- For the GitHub Action the `ci` skill emits — its inputs, JUnit output and score
  floor — see [CI Gate & GitHub Action](CI_GATE.md).
- For the criterion vocabulary in full, see the
  [Task Definition Guide](TASK_DEFINITION_GUIDE.md).
