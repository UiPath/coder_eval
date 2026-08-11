---
description: Set up coder-eval in this repository — scan for what is worth evaluating (Claude Code skills, an MCP server, a CLI), then scaffold a task directory with one real, passing-or-failing task and the exact command to run it.
disable-model-invocation: true
allowed-tools: ["Read", "Glob", "Grep", "Write", "Bash"]
---

# Set up coder-eval in this repository

Goal: leave the user with a task directory containing **one real task** they can run
immediately, not an empty scaffold. The task must exercise something this repository
actually ships.

The user's request is: `$ARGUMENTS`

## Step 1 — Check prerequisites

Run `coder-eval --version`. Installing this plugin did not install the CLI, and
every later step needs it.

If it is missing, follow `${CLAUDE_PLUGIN_ROOT}/reference/cli-setup.md`: offer the
install, **ask before running it**, and confirm with `coder-eval --version`
afterwards. Never install unprompted, and do not continue if the user declines.

## Step 2 — Scan for what is testable

Look for these, in priority order, and **report what you found before writing
anything**:

1. **Claude Code skills** — glob `.claude/skills/*/SKILL.md` and `**/skills/*/SKILL.md`.
   Skills are the highest-value thing to evaluate, because whether they trigger is
   invisible until it fails. If you find any, recommend `/coder-eval:skill-check` for
   each of them — that is a purpose-built activation suite, not something to hand-roll
   here.
2. **An MCP server** — an `.mcp.json`, an `mcpServers` key in `package.json` or
   `pyproject.toml`, or a server entry point (a `server.py` / `index.ts` that registers
   tools). Note which tools it exposes.
3. **A CLI entry point** — `[project.scripts]` in `pyproject.toml`, `bin` or `scripts` in
   `package.json`, or a `Makefile` with usable targets.

If the repository is a monorepo with many skill or package directories, cap the scan
and ask which subtree to focus on rather than reporting fifty candidates.

If you find **nothing** in these three categories, say so plainly. Then offer the
smallest useful thing instead — a task that runs a script or test command the repo
already has and checks its output — rather than scaffolding an empty suite that proves
nothing.

## Step 3 — Scaffold one real task

Work out where tasks should live by following
`${CLAUDE_PLUGIN_ROOT}/reference/repo-layout.md`. If the repository already has a task
tree, write into it; if it has none, propose a location and ask.

If that directory already exists and holds YAML files with a `task_id:` key, **never
overwrite them**. Report what is already there and add alongside it.

Write one task derived from what step 2 found:

- **A CLI** — a task whose prompt asks for something the CLI does, with criteria that
  check the resulting file's *content*, not just that a file appeared.
- **An MCP server** — a task that exercises one specific tool and verifies its effect.
- **Skills** — point at `/coder-eval:skill-check` instead; an activation suite is a
  different shape from a capability task and that skill builds it properly.

Prompts instruct, criteria validate. Do not restate in the prompt what the criteria
check — a prompt that says "make sure the file contains X" tests reading
comprehension, not capability.

Before writing the criteria, read `${CLAUDE_PLUGIN_ROOT}/reference/task-rubric.md`. It is
the shared checklist for whether a task can pass for the wrong reason, and the one task you
scaffold here is the example every later task in this repository gets modelled on — so it
is worth getting right rather than fixing later. For criterion types and their fields, read
`${CLAUDE_PLUGIN_ROOT}/reference/criteria.md`.

Use `/coder-eval:task` if the user wants more tasks after this one; it is the same
authoring loop with a natural-language brief.

## Step 4 — Environment variables

Write the variables the chosen agent needs (e.g. `ANTHROPIC_API_KEY` for the default
`claude-code` agent) to **`.env.example`** — create it or append to it.

Never write `.env`: it may already hold real secrets. If `.env` does not exist, check
whether it is gitignored before suggesting the user create one, and say so if it is
not.

## Step 5 — Validate

Run `coder-eval plan <task-directory>/*.yaml` and iterate until it exits 0. This
validates the task schema through the real models, so a field name you guessed wrong
surfaces here. `plan` takes task *files*: a bare directory argument is rejected
outright (`Expected a YAML task file but got a directory`), so always pass explicit
paths or a glob. Do not suggest `coder-eval plan` with no argument — zero-argument
discovery only works from a coder-eval source checkout and exits 1 anywhere else.

An empty or task-less directory does not produce a meaningful success — if `plan`
reports no tasks, treat that as a failure to scaffold, not a pass. Note that the
bare-directory error is a different outcome: that one means you passed the wrong
argument shape, not that the scaffold is empty.

## Step 6 — Report

Tell the user:

- what you found in the scan, and what you chose to evaluate first;
- the exact command to run it: `coder-eval run <path>`, plus a note that it costs real
  tokens and needs the credentials from step 4;
- that `/coder-eval:skill-check` is the next step if the repo ships skills;
- that `/coder-eval:analyze` reads the run directory afterwards, and `/coder-eval:ci`
  turns the suite into a GitHub Actions gate.
