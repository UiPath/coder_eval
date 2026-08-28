---
description: >-
  Install the Coder Eval plugin for Claude Code — six slash commands to scaffold,
  author, review, run and analyze evaluation suites, including an activation suite
  that measures whether your own Claude Code skills actually trigger.
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

**Installing the plugin does not install the CLI.** A plugin ships skills and
references, not packages, so the `coder-eval` binary is a separate step:

```bash
uv tool install coder-eval    # or: pip install coder-eval
```

You do not have to do it in advance. `init`, `task` and `check-skill` — the three
skills that shell out to the CLI — check `coder-eval --version` before doing any
work and, if it is missing, **offer to install it and ask first**. They never
install unprompted: that writes outside your repository, so it is your call, and
they verify the install worked before continuing. `analyze` and `ci` do not invoke
the CLI, and `lint-tasks` needs neither the CLI nor credentials — it only reads
files, though the report it produces ends by suggesting you run `coder-eval plan`
yourself.

Running a suite additionally needs credentials for whichever agent the tasks use —
`ANTHROPIC_API_KEY` for the default `claude-code` agent.

## The six skills

| Command | What it does |
| --- | --- |
| `/coder-eval:init` | Scans the repository for what is worth evaluating (Claude Code skills, an MCP server, a CLI), reports the findings, then scaffolds a task directory with one real task. |
| `/coder-eval:check-skill` | Builds and runs an activation suite for one of your skills — does the agent engage it when it should, and leave it alone when it shouldn't? |
| `/coder-eval:task` | Turns a natural-language description into task YAML with criteria that check output *content*, validated through `coder-eval plan`. |
| `/coder-eval:lint-tasks` | Reviews task YAML that already exists and reports, per task, criteria that cannot fail, prompts that leak the answer, fixtures with no cleanup and near-duplicates — each with a severity and a fix. Read-only. |
| `/coder-eval:analyze` | Reads a finished run directory and writes `analysis.md`: systemic failure patterns, per-task findings, and concrete fixes. |
| `/coder-eval:ci` | Emits a GitHub Actions workflow that runs the suite as a gate, or on a schedule to catch skill drift. |

`init` and `ci` are explicit-invocation only — scaffolding a directory or writing
a workflow is never something to do unprompted. The other four can also be
reached by the agent on its own when a request clearly calls for them.

`task` and `lint-tasks` are two halves of the same concern and share one bundled
rubric: `task` applies it to work it is writing, `lint-tasks` applies it to files
you already have. They are separate skills because authoring needs `Write` and a
review pass should not have it, and frontmatter declares tool policy per skill —
so one skill cannot hold both stances.

`lint-tasks` expresses read-only three ways, and it is worth being precise about
what each buys, because only the last one spans a whole review: its
`allowed-tools` lists just `Read`, `Glob` and `Grep`; its `disallowed-tools` names
every write tool, which removes them from the pool **for the invoking turn only** —
the restriction clears when you send your next message, and the skill asks you one
before linting a whole directory; and its own instructions carry a standing
prohibition on modifying a file, which is what actually holds for the rest of the
review.

## Worked example: does my skill actually trigger?

A skill is selected almost entirely from its frontmatter `description`. Whether
that description wins the requests it should — and loses the ones it shouldn't —
is invisible until a user complains. `check-skill` turns it into a number.

Point it at a skill:

