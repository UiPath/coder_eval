---
description: Generate a GitHub Actions workflow that runs a coder-eval suite as a CI gate or on a schedule, using the published composite action — with the agent runtime, credentials, JUnit output and a score floor wired correctly.
disable-model-invocation: true
allowed-tools: ["Read", "Glob", "Grep", "Write", "Bash"]
---

# Wire coder-eval into GitHub Actions

The user's request is: `$ARGUMENTS`

## Step 1 — Check the repository

Find the repository's task tree by following
`${CLAUDE_PLUGIN_ROOT}/reference/repo-layout.md`, and check whether `.github/workflows/`
exists. The paths you resolve here become `args:` entries in the workflow in step 3 — that
input is written from discovery, never from a fixed guess.

If there is no `.github/` directory at all, say that this skill targets GitHub Actions
and stop — do not invent an equivalent for another CI system unless the user asks.

If a workflow already runs coder-eval (grep the workflows for `coder_eval`), do not add a
second one. Show what is there and offer to update it.

## Step 2 — Choose the trigger

Ask, or infer from the request:

- **On pull request** — gate changes to the tasks or to whatever they exercise.
- **On a schedule** — the skill-drift case: re-run the suite weekly against the current
  model so a skill that quietly stops triggering surfaces before users hit it. This is
  the trigger most repositories actually want, and the one they forget. If the suite is
  an activation suite, the environment note in step 3 is **not optional** for this
  trigger — without it the scheduled run reports total drift every week regardless of
  whether anything drifted.
- **Both**, which is fine — one workflow, two `on:` keys.

## Step 3 — Emit the workflow

The composite action installs the `coder-eval` CLI and nothing else: it is
agent-agnostic and installs **no coding-agent runtime**. A task using the default
`claude-code` agent therefore needs Node plus the Claude CLI provided by the job first,
or the run dies on a missing `claude` binary. There is no Marketplace install step for
the action itself, but those two prerequisite steps are not optional.

```yaml
name: Coder Eval

on:
  pull_request:
  schedule:
    - cron: "0 6 * * 1"   # Mondays 06:00 UTC — catches model/skill drift

# Least privilege: this job runs agent-generated code, so it gets no write scope.
permissions:
  contents: read

jobs:
  eval:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v6
        with:
          # Do not leave a credentialed .git/config in a workspace where
          # agent-generated code runs.
          persist-credentials: false

      # The action installs no coding-agent runtime — provide it here.
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
      - run: npm install -g @anthropic-ai/claude-code

      - id: eval
        uses: UiPath/coder_eval@v0
        with:
          run-dir: runs/ci
          args: |
            tasks/**/*.yaml
            --model
            claude-haiku-4-5-20251001
          env: |
            ANTHROPIC_API_KEY=${{ secrets.ANTHROPIC_API_KEY }}

      - if: always()
        run: cat "${{ steps.eval.outputs.run-md-path }}" >> "$GITHUB_STEP_SUMMARY"
```

Adjust the model and the cron to the repository. Pin the action at `@v0`, the moving major
tag. Then work through the four things the snippet cannot guess.

Note the shape of `args:`: the action promotes **none** of `coder-eval run`'s flags to a
named input, so task paths and flags all go there, one argument per line, with a flag and
its value on separate lines. There is no `tasks:`, `tags:` or `model:` input.

### The task paths — from discovery

The value above is a placeholder for whatever step 1 discovered. `args:` entries are
handed to the CLI **verbatim**: no word splitting, no pathname expansion. The CLI expands
the globs itself, which makes this simpler than it looks:

- **`**` works.** `tasks/**/*.yaml` is genuinely recursive, so one pattern covers a tree
  of any depth. No per-depth ladder, no `globstar` caveat.
- **A glob matching nothing fails loudly** with `No task files found!` and exit 1, rather
  than reaching the CLI as a literal path.
- **One path per line.** Two globs are two lines, not one space-separated string, which
  would arrive as a single malformed argument.

So emit what step 1 found, one entry per line:

```yaml
args: |
  tests/tasks/**/*.yaml
```

### `version:` — conditional on the repository's pin

If the repository pins a coder-eval version, **pass it** and say why: the gate should run
the CLI the repo is authored against, not whichever release the action defaults to. If
there is no pin, omit the input and let the action's default track the matching release.
`${CLAUDE_PLUGIN_ROOT}/reference/cli-setup.md` covers how to find a pin — and passing one
explicitly is right even when it happens to match today's default, because it is
self-documenting.

### The experiment, if the suite runs through one

If the repository's suite resolves through an experiment, the workflow must pass it in
`args` — two lines, and with the discovered path, not the illustrative one below:

```yaml
args: |
  tests/tasks/**/*.yaml
  -e
  tests/experiments/default.yaml
```

