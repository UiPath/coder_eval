---
description: >-
  Run Coder Eval as a CI gate — the packaged composite GitHub Action, JUnit XML
  output for test-report ingestion, and an optional per-task score floor.
---

# CI Gate: GitHub Action & JUnit reports

Coder Eval ships a **packaged CI gate**: a composite GitHub Action that installs
the CLI, runs your tasks, emits a JUnit XML report, appends the run summary to the
job summary, and fails the build on any task/gate failure. This page is the
reference for the Action and the JUnit output. For a step-by-step walkthrough
(including a hand-rolled workflow), see
[Tutorial 02 — Running Coder Eval in CI](tutorials/02-ci-pipeline.md).

## The GitHub Action

The action is published on the GitHub Actions Marketplace as
[**coder_eval**](https://github.com/marketplace/actions/coder_eval). It is a
composite action living at the repo root (`action.yml`), so you reference it by
repo path — there is nothing to install:

```yaml
- uses: UiPath/coder_eval@v0    # becomes @v1 once 1.0.0 ships; @vX.Y.Z pins exactly
  with:
    tasks: tests/tasks/**/*.yaml
    model: claude-sonnet-5
    env: |
      ANTHROPIC_API_KEY=${{ secrets.ANTHROPIC_API_KEY }}
```

The action is **agent-agnostic** — it installs `coder-eval` but *not* any
coding-agent runtime. Tasks using the default `claude-code` agent need the
`claude` CLI on `PATH` (Node + `@anthropic-ai/claude-code`), provided by your job
*before* this step runs.

### Inputs

| Input | Default | Purpose |
| --- | --- | --- |
| `tasks` | *(all `tasks/`)* | Task YAML path(s)/glob passed to `coder-eval run`. |
| `tags` | — | Only run tasks matching these comma-separated tags (`--tags`). |
| `model` | — | Override agent model for all tasks (`--model`). |
| `extra-args` | — | Extra args appended verbatim to `coder-eval run` (`--experiment`, `-D …`, `--exclude-tags`, …). Trusted caller input. |
| `version` | pinned release | `coder-eval` version to install from PyPI, or `local` to install from the action checkout. |
| `run-dir` | `runs/ci` | Run directory (`--run-dir`). |
| `junit-path` | `coder-eval-junit.xml` | Where to write the JUnit XML report. |
| `step-summary` | `true` | Append `run.md` to the GitHub job summary. |
| `env` | — | Credential/backend passthrough (see below). |
| `minimum-task-score` | *(off)* | Optional strict per-task score floor (see below). |

### Outputs

| Output | Description |
| --- | --- |
| `run-dir` | The run directory containing `run.json` / `run.md`. |
| `junit-path` | Path to the written JUnit XML report. |

### Credentials via `env`

**`env` is the sole channel for credentials and backend config.** It takes
newline-separated `NAME=VALUE` pairs, exported for the `coder-eval` process
**only** — scoped to the run step, never written to `$GITHUB_ENV`, so a forwarded
secret can't bleed into later job steps. Names must match
`^[A-Za-z_][A-Za-z0-9_]*$`; blank lines and `#` comments are ignored. Always wire
values from repository secrets — never inline a secret literal.

```yaml
- uses: UiPath/coder_eval@v0
  with:
    tasks: tests/tasks/**/*.yaml
    env: |
      ANTHROPIC_API_KEY=${{ secrets.ANTHROPIC_API_KEY }}
      API_BACKEND=direct
```

Set whatever the run needs — `ANTHROPIC_API_KEY`, `API_BACKEND`, Bedrock/model
vars, `GEMINI_API_KEY` for Antigravity, `EVALBOARD_*`, plugin paths, etc. See the
[User Guide → Environment Variables](USER_GUIDE.md#environment-variables) and the
per-agent guides ([Claude Code](agents/CLAUDE_CODE.md) · [Codex](agents/CODEX.md) ·
[Antigravity](agents/ANTIGRAVITY.md)) for what each backend needs.

### The score floor (`minimum-task-score`)

An **additional** gate on top of `coder-eval`'s own exit code. Set a float in
`[0.0, 1.0]` and the step fails if **any** scored task, in any variant, has a
`weighted_score` below it — *or* if `coder-eval` itself exits non-zero (both
verdicts surface). It reads the always-written `run.json` spine
(`task_results[*].weighted_score`), so it works for plain and experiment runs
alike. Errored tasks (null score) are left to `coder-eval`'s exit code; a
malformed/`NaN` score fails closed. Empty (the default) disables the floor.

### Security

Evaluated tasks execute agent-generated code. **Do not** run this action under
`pull_request_target` with secrets exposed to untrusted fork PRs. For untrusted
tasks, use the [Docker driver](DOCKER_ISOLATION.md) — the `tempdir` driver is not
a security boundary.

> The README's [Use as a GitHub Action](https://github.com/UiPath/coder_eval#use-as-a-github-action)
> section carries the same reference alongside a copy-paste workflow.

## JUnit XML output

Any run can emit a JUnit XML report — the lingua franca CI platforms understand for
per-test annotations, history, and flake tracking. Two entry points, one code path
(so they can't drift):

```bash
# During a run
coder-eval run tasks/*.yaml --junit-xml coder-eval-junit.xml

# After the fact, from a finished run dir
coder-eval report runs/latest -f junit               # writes runs/latest/junit.xml
coder-eval report runs/latest -f junit -o out.xml    # custom path
```

The report is built **from the finalized run directory on disk** — the `run.json`
spine (required), any `suite.json` gates (optional), and per-failed-row `task.json`
for failure detail (best-effort). Each task result maps to a JUnit `testcase`;
failures/errors carry a (capped) detail body, and statuses map through
`FinalStatus.category` (`succeeded` / `failed` / `error`). See the
[Report Schema](REPORT_SCHEMA.md) for the underlying fields.

### Ingesting the report

**GitHub Actions** — [`mikepenz/action-junit-report`](https://github.com/mikepenz/action-junit-report):

```yaml
- uses: mikepenz/action-junit-report@v5
  if: always()
  with:
    report_paths: coder-eval-junit.xml
```

**Azure DevOps** — `PublishTestResults@2`:

```yaml
- task: PublishTestResults@2
  inputs:
    testResultsFormat: 'JUnit'
    testResultsFiles: 'coder-eval-junit.xml'
```

## See also

- [Tutorial 02 — Running Coder Eval in CI](tutorials/02-ci-pipeline.md) — the walkthrough
- [Report Schema](REPORT_SCHEMA.md) — the JSON the JUnit report is built from
- [User Guide](USER_GUIDE.md) — the `run` / `report` commands and environment variables