```
/coder-eval:check-skill .claude/skills/pdf-forms
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
   low precision means the description over-claims and is stealing adjacent
   requests; low recall means it under-claims **once truncation and listing
   eviction are ruled out** (see below).

One prerequisite the suite cannot infer: the evaluated agent runs in a fresh
sandbox holding none of your files, so it is offered no skills unless the task
says where they live. The template reads that location from an environment
variable — point it at a **plugin root**: a directory holding a `skills/`
subdirectory, so the skill sits at `<path>/skills/<skill-name>/SKILL.md`. For
`.claude/skills/pdf-forms/SKILL.md` that root is `.claude`, not `.claude/skills`:

```bash
export SKILL_SOURCE_PATH="$(pwd)/.claude"
```

Leave it unset and the skill is simply absent, every positive row scores 0, and
the result is indistinguishable from a skill that never fires. It stays an
environment variable rather than a path baked into the YAML so the suite is
portable — it is committed and re-run on other machines, and in CI.

### A low-recall result has three causes, not one

Two of them are budgets rather than wording, and both produce a number that looks
exactly like a badly written description:

- **Per-skill truncation.** `description` and `when_to_use` are concatenated and
  cut at 1,536 characters (configurable via `skillListingMaxDescChars`). Trigger
  text past the cutoff cannot affect activation at all.
- **Whole-listing eviction.** The skill listing's character budget scales at about
  1% of the model's context window and is shared with *every* skill you have
  installed. On overflow, descriptions are dropped **starting with the skills you
  invoke least** — so a newly authored skill, which is by definition rarely
  invoked, is the likeliest casualty. That is a systematic bias against exactly
  the skill you are testing.

Check both before rewriting anything: `/doctor` estimates the listing's context
cost and its biggest contributors, and the Skills row in `/context` reports the
listing size *after* the budget is applied — what the model actually received.
Only once the description is demonstrably in the listing is the wording the
culprit.

The suite is a normal task file, so it stays in your repository and can be re-run
after every description edit — which is the point. Editing skill wording without
a suite is guesswork; with one, the change either moves recall or it doesn't.

Because the suite is gated on `suite_thresholds` (`recall.yes`, `precision.yes`),
it also works as a CI gate: run it on a schedule and a skill that quietly stops
triggering — because the model changed, or someone reworded the description —
fails a build instead of surprising a user.

## Adopting this where coder-eval already exists

The skills are written for a repository that already has a suite, not only for an empty
one. Three things follow from that, and they are worth knowing before you install:

- **They discover; they do not assume.** Nothing looks for `tasks/` or `runs/latest` by
  name. A skill globs for YAML carrying a `task_id:` key and for `run.json`, reports the
  tree it resolved, and asks when a monorepo offers more than one. Call your eval tree
  `evals/`, or nest the whole thing under `tests/`, and the skills follow it.
- **They will not scaffold over what you have.** `init` run against a configured
  repository reports the inventory and stops, pointing at `lint-tasks` and `analyze`
  instead. `check-skill` looks for a suite that already covers the skill and offers to
  extend it rather than writing a second one. And where your repository pins a
  coder-eval version, the CLI-driving skills resolve that pin first and stop on a
  mismatch rather than validating with the wrong binary — a schema error from a
  mismatched CLI is indistinguishable from a real one.
- **Your own tooling stays authoritative.** If you already have slash commands or a CI
  workflow covering the same ground, these skills are additive. The naming keeps them
  apart: plugin skills are always namespaced `/coder-eval:<name>`, while a repo-local
  command in your own `.claude/commands/` keeps whatever name you gave it. Where both
  exist, prefer yours — it knows things the plugin cannot.

`task` applies the same rule to authoring: it reads whatever your repository declares
about writing tasks and follows it, and reports which conventions it adopted. The bundled
rubric is a check of last resort, for what no local rule covers.

## What ships with the plugin

An installed plugin is copied to `~/.claude/plugins/cache/` without its parent
directories, so every file a skill reads travels with it under `reference/`:

- `criteria.md` — every criterion type and its fields, **generated** from Coder
  Eval's own `SuccessCriterion` model union (`make plugin-reference`), so it
  cannot drift from the schema the CLI validates against.
- `task-rubric.md` — the adversarial task-quality checklist `task` applies to work it
  writes and `lint-tasks` applies to task files already on disk: could this task pass for
  the wrong reason, does it grade behavior or a self-report, do its fixtures reset and
  clean up.
- `cli-setup.md` — how the CLI-driving skills handle a missing `coder-eval`
  binary: offer the install, ask first, verify it worked.
- `run-layout.md` — the on-disk run-directory contract `analyze` reads: what is *inside*
  a run directory.
- `repo-layout.md` — how a skill finds *where* your eval tree is, by globbing for
  `task_id:` files and `run.json` rather than assuming `tasks/` and `runs/`. All six
  skills read it, which is what lets them work in a repository that names or nests the
  tree differently.
- `templates/` — the canonical activation suite `check-skill` copies.

## Related

- The plugin's own README lives at
  [`plugins/coder-eval/README.md`](https://github.com/UiPath/coder_eval/blob/main/plugins/coder-eval/README.md).
- For the GitHub Action the `ci` skill emits — its inputs, JUnit output and score
  floor — see [CI Gate & GitHub Action](CI_GATE.md).
- For the criterion vocabulary in full, see the
  [Task Definition Guide](TASK_DEFINITION_GUIDE.md).
