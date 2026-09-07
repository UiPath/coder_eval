---
description: >-
  Run Coder Eval in GitHub Actions — install the CLI, execute your coding-agent
  evaluation suite on demand or on a schedule, and gate merges on a pass/fail
  verdict.
---

# Tutorial 02 — Running Coder Eval in CI

Run your evaluation suite automatically in GitHub Actions: on demand, on a
schedule, or as a merge gate. By the end you'll have a workflow that installs
`coder-eval`, runs a set of tasks, turns the results into a pass/fail verdict, and
uploads the reports as artifacts.

This mirrors how the [UiPath skills repo](https://github.com/UiPath/skills)
drives its task tree under
[`tests/tasks/`](https://github.com/UiPath/skills/tree/main/tests/tasks) — a task
directory checked into the repo, a pinned `coder-eval` version, and a
`task.json`-based verdict — distilled to the parts that transfer to any project.

## The shape of a Coder Eval CI job

Every CI setup, however elaborate, is the same five steps:

1. **Install** a pinned `coder-eval` and set up Python/uv.
2. **Provide credentials** via repository secrets (never hard-code keys).
3. **Run** `coder-eval` over a glob of task files into a known run directory.
4. **Gate** — parse the per-task `task.json` files into a pass/fail exit code.
5. **Upload** the run directory as an artifact for later inspection.

## 1. Lay out your tasks

Keep your task YAMLs in the repo — e.g. a `tests/tasks/` tree grouped by area:

```
tests/
└── tasks/
    ├── group-a/
    │   ├── task-one.yaml
    │   └── task-two.yaml
    └── group-b/
        └── task-three.yaml
```

## 2. Add the workflow

Create `.github/workflows/coder-eval.yml`:

```yaml
name: Coder Eval

on:
  workflow_dispatch:
    inputs:
      task_globs:
        description: 'Space-separated globs, e.g. tests/tasks/group-a/**/*.yaml'
        type: string
        required: true
        default: 'tests/tasks/**/*.yaml'
      parallelism:
        description: 'Parallel tasks (-j). Keep ≤ runner vCPUs (ubuntu-latest = 4).'
        type: string
        default: '4'
  # Or run on a schedule to keep skills up to date as models change — a skill
  # that quietly stops triggering shows up as a failing task before users hit it:
  # schedule:
  #   - cron: '0 6 * * *'   # 06:00 UTC daily
  # Or gate merges:
  # pull_request:
  #   paths: ['tests/tasks/**']

concurrency:
  group: coder-eval-${{ github.ref }}
  cancel-in-progress: false

jobs:
  eval:
    runs-on: ubuntu-latest
    timeout-minutes: 120
    env:
      # Usage telemetry is on by default — turn it off in CI.
      TELEMETRY_ENABLED: 'false'
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'

      - uses: astral-sh/setup-uv@v4

      - name: Install coder-eval
        # Install the published package. In a real gate, pin an exact released
        # version (coder-eval==X.Y.Z) so a harness upgrade can't move your results.
        run: uv pip install --system coder-eval

      - name: Run evaluations
        env:
          # Provide exactly the credentials your tasks need.
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          TASK_GLOBS: ${{ inputs.task_globs }}
          PARALLELISM: ${{ inputs.parallelism }}
        run: |
          # TASK_GLOBS is intentionally unquoted so the shell expands the globs.
          coder-eval run $TASK_GLOBS -j "${PARALLELISM:-4}" -v

      - name: Verdict (fail the job if any task didn't pass)
        if: always()
        run: |
          set -uo pipefail
          run_dir=$(ls -td runs/*/ 2>/dev/null | head -n 1 || true)
          if [ -z "$run_dir" ]; then
            echo "FAIL — no run directory produced" | tee -a "$GITHUB_STEP_SUMMARY"
            exit 1
          fi
          python - "$run_dir" <<'PY' | tee -a "$GITHUB_STEP_SUMMARY"
          import json, pathlib, sys
          rows = [json.loads(p.read_text(encoding="utf-8"))
                  for p in sorted(pathlib.Path(sys.argv[1]).rglob("task.json"))]
          for r in rows:
              print(f"- {r.get('task_id','?')}: {r.get('final_status','?')}")
          ok = sum(1 for r in rows if r.get("final_status") == "SUCCESS")
          print(f"\n**{ok}/{len(rows)} PASS**")
          sys.exit(0 if rows and ok == len(rows) else 1)
          PY

      - name: Upload run reports
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: coder-eval-${{ github.run_id }}
          # `**` matches the replicate-index segment coder_eval adds
          # (runs/<ts>/<variant>/<task>/<NN>/).
          path: |
            runs/*/run.json
            runs/*/run.md
            runs/**/task.json
            runs/**/task.log
          if-no-files-found: warn
          retention-days: 14
```

## How the pieces work

- **Telemetry off in CI.** `TELEMETRY_ENABLED: 'false'` at the job level keeps CI
  runs out of the usage stream. (See the [User Guide](../USER_GUIDE.md#usage-telemetry).)
- **Pin the version.** Pinning an exact release (`coder-eval==X.Y.Z`) makes runs
  reproducible; bump it deliberately. A single source of truth (a
  `tests/.coder-eval-version` file read in a resolve step) scales better than
  hard-coding it in the YAML.
- **Run directory.** Omitting `--run-dir` lets the harness create `runs/<ts>/` (and a
  `runs/latest` symlink); the verdict step finds the newest via `ls -td runs/*/` and the
  upload globs match `runs/*/run.json`. The per-task result is
  `runs/<ts>/<variant>/<task>/<NN>/task.json` — see [Output Structure](../USER_GUIDE.md#output-structure).
- **The verdict is just `task.json` parsing.** `final_status == "SUCCESS"` is the
  pass condition. `sys.exit(1)` on any non-pass fails the job; writing to
  `$GITHUB_STEP_SUMMARY` puts the table on the run's summary page.
- **`if: always()`** on the verdict and upload steps means you still get results
  even when a task fails.

## Shortcut: the packaged action

The five steps above spell out the mechanics, but Coder Eval also ships a
composite action — on the Marketplace as
[**coder_eval**](https://github.com/marketplace/actions/coder_eval) — that
bundles install + run + JUnit report + fail-on-failure into one step:

```yaml
      - uses: actions/setup-node@v4      # the claude-code agent needs the Claude CLI…
        with: { node-version: '20' }
      - run: npm install -g @anthropic-ai/claude-code

      - uses: UiPath/coder_eval@v0       # …then run the gate (pin @vX.Y.Z in production)
        with:
          args: |
            tests/tasks/**/*.yaml
            --model
            claude-sonnet-5
          env: |
            ANTHROPIC_API_KEY=${{ secrets.ANTHROPIC_API_KEY }}
```

The action is agent-agnostic — it installs `coder-eval` but not the agent
runtime, so provide the `claude` CLI (or your agent's runtime) in the job first.
See the [CI Gate reference](../CI_GATE.md) for the full inputs table, JUnit
output, the score-floor gate, and the fork/`pull_request_target` security caveat.
The rest of this tutorial's hand-rolled workflow is still useful when you need
finer control than the action's inputs expose.

## Publishing test results (JUnit)

The verdict step above parses `task.json` by hand. If your CI already ingests
JUnit XML — for the per-test annotations, history, and flake tracking most CI
systems render from it — let `coder-eval` emit it directly instead. Add
`--junit-xml` to the run:

```yaml
      - name: Run evaluations
        run: coder-eval run $TASK_GLOBS -j 4 --junit-xml coder-eval-junit.xml
```

The file is written **before** the exit-code decision, so a failing run still
produces a report. You can also regenerate it from any existing run directory
without re-running the suite:

```bash
coder-eval report runs/latest -f junit            # writes runs/latest/junit.xml
coder-eval report runs/latest -f junit -o out.xml # custom path
```

The mapping: one `<testcase>` per task row (grouped into a `<testsuite>` per
variant), failed/errored rows carry a `<failure>`/`<error>` with the per-criterion
breakdown, skipped tasks and failing suite gates get their own synthetic suites.

Feed the file to whatever your platform uses to render test results. On GitHub
Actions, [`mikepenz/action-junit-report`](https://github.com/mikepenz/action-junit-report):

```yaml
      - name: Publish test report
        if: always()
        uses: mikepenz/action-junit-report@v5
        with:
          report_paths: coder-eval-junit.xml
```

On Azure DevOps, `PublishTestResults@2`:

```yaml
      - task: PublishTestResults@2
        condition: always()
        inputs:
          testResultsFormat: 'JUnit'
          testResultsFiles: 'coder-eval-junit.xml'
```

## Secrets

Add these under **Settings → Secrets and variables → Actions**:

| Secret | For |
| --- | --- |
| `ANTHROPIC_API_KEY` | Direct Anthropic API (default backend) |
| `AWS_BEARER_TOKEN_BEDROCK`, `AWS_REGION`, `BEDROCK_MODEL` | Only if you run `--backend bedrock` |

## Next steps

The five steps above are the whole core. As a suite grows you'll layer on cost
guardrails, matrix partitioning, scheduled sweeps, and artifact hygiene — the
`coder-eval run` call stays the same, only the wrapper grows:

- **Full CLI & config reference** → [User Guide](../USER_GUIDE.md)
- **Pin CI agent/model via an experiment** → [A/B Experiments](../AB_EXPERIMENTS.md)
- **A worked, scaled-up pipeline** → the [UiPath skills repo](https://github.com/UiPath/skills)
  workflow (cost guardrail, matrix partitioning, nightly schedule).
