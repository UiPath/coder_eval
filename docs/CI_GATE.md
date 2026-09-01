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
    args: |
      tests/tasks/**/*.yaml
      --model
      claude-sonnet-5
    env: |
      ANTHROPIC_API_KEY=${{ secrets.ANTHROPIC_API_KEY }}
```

The first two steps are there because the action is **agent-agnostic** — it
installs `coder-eval` but *not* any coding-agent runtime. Tasks using the default
`claude-code` agent need the `claude` CLI on `PATH` (Node +
`@anthropic-ai/claude-code`), provided by your job *before* the action runs; swap
those steps for your own agent's runtime as needed.

### Inputs

Eight, and **none of them is a `coder-eval run` flag**. The CLI has 21 flags; an
input that merely forwards one buys nothing and costs a lot, because GitHub
silently *ignores* an input the referenced tag does not define. A forwarding input
that is mistyped, or newer than the tag you pinned, produces a run that measured
something else and still exits 0. A wrong CLI flag is a hard error instead. So
every flag goes through `args`, and an input exists only where the action does
something with the value besides pass it along.

| Input | Default | Purpose |
| --- | --- | --- |
| `args` | — | Everything for `coder-eval run` — task paths/globs and every flag — one argument per line, appended verbatim. See below. |
| `version` | pinned release | `coder-eval` version to install from PyPI, or `local` to install from the action checkout. |
| `extras` | — | Comma-separated `coder-eval` extras, composed into the install requirement (`codex`, `antigravity,litellm`). |
| `extra-packages` | — | Extra requirements installed into `coder-eval`'s environment (`uv tool install --with`), one per line. |
| `install-flags` | — | Flags for `uv tool install`, one per line (`--prerelease=allow`, `--extra-index-url …`). |
| `env` | — | Credential/backend passthrough (see below). |
| `working-directory` | `.` | Directory every step of the action runs in — see below. |
| `run-dir` | `runs/ci` | Run directory (`--run-dir`). Also where the reports are written. |

#### Writing `args`

**One argument per line, appended verbatim.** No word splitting, no pathname
expansion. A flag and its value are **two lines**, or one line in `=` form:

```yaml
args: |
  tests/tasks/**/*.yaml
  --tags
  smoke
  --model=claude-sonnet-5
  -D
  sandbox.docker.env_passthrough_extra=[AUTH_TOKEN,BASE_URL]
```

A flag and value sharing a line arrive as a single malformed token, which the CLI
rejects. Blank lines, `#` comments and surrounding whitespace are ignored.

Verbatim is the point. A `-D` override whose value is a bracketed list is a bash
character class, so any input that split on whitespace would leave it intact only
while no file in the working directory happened to match — one named
`sandbox.docker.env_passthrough_extra=A` would rewrite it to a single-name list
and the run would measure something other than what the workflow asked for,
silently.

Task globs are handed to the CLI **unexpanded**, and it expands them itself:

- **`**` works.** `tests/tasks/**/*.yaml` is recursive, no `globstar` needed.
- **A glob matching nothing exits 1** with `No task files found!`, rather than
  reaching the CLI as a literal path or vanishing.
- **Omitting `args` entirely does not run your suite.** Zero-argument discovery
  resolves against `tasks/` relative to the working directory. Pass your paths.

#### Extras and plugins (`extras`, `extra-packages`, `install-flags`)

The action installs the CLI with `uv tool install`, which builds an isolated
environment whose shims **shadow** anything else named `coder-eval` on `PATH`.
Pre-installing your own copy beside it therefore does not work: the action's copy
is the one that runs. These inputs exist because of that.

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

`install-flags` passes resolver flags through, one per line, for what the install
needs and the action does not model:

```yaml
install-flags: |
  --prerelease=allow
  --extra-index-url
  https://my-private-index.example/simple
```

#### Running from a subdirectory (`working-directory`)

A suite that lives under `tests/` needs the run to happen there, and GitHub
rejects `working-directory:` on a `uses:` step — a job-level `defaults.run` does
not reach inside a composite action either. The `working-directory` input is the
way in. It applies to **every** step the action runs, so `run-dir`, the task
paths in `args` and relative `extra-packages` entries all resolve against it, and
the `run-dir` output is reported exactly as passed (a relative one is relative to
that directory, which matters when a later step reads it from the job's default
cwd).

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

### Outputs

| Output | Description |
| --- | --- |
| `run-dir` | The run directory, as passed, containing `run.json` / `run.md`. |
| `junit-path` | The JUnit XML report, at `<run-dir>/junit.xml`. |
| `run-md-path` | The markdown run report, at `<run-dir>/run.md`. |

There is no `junit-path` **input**: the report belongs with the run it describes,
and every consumer that had the choice put it there anyway.

The action does not append the report to `$GITHUB_STEP_SUMMARY`. A consumer that
must redact the report first cannot undo a write that has already happened, so the
write is yours to make:

```yaml
- id: eval
  uses: UiPath/coder_eval@v0
  with: { args: "tests/tasks/**/*.yaml" }
- if: always()
  run: cat "${{ steps.eval.outputs.run-md-path }}" >> "$GITHUB_STEP_SUMMARY"
```

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
    args: tests/tasks/**/*.yaml
    env: |
      ANTHROPIC_API_KEY=${{ secrets.ANTHROPIC_API_KEY }}
      API_BACKEND=direct
```

Set whatever the run needs — `ANTHROPIC_API_KEY`, `API_BACKEND`, Bedrock/model
vars, `GEMINI_API_KEY` for Antigravity, `EVALBOARD_*`, plugin paths, etc. See the
[User Guide → Environment Variables](USER_GUIDE.md#environment-variables) and the
per-agent guides ([Claude Code](agents/CLAUDE_CODE.md) · [Codex](agents/CODEX.md) ·
[Antigravity](agents/ANTIGRAVITY.md)) for what each backend needs.

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
