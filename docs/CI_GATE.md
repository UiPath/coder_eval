---
description: >-
  Run Coder Eval as a CI gate — the coder_eval GitHub Action from the Actions
  Marketplace, JUnit XML output for test-report ingestion, and an optional
  per-task score floor.
---

# CI Gate: GitHub Action & JUnit reports

Coder Eval ships a **packaged CI gate**: a composite GitHub Action — on the
Actions Marketplace as
[**coder_eval**](https://github.com/marketplace/actions/coder_eval) — that
installs the CLI, runs your tasks, emits a JUnit XML report, appends the run
summary to the job summary, and fails the build on any task/gate failure. This
page is the reference for the Action and the JUnit output. For a walkthrough
(including a hand-rolled workflow), see
[Tutorial 02 — Running Coder Eval in CI](tutorials/02-ci-pipeline.md).

## The GitHub Action

The action is published on the GitHub Actions Marketplace as
[**coder_eval**](https://github.com/marketplace/actions/coder_eval). It is a
composite action living at the repo root (`action.yml`), so you reference it by
repo path — there is no Marketplace install step:

```yaml
- uses: actions/setup-node@v4      # the claude-code agent needs the Claude CLI…
  with: { node-version: '20' }
- run: npm install -g @anthropic-ai/claude-code

- uses: UiPath/coder_eval@v0       # …then run the gate (@v1 once 1.0.0 ships; @vX.Y.Z pins exactly)
  with:
    tasks: tests/tasks/*.yaml tests/tasks/*/*.yaml
    model: claude-sonnet-5
    env: |
      ANTHROPIC_API_KEY=${{ secrets.ANTHROPIC_API_KEY }}
```

The first two steps are there because the action is **agent-agnostic** — it
installs `coder-eval` but *not* any coding-agent runtime. Tasks using the default
`claude-code` agent need the `claude` CLI on `PATH` (Node +
`@anthropic-ai/claude-code`), provided by your job *before* the action runs; swap
those steps for your own agent's runtime as needed.

### Inputs

| Input | Default | Purpose |
| --- | --- | --- |
| `tasks` | — | Task YAML path(s)/glob(s) passed to `coder-eval run`. Effectively required — see below. |
| `tags` | — | Only run tasks matching these comma-separated tags (`--tags`). |
| `model` | — | Override agent model for all tasks (`--model`). |
| `extra-args` | — | Extra args appended verbatim to `coder-eval run` (`--experiment`, `-D …`, `--exclude-tags`, …), whitespace-split. Trusted caller input. |
| `args` | — | The same, one argument per line, never split or glob-expanded — see below. |
| `version` | pinned release | `coder-eval` version to install from PyPI, or `local` to install from the action checkout. |
| `extras` | — | Comma-separated `coder-eval` extras to install (`codex`, `antigravity,litellm`). |
| `extra-packages` | — | Extra requirements installed into `coder-eval`'s environment (`uv tool install --with`), one per line. |
| `prerelease` | `false` | Allow prerelease versions while resolving the install. |
| `working-directory` | `.` | Directory every step of the action runs in — see below. |
| `run-dir` | `runs/ci` | Run directory (`--run-dir`). |
| `junit-path` | `coder-eval-junit.xml` | Where to write the JUnit XML report. |
| `step-summary` | `true` | Append `run.md` to the GitHub job summary. |
| `env` | — | Credential/backend passthrough (see below). |
| `minimum-task-score` | *(off)* | Optional strict per-task score floor (see below). |

#### Writing the `tasks` glob

Always pass `tasks` explicitly, and spell out each depth you actually have:

```yaml
tasks: tests/tasks/*.yaml tests/tasks/*/*.yaml
```

Three sharp edges make that worth the words:

- **Omitting `tasks` does not run everything.** The value is shell-expanded into the
  `coder-eval run` argument list, so an empty one invokes the CLI with no paths — and
  zero-argument discovery resolves against the *installed package's* location, not your
  checkout. It finds nothing and exits 1.
- **Do not use `**`.** The expansion happens with `globstar` off, so
  `tests/tasks/**/*.yaml` collapses to `tests/tasks/*/*.yaml` and **silently drops every
  top-level task** — the gate goes green having never run them.
- **Only list depths that match.** `nullglob` is off too, so a pattern matching nothing
  reaches the CLI verbatim and fails the run with
  `Error: Task file not found: tests/tasks/*/*/*.yaml`.

An explicit file list is always safe, and is the better choice for a small suite.

#### `args` vs `extra-args`

Both append to `coder-eval run`; they differ in how the value is tokenized.
`extra-args` is one string, split on whitespace and pathname-expanded — the same
mechanism as `tasks`, and convenient for ordinary flags. `args` takes **one
argument per line** and appends each verbatim, with no splitting and no globbing.

Reach for `args` whenever a value contains whitespace or a glob metacharacter
(`[`, `]`, `*`, `?`). The canonical case is a `-D` override whose value is a
bracketed list, which bash reads as a character class:

```yaml
args: |
  -D
  sandbox.docker.env_passthrough_extra=[AUTH_TOKEN,BASE_URL]
```

Through `extra-args` that value is intact only as long as no file in the working
directory happens to match the class — one named
`sandbox.docker.env_passthrough_extra=A` rewrites it to a single-name list, and
the run measures something other than what the workflow asked for, silently.

A flag and its value are **two lines** (`-D`, then the assignment), or one line in
`=` form (`--model=claude-sonnet-5`). A flag and value sharing a line arrive as a
single malformed token, which the CLI rejects. Blank lines, `#` comments and
surrounding whitespace are ignored.

#### Running from a subdirectory (`working-directory`)

A suite that lives under `tests/` needs the run to happen there, and GitHub
rejects `working-directory:` on a `uses:` step — a job-level `defaults.run` does
not reach inside a composite action either. The `working-directory` input is the
way in. It applies to **every** step the action runs, so `tasks`, `run-dir`,
`junit-path` and relative `extra-packages` entries all resolve against it, and the
`run-dir` output is reported exactly as passed (a relative one is relative to that
directory, which matters when a later step reads it from the job's default cwd).

#### Extras and plugins (`extras`, `extra-packages`, `prerelease`)

The action installs the CLI with `uv tool install`, which builds an isolated
environment whose shims **shadow** anything else named `coder-eval` on `PATH`.
Pre-installing your own copy beside it therefore does not work: the action's copy
is the one that runs. Both inputs exist because of that.

`extras` is composed into the requirement string, so agent extras land in the
environment the action actually invokes:

```yaml
extras: codex          # -> coder-eval[codex]==<version>
```

`extra-packages` adds requirements *into* that same environment, one per line —
a PEP 508 specifier or a local path. This is how a `coder-eval` plugin
distributed outside this repo becomes discoverable, since an entry point is only
found when the plugin shares a virtualenv with its host:

```yaml
extra-packages: |
  ./vendor/my-coder-eval-plugin
  some-published-plugin>=1.2
```

`prerelease: "true"` passes `--prerelease=allow` for when `version` or one of
those requirements needs a prerelease to resolve.

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
    tasks: tests/tasks/*.yaml tests/tasks/*/*.yaml
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
