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
exists. The paths you resolve here become the workflow's `tasks:` input in step 3 — that
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
  the trigger most repositories actually want, and the one they forget.
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

      - uses: UiPath/coder_eval@v0
        with:
          tasks: tasks/*.yaml
          model: claude-haiku-4-5-20251001
          junit-path: runs/ci/junit.xml
          step-summary: true
          minimum-task-score: "0.7"
          env: |
            ANTHROPIC_API_KEY=${{ secrets.ANTHROPIC_API_KEY }}
```

The `tasks:` value above is a placeholder for whatever step 1 discovered — substitute the
real paths rather than shipping this one. Adjust `model:` and the cron to the repository
too. Pin the action at `@v0` (the moving major tag) and do **not** pass a `version:`
input — the action's own default tracks the matching coder-eval release.

## Step 4 — Credentials

Credentials go through the action's `env:` passthrough, sourced from repository secrets,
as in the snippet above. It is the only channel: values are exported for the coder-eval
process only, not written to `$GITHUB_ENV`, so nothing leaks into later steps.

Never inline a key literal, and never commit one. If the repository has no
`ANTHROPIC_API_KEY` secret, say which secret to add and where.

## Step 5 — Reports

- `junit-path:` writes a JUnit XML report, which GitHub and most test-report tooling
  ingest to show per-task pass/fail.
- `step-summary: true` appends the run's markdown report to the job summary, so a
  reviewer sees the scores without downloading anything.

Consider uploading the run directory as an artifact on failure so a failing gate can be
analyzed with `/coder-eval:analyze` afterwards.

## Step 6 — Choose the floor

`minimum-task-score` is a strict floor: **every** scored task, in every variant, must
reach it or the step fails. It sits on top of coder-eval's own exit code — the step fails
if either coder-eval fails or any task scores below the floor. Leave it empty to disable
it.

Explain the tradeoff and let the user pick rather than choosing for them: a floor that is
too high makes the gate flaky (agents are nondeterministic), one that is too low never
catches anything. Suggest running the suite once, then setting the floor a little below
the observed minimum.

## Step 7 — Warn about fork PRs, and explain the two hardening lines

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