This matters rather than being tidy: an experiment usually supplies `agent:` config, so
omitting it silently changes what the run measures — the gate and the local run stop
being the same test. If the repository has **several** experiments, ask which one the
gate should use; a CI gate quietly running the wrong experiment is precisely the failure
this exists to prevent.

### Environment — including the skill source, if the suite is an activation suite

If the resolved experiment or the tasks interpolate environment variables, pass them
through the action's `env:` input alongside the credentials. Missing ones do not fail
loudly — the run just measures the wrong thing.

One case is common enough to check for by name. An activation suite loads the skill under
test through `agent.plugins`, whose `path` is an environment variable so the committed
task stays portable — the suite `/coder-eval:check-skill` writes uses `SKILL_SOURCE_PATH`.
**Grep the discovered tasks for `$` in an `agent.plugins` path and pass every variable you
find**, resolved against the checkout:

```yaml
env: |
  ANTHROPIC_API_KEY=${{ secrets.ANTHROPIC_API_KEY }}
  SKILL_SOURCE_PATH=${{ github.workspace }}/.claude
```

Use the directory the repository actually keeps skills in, from step 1, not the path
above — and note **what level that variable points at**. A local plugin path must be a
**plugin root**: a directory holding a `skills/` subdirectory, so the skill sits at
`<path>/skills/<name>/SKILL.md`. For `.claude/skills/my-skill/SKILL.md` that is `.claude`,
**not** `.claude/skills`. Pointing one level too deep loads nothing at all and produces the
same permanent red as leaving it unset.

If the suite stages a minimal root (which `/coder-eval:check-skill` recommends, so that
sibling subagents and commands under `.claude` cannot confound the measurement), the
workflow has to build it before the run — a scheduled job has no shell history to
inherit it from:

```yaml
- name: Stage the skill under test as a minimal plugin root
  run: |
    mkdir -p "$RUNNER_TEMP/skill-root/skills"
    cp -R "${{ github.workspace }}/.claude/skills/my-skill" "$RUNNER_TEMP/skill-root/skills/"
    echo "SKILL_SOURCE_PATH=$RUNNER_TEMP/skill-root" >> "$GITHUB_ENV"
```

This is the one omission the scheduled trigger cannot survive: unset, the skill is
never offered to the sandboxed agent, every positive row scores 0, and the job fails its
`recall` threshold every week — a permanent red that looks exactly like the drift the
schedule exists to detect, so the real thing goes unnoticed when it arrives.

## Step 4 — Credentials

Credentials go through the action's `env:` passthrough, sourced from repository secrets,
as in the snippet above. It is the only channel: values are exported for the coder-eval
process only, not written to `$GITHUB_ENV`, so nothing leaks into later steps.

Never inline a key literal, and never commit one. If the repository has no
`ANTHROPIC_API_KEY` secret, say which secret to add and where.

## Step 5 — Reports

The action reports three paths as outputs and writes no report anywhere itself:

- `junit-path` — the JUnit XML at `<run-dir>/junit.xml`, which GitHub and most
  test-report tooling ingest to show per-task pass/fail.
- `run-md-path` — the run's markdown report. Append it to the job summary, as the
  snippet does, so a reviewer sees the scores without downloading anything. The action
  deliberately does not do this for you: a workflow that has to redact the report first
  cannot undo a write that already happened.
- `run-dir` — the whole run directory.

Consider uploading the run directory as an artifact on failure so a failing gate can be
analyzed with `/coder-eval:analyze` afterwards.

The step's exit code is coder-eval's own: non-zero on any failed task. There is no score
floor input; a suite that needs one gates on `run.json` in a following step.

## Step 6 — Warn about fork PRs, and explain the two hardening lines

Evaluated tasks execute agent-generated code. Never run this under
`pull_request_target` with secrets exposed to untrusted fork PRs — that combination
hands a fork's code your API keys. If the repository takes outside contributions, use
`pull_request` and accept that fork PRs will not have the secret (as the repository's own
runs do), or gate the job on the PR being from the same repository.

Say why the workflow carries `permissions: contents: read` and
`persist-credentials: false`, so neither gets dropped as boilerplate. Both follow from the
same fact: **this job runs agent-generated code on the runner**, and the default `tempdir`
sandbox driver is not an OS-level confinement boundary. Without them the job inherits the
repository's default `GITHUB_TOKEN` scope — still write-all in many organizations — and
`actions/checkout` leaves that token in `.git/config` in the very workspace the agent's code
executes in, so a misbehaving or prompt-injected task could push to the repository. Neither
line costs anything: the eval only needs to read the checkout.

If a task genuinely needs to write back (committing a baseline, say), add that one permission
explicitly to that job rather than restoring the default.
