---
description: >-
  Full schema reference for Coder Eval task YAML — agent config, sandboxes, run
  limits, dataset fan-out, and all 14 success criterion types with weighted
  0.0–1.0 scoring.
---

# Task Definition Guide

Complete reference for defining evaluation tasks in Coder Eval.

## Table of Contents

- [Task YAML Structure](#task-yaml-structure)
  - [dataset](#dataset)
  - [skip](#skip)
- [Agent Configuration](#agent-configuration)
- [Run Limits](#run-limits)
- [Sandbox Configuration](#sandbox-configuration)
- [Template Sources](#template-sources)
- [Success Criteria](#success-criteria)
  - [Continuous Scoring](#continuous-scoring)
  - [file_exists](#file_exists)
  - [file_contains](#file_contains)
  - [file_check](#file_check)
  - [json_check](#json_check)
  - [run_command](#run_command)
  - [file_matches_regex](#file_matches_regex)
  - [reference_comparison](#reference_comparison)
  - [command_executed](#command_executed)
  - [uipath_eval](#uipath_eval)
  - [llm_judge](#llm_judge)
  - [agent_judge](#agent_judge)
  - [skill_triggered](#skill_triggered)
- [Reference Solutions](#reference-solutions)
- [Pre-Run Commands](#pre-run-commands)
- [Post-Run Commands](#post-run-commands)
- [Simulation (Multi-Turn User Dialog)](#simulation-multi-turn-user-dialog)
- [Command Telemetry](#command-telemetry)
- [Complete Example](#complete-example)

## Task YAML Structure

Every task is a YAML file with this top-level structure:

```yaml
task_id: "my_task"                    # Unique identifier (required)
description: "What this task tests"   # Human-readable description (required)
initial_prompt: "Instructions..."     # Prompt sent to the agent (required)
tags: [smoke, golden, pure-python]    # Optional tags for filtering (kebab-case)

skip: false                           # Optional: quarantine this task (see below)

agent: { ... }                        # Agent configuration (optional, resolved from experiment)
sandbox: { ... }                      # Sandbox configuration (optional, defaults to tempdir)
run_limits: { ... }                   # Optional run-time caps (turns, wall-clock, tokens, USD)
success_criteria: [ ... ]             # List of criteria (required, at least 1)

reference: { ... }                    # Optional reference solution
pre_run: [ ... ]                      # Optional pre-run commands (before agent starts)
post_run: [ ... ]                     # Optional post-run commands
dataset: { ... }                      # Optional dataset fan-out (one task -> N row-tasks)
```

### `dataset`

An optional `dataset:` block fans this single task out into **one sub-task per row**. Each row-task
gets its own sandbox, run directory, and `task.json`; its `task_id` becomes `<task_id>/<row_id>`.
Row values substitute into `initial_prompt` and into the string leaves of `success_criteria` via
`${row.<field>}`. Expansion happens at load time, **before** experiment-variant resolution, so a
variant can never override the dataset.

```yaml
dataset:
  rows:                                # inline rows — mutually exclusive with `paths`
    - id: alpha
      expected: "alpha"
  # paths: ["datasets/rows.jsonl"]     # or JSONL files, relative to this task YAML
  id_field: "id"                       # which row field is the row identifier
  sample_per_stratum: 5                # optional: keep up to N rows per stratum
  stratify_field: "expected_skill"     # which row field defines the stratum
  sample_seed: 1234                    # optional: pin the stratified draw
```

| Field | Default | Description |
|-------|---------|-------------|
| `rows` | `null` | Inline list of row dicts. Mutually exclusive with `paths`; exactly one is required. |
| `paths` | `null` | JSONL file paths **relative to the task YAML**, concatenated in declared order. |
| `id_field` | `"id"` | Row field used as the row identifier. Must be present, unique, and match `^[A-Za-z0-9_][A-Za-z0-9_.\-]*$` (it becomes a directory name). |
| `sample_per_stratum` | `null` | Stratified random sample: keep up to N rows per stratum. Overridden by CLI `--sample`. |
| `stratify_field` | `"expected_skill"` | Row field whose value defines the stratum for `sample_per_stratum`. |
| `sample_seed` | `null` | Seed for the stratified draw. Unset means the sample is **re-drawn every run**; set an integer to pin it. CLI `--sample` is separately fixed-seed and always reproducible. |

Full guide — row sources, substitution rules, sampling precedence, suite-level scoring, and worked
examples: **[Bring Your Own Dataset](DATASETS.md)**.

### `skip`

`skip: true` quarantines a task. The runner records it in `RunSummary.skipped_tasks` at resolution
time and it **never reaches the orchestrator** — no dataset fan-out, no variant resolution, no
sandbox, no API call. Use it to park a task that is blocked on something outside your control
(an upstream bug, a missing service) without deleting the YAML and losing its history.

```yaml
task_id: "codex_disallowed_tools_test"
# Blocked: the Codex SDK doesn't enforce disallowed_tools via config. Re-enable
# once upstream ships the fix.
skip: true
```

Pair it with a comment naming the blocker — a ticket link, an upstream issue — so the next reader
knows what has to change before it can come back. Run quarantined tasks on demand with
`coder-eval run --include-skipped`; CI leaves the flag off, so they stay excluded there.

## Tags

The `tags` list categorizes tasks for selective execution. Each tag is lowercase kebab-case and may
optionally be namespaced as `key:value` where both sides are kebab-case.

```yaml
tags: [smoke, golden, uipath-python, lifecycle:generate, connector:google-tasks]
```

Namespaced tags let downstream tools slice on a single dimension (e.g. queries
filtering on `connector:*`). Bare tags continue to work with existing
`--tags` filters.

**Well-known tags:**

| Tag | Purpose |
|-----|---------|
| `smoke` | Quick sanity check, should always pass |
| `golden` | High-confidence reference tasks for framework validation |
| `basic` | Simple tasks testing core functionality |
| `integration` | Requires external services or network |
| `example` | Demonstration/tutorial tasks, not for CI |
| `uipath-python` | Uses UiPath Python SDK |
| `uipath-langchain` | Uses UiPath + LangChain integration |
| `pure-python` | No external SDK dependencies |
| `template` | Uses template sources |
| `network` | Requires network access |

**CLI filtering:**

```bash
coder-eval run tasks/*.yaml --tags smoke-pass     # Smoke tasks expected to succeed
coder-eval run tasks/*.yaml --tags smoke-fail     # Smoke tasks expected to fail (failure-detection sentinels)
coder-eval run tasks/*.yaml --tags smoke          # Umbrella tag: all smoke tasks (pass + fail buckets)
coder-eval run tasks/*.yaml --tags golden,basic   # Run golden OR basic tasks
coder-eval run tasks/*.yaml --exclude-tags example # Skip example tasks
```

## Agent Configuration

Run-time caps (turns, wall-clock, tokens, USD) are **not** part of this block — they live under
[`run_limits`](#run-limits).

```yaml
agent:
  type: "claude-code"                 # Agent type — optional if supplied via experiment / --type
  permission_mode: "acceptEdits"      # Permission mode (see below)
  allowed_tools:                      # Tools the agent can use
    - "Read"
    - "Write"
    - "Bash"
  model: "claude-sonnet-4-20250514"   # Optional: specific model
  sdk_options:                        # Optional: Claude Code SDK pass-through
    effort: high                      # any non-framework-managed ClaudeAgentOptions field
```

**`sdk_options`** is a typed pass-through dict for Claude Code SDK
`ClaudeAgentOptions` fields that Coder Eval doesn't own directly. Keys are
validated against the SDK's dataclass at YAML load; framework-managed keys
(`model`, `allowed_tools`, `permission_mode`, `hooks`, `mcp_servers`, …)
are rejected. Deep-merged across the 5-layer config chain. Override via
CLI with the repeatable `-D agent.sdk_options.KEY=VALUE`.
**Requires `type: "claude-code"`** to function; on other agent types it raises
an error.

**Permission Modes:**
- `default` — Default permission handling
- `acceptEdits` — Auto-accept file edits (recommended for evaluations)
- `plan` — Agent proposes changes, waits for approval
- `bypassPermissions` — No permission checks (use with caution)

> **Codex note:** `permission_mode` confines the **`claude-code`** agent only. The **`codex`** agent always runs full-access regardless of the mode — its in-process OS sandbox is redundant given Coder Eval's docker/tempdir isolation and unusable on our CI hosts (and on Windows). Run adversarial or untrusted Codex evals under the **docker driver**, which is the OS-level write boundary; the tempdir/host driver is a working directory, not a confinement boundary.

**Agent Types:**
- `claude-code` (default) — Claude Code SDK agent. Supports `sdk_options`, `claude_settings`, and all permission modes.
- `codex` — OpenAI Codex agent (requires `[codex]` extra; set `CODEX_API_KEY` and optional `CODEX_BASE_URL` environment variables).
- `none` — No-op agent: no coding agent runs and no model API call is made. See [No-op / System Tasks](#no-op--system-tasks-type-none) below.

<a id="no-op--system-tasks-type-none"></a>

### No-op / System Tasks (`type: none`)

Set `agent: {type: none}` to run a task with **no coding agent** — "coder-eval
without a coder". coder-eval sets up the sandbox, executes `pre_run`, and checks
the `success_criteria` directly against it. No agent is created, no model API
call is made, no agent loop runs, and the result records `agent_type = none`.

Use it for **system / canary checks** (e.g. verifying Orchestrator or
Integration Service connectivity) that want to reuse the eval infrastructure
(sandbox, reports, evalboard, ADX) but don't need an agent to do anything. It
replaces the no-op-prompt workaround (a dummy `initial_prompt: "Do nothing."`),
which still spins up an agent and burns a real API call.

```yaml
agent:
  type: none
sandbox:
  driver: tempdir
pre_run:
  - command: "uip orchestrator ping"   # the system check happens here (or in a criterion)
success_criteria:
  - type: run_command
    command: "uip orchestrator ping"
    description: Orchestrator is reachable.
```

Contract (enforced at load): a `type: none` task must declare no `initial_prompt`
/ `initial_prompt_file` and no enabled `simulation` (no agent reads them), and
every criterion must be agent-independent — criteria that inspect the agent
trajectory (`command_executed`, `skill_triggered`, `reference_comparison`,
`commands_efficiency`) are rejected. A worked example lives at
[`tasks/agentless_smoke_test.yaml`](https://github.com/UiPath/coder_eval/blob/main/tasks/agentless_smoke_test.yaml).

## Run Limits

`run_limits:` is the single namespace for every run-time cap: the **structural** caps that bound how
long a task may run, and the **budget** caps that bound what it may spend. Any subset of fields is
valid and an empty block is legal — every field defaults to "no limit".

```yaml
run_limits:
  # Structural caps
  max_turns: 20                       # hard cap on agent inner-loop turns per iteration
  expected_turns: 8                   # SOFT efficiency budget (visible turns) — never aborts
  task_timeout: 600                   # wall-clock cap across all iterations, seconds
  turn_timeout: 300                   # per-communicate() timeout, seconds

  # Budget caps
  max_total_tokens: 200000            # cumulative input + output
  max_usd: 2.50                       # cumulative cost

  # Early stop
  stop_early: true                    # end once the armed criteria are decided
```

| Field | Default | Constraint | Description |
|-------|---------|------------|-------------|
| `max_turns` | *unset* | `> 0` | Hard cap on agent inner-loop turns per iteration. Unset uses the SDK default. |
| `expected_turns` | *unset* | `>= 1` | **Soft** target for cumulative visible turns. Exceeding it warns and badges the report; it never aborts. See [`expected_turns`](#expected_turns-soft-efficiency-budget). |
| `task_timeout` | *unset* | `>= 30` | Max seconds for the whole evaluation loop (all iterations). |
| `turn_timeout` | *unset* | `>= 10` | Max seconds for a single agent `communicate()` call. |
| `max_input_tokens` | *unset* | `>= 1` | Max cumulative input (prompt) tokens. |
| `max_output_tokens` | *unset* | `>= 1` | Max cumulative output (completion) tokens. |
| `max_total_tokens` | *unset* | `>= 1` | Max cumulative input + output tokens. Distinct from [`simulation.max_total_tokens`](#simulation-multi-turn-user-dialog) — see the note below. |
| `max_usd` | *unset* | `> 0.0` | Max cumulative cost in USD. Requires per-turn SDK cost reporting. |
| `count_cached_input` | `false` | — | Count `cache_read_input_tokens` toward the input/total budgets. Off by default — cached reads are typically free. |
| `count_cache_creation` | `false` | — | Count `cache_creation_input_tokens` toward the input/total budgets. Off by default. |
| `stop_early` | `false` | — | Opt-in master switch for early-stop-on-criterion. See [`stop_early`](#stop_early-opt-in-early-stop). |

The authoritative source is `src/coder_eval/models/limits.py`. A lint rule (CE030) fails the build if
a field defined there goes undocumented in this guide, so the table can't quietly fall behind the
model.

**Budget-cap semantics:**

- **Checked after each completed agent turn**, and **cumulative** across all of the task's turns.
  There is no mid-turn enforcement, so a single runaway turn can overshoot the cap before the
  between-turns check sees it. Size caps with headroom for one turn.
- **Subject agent only.** Judge (`llm_judge` / `agent_judge`) and user-simulator token spend are
  **not** counted against these caps.
- A breach aborts the task with `FinalStatus.TOKEN_BUDGET_EXCEEDED` (any of the three token caps) or
  `FinalStatus.COST_BUDGET_EXCEEDED` (`max_usd`). Both categorize as `failed` — see
  [Report Schema](REPORT_SCHEMA.md).
- **`max_usd` needs per-turn cost from the SDK.** If no turn reports a cost, the check is **skipped
  with a one-shot warning per task**, not failed. A run can therefore blow past `max_usd` silently
  on a backend that doesn't report cost — don't rely on it as your only guardrail.
- **Cached-read and cache-creation tokens are excluded by default.** `count_cache_creation: true` is
  what makes an input-token budget meaningful for **Codex**, which buckets its fresh (full-price)
  prompt slice into `cache_creation`; with the default `false`, a Codex token budget effectively
  caps output only.

**Setting caps from the CLI** — every field is reachable through `-D`, which merges per-key into
`run_limits` without disturbing the task's other caps:

```bash
coder-eval run task.yaml -D run_limits.max_turns=30 -D run_limits.task_timeout=900
coder-eval run task.yaml -D run_limits.max_usd=2.50 -D run_limits.max_total_tokens=200000
```

> **`run_limits.max_total_tokens` vs. `simulation.max_total_tokens`.** They are different budgets.
> `run_limits.max_total_tokens` caps the **subject agent's** cumulative tokens and **aborts the task**
> with `TOKEN_BUDGET_EXCEEDED`. [`simulation.max_total_tokens`](#simulation-multi-turn-user-dialog)
> caps the **whole dialog** — simulator *plus* agent — and ends the dialog gracefully with
> `stop_reason='budget'`, leaving the task to be scored normally. Set both if you want a hard
> ceiling on a simulated task.

> **No longer supported:** `max_turns` / `turn_timeout` (and top-level
> `task_timeout`) under `agent:` or at the task top level are rejected —
> the agent model's `extra="forbid"` raises a clear validation error.
> They must live under `run_limits:`. (A deprecation shim hoisted them
> automatically until it was removed on 2026-06-01.)

### `expected_turns` (soft efficiency budget)

`run_limits.expected_turns` is a **soft target**, not a cap: the run is never
aborted for exceeding it (use `max_turns` for a hard limit). It's the budget the
dashboard's **"Within Expected Turns"** metric divides by — a task counts as
"within budget" when it succeeds *and* its turn count stays within **1.5×**
`expected_turns`. The run-level headline reports the share of **budgeted** tasks
that did: a budgeted task that failed counts as over budget, while tasks with no
`expected_turns` budget are excluded entirely (success or fail).

The count compared against the budget is **visible turns** — one per tool call
plus one for the agent's final reply — *not* the SDK's `total_turns` (which
counts assistant messages and can bundle several tool calls into one).

Set it to the number of turns a competent agent should need for the task. Pick
budgets consistently across a suite — the headline % is only comparable when
tasks are measured against realistic, like-for-like targets. Omit it (the
default) to exclude a task from the metric entirely.

### `stop_early` (opt-in early stop)

`run_limits.stop_early` (default `false`) ends a single-shot run **early** once
the run's **armed** criteria are decided — so you can raise `max_turns` for the
full-run flavor without paying for turns the smoke flavor doesn't need. A
criterion is *armed* by giving it a `stop_when` (see the criterion-fields table);
`stop_early` is the master switch that turns arming on for the run.

```yaml
run_limits:
  max_turns: 30
  stop_early: true            # opt in; default false leaves behavior unchanged
success_criteria:
  - type: skill_triggered
    skill_name: date-teller
    expected_skill: date-teller
    stop_when: auto           # arm whichever polarity this instance can decide
  - type: file_exists         # not armed → advisory on an early-stopped run
    path: report.md
```

Semantics:

- **Opt-in, per run.** With `stop_early: false` (the default) the run behaves
  exactly as before — `stop_when` is inert and every criterion gates normally.
- **Polarity.** `stop_when: pass` stops the moment all **pass-armed** criteria are
  decided in the pass direction; `stop_when: fail` stops on a definitive
  wrong-signal fail; `stop_when: decided` stops on either (the criterion instance
  must be able to decide **both**). `stop_when: auto` arms whichever polarities
  **this instance** can decide — use it when the decidable polarity is
  instance-dependent, e.g. a `skill_triggered` activation suite where a positive
  row (`skill_name == expected_skill`) can only live-pass and a distractor can only
  live-fail, so one static value on a dataset-fanned criterion (whose
  positive/distractor role flips per row) cannot fit every row. A **pass-stop**
  needs every pass-armed criterion to pass — fail-armed distractors are not
  required to, and a row with **zero** pass-armed criteria (e.g. a negative row)
  never pass-stops; a **fail-stop** fires on the first fail-armed criterion that
  live-fails, but is **deferred while any pass-armed criterion is still
  undecided** — a distractor misfire on an early tool call must not cut a
  positive row before its expected signal can appear (that would freeze a
  would-be true positive as a false negative and deflate suite recall). The
  misfire is latched, so the deferred fail-stop fires the moment every
  pass-armed criterion decides; if none ever decides, the run simply continues
  to the cap. Only criteria that can decide from a partial trajectory (currently
  `skill_triggered`, `command_executed`) may be armed — arming any other criterion
  is a hard error at resolution (plan *and* run), never a silent no-op.
  Decidability can also depend on a criterion's own fields: `command_executed` can
  live-**pass** only with `max_count` unset and `min_count > 0`, and live-**fail**
  only with `max_count` set (which includes the `min_count: 0, max_count: 0`
  "must-NOT-run" form). Arming a polarity the configured criterion can never reach
  (e.g. `stop_when: pass` alongside a `max_count`, or `auto` on an instance that
  can decide neither) is likewise a hard error at resolution, not a silent full
  run.
- **Verdict.** An early-stopped run is gated on the **armed subset only**; the
  non-armed criteria become **advisory** and are clearly marked (report badge +
  per-criterion note + `stopped_early` row). A run that completes naturally is
  gated on the **full** set, as always. This is what lets one file serve both a
  `smoke` flavor (`stop_early: true`) and an `e2e` flavor (`stop_early: false`) —
  see [AB_EXPERIMENTS.md](AB_EXPERIMENTS.md). Verdict parity between the flavors
  is one-sided: a **fail-stop** is verdict-preserving (the deferral above
  guarantees every pass-armed signal was allowed to resolve first), but a
  **pass-stop** cuts the run once the positives are decided, so a distractor that
  would misfire on a *later* tool call is not observed (the frozen row scores as a
  clean pass) — the smoke flavor trades some precision completeness for budget, so
  authoritative precision/recall belongs on the `stop_early: false` run.
- **Fail-safe.** A live-verdict bug **fails open** to a full run (logged loudly) —
  it can never silently disable a criterion or cause a false early stop.

Observability (every early-stopped run is flagged everywhere so analysis never
compares a truncated run against a full one):

| Surface | Field / marker |
|---------|----------------|
| `run.json` row | `stopped_early`, `early_stop_reason`, `turns_remaining_at_stop` |
| `run.md` | `> **NOTE:** […] stopped early (<reason>); <= N turn(s) avoided …` |
| `task.html` | header badge `stopped early (<reason>)` + `advisory — not gated` markers |
| Telemetry | `EarlyStopped` / `EarlyStopReason` dimensions on `CoderEval.Task.End` |

## Sandbox Configuration

The `sandbox` block is optional. When omitted, it defaults to `driver: "tempdir"` with standard Python environment.

```yaml
sandbox:
  driver: "tempdir"                   # Sandbox type ("tempdir" or "docker"); default: "tempdir"
  python:                              # Python env config (null to skip venv)
    env_packages:                      # Packages to install in sandbox venv
      - pytest
      - pylint>=3.0
  template_sources: [ ... ]           # Optional: preset files (see below)
  ignore_patterns:                    # Optional: overrides for template-copy filtering
    - "!dist"                         #   `!`-prefix un-ignores a default pattern
    - "!node_modules"
    - "*.bak"                         #   bare entry adds an extra pattern
  limits:                             # Optional: resource limits
    timeout: 300                       # Enforced via subprocess timeout (both drivers)
    max_memory_mb: 512                 # driver:docker -> `--memory`; ignored under tempdir
    max_cpus: 2                        # driver:docker -> `--cpus`; ignored under tempdir
    max_pids: 512                      # driver:docker -> `--pids-limit`; ignored under tempdir
    max_disk_mb: 1024                  # NOT enforced (reserved: no portable docker knob)
```

Under `driver: tempdir` only `timeout` is enforced — the agent can consume
arbitrary host memory, CPU, and PIDs. Use `driver: docker` when you need the
container limits above to actually bind.

## Template Sources

Tasks can start with preset files instead of an empty sandbox. Multiple sources are applied sequentially (last wins for conflicts).

### Git Repository

```yaml
template_sources:
  - type: "repo"
    url: "https://github.com/user/repo.git"
    commit: "abc123"                  # Optional: pin to specific commit
```

**Note:** If using `repo` source, it must be first in the list.

### Template Directory

Copy a local directory into the sandbox:

```yaml
template_sources:
  - type: "template_dir"
    path: "../templates/python-starter"  # Relative to task YAML file
    mount_point: "."                      # Optional: subdir inside sandbox to copy into (default ".")
```

The framework automatically ignores `.venv`, `.git`, `__pycache__`, `node_modules`, `dist`, `build`, and other common build/cache artifacts (full list: `coder_eval/resources/default_ignore_patterns.yaml`).

Override the defaults via `sandbox.ignore_patterns` (or `agent.ignore_patterns` for judge-style sub-agents). Each entry is either:

- A bare pattern (e.g. `*.bak`) — added on top of the defaults.
- A `!`-prefixed pattern (gitignore-style negation, e.g. `!dist`) — removes that pattern from the defaults so the directory survives the template copy. Useful for tasks that ship a vendored toolchain under `dist/` or `node_modules/`. Surrounding whitespace is stripped; bare `!` and empty entries raise `ValueError` at YAML load.

`mount_point` controls where inside the sandbox the template contents land. With `mount_point: "."` (default) files are copied to the sandbox root. With `mount_point: "c"` everything from the source directory ends up under `<sandbox>/c/`. The mount point must be a relative path that stays within the sandbox.

### Inline Starter Files

Define files directly in YAML (ideal for 1–3 files):

```yaml
template_sources:
  - type: "starter_files"
    files:
      - path: "README.md"
        content: |
          # My Project
          Instructions for the agent...
      - path: "src/main.py"
        content: |
          def main():
              pass  # TODO: Implement
```

### Combining Sources

```yaml
template_sources:
  - type: "template_dir"
    path: "../templates/python-base"
  - type: "starter_files"
    files:
      - path: "requirements.txt"
        content: "pytest>=8.0\npylint>=3.0"
```

### Template Sources in Experiment Variants

Experiment variants can add `template_sources` that are **appended after** the task's own template sources. This is useful for injecting variant-specific context (like a `CLAUDE.md` hint file) without duplicating the base task or creating separate template directories.

```yaml
# experiments/my-experiment.yaml
variants:
  - variant_id: baseline
    agent:
      model: "claude-sonnet-4-20250514"

  - variant_id: with-context-hint
    agent:
      model: "claude-sonnet-4-20250514"
    template_sources:
      - type: "starter_files"
        files:
          - path: "CLAUDE.md"
            content: |
              The UiPath flows are in folder ID abc-123-def.
              Use this folder when interacting with the Orchestrator API.
```

In this example, the `with-context-hint` variant gets the same sandbox as `baseline`, plus a `CLAUDE.md` file written into the sandbox root. Since variant template sources are appended last, they can also overwrite files from earlier sources (last-wins).

This pattern is especially useful for A/B testing whether additional context improves agent performance.

## Success Criteria

Every task needs at least one success criterion. The framework supports 14 criterion types.

### Continuous Scoring

All criteria share these fields:

| Field | Default | Description |
|-------|---------|-------------|
| `description` | — | Human-readable description (required) |
| `weight` | 1.0 | Relative importance for weighted score. `0` = **informational**: excluded from both the score and the pass/fail gate |
| `pass_threshold` | 0.9 | Minimum score (0.0–1.0) to pass |
| `stop_when` | `null` | Arms this criterion for early stop (`pass`/`fail`/`decided`/`auto`); requires `run_limits.stop_early: true` and an observable criterion type (`skill_triggered`, `command_executed`). `auto` arms whichever polarity this instance can decide (for dataset-fanned criteria whose positive/distractor role flips per row). See [`stop_early`](#stop_early-opt-in-early-stop). |

**Scoring types:**
- **Binary** (1.0 or 0.0): `file_exists`, `run_command`, `file_matches_regex`, `classification_match`, `skill_triggered`
- **Fractional** (0.0–1.0): `file_contains`, `file_check`, `json_check`, `command_executed`, `uipath_eval`
- **Continuous** (0.0–1.0): `reference_comparison`, `commands_efficiency`, `llm_judge`, `agent_judge`

**Task success:** all *gating* criteria must score >= their `pass_threshold`. A
criterion with `weight: 0` is informational — it is still checked, stored, and
rendered in reports, but it neither contributes to the score nor fails the task.
(A `weight: 0` criterion may not set `stop_when` or `suite_thresholds`: arming a
non-gating criterion for the early-stop or suite gate would let an
"informational" check flip a run to failure.)

Every surface labels it as such rather than as a failure: the terminal shows `○`
instead of `✓`/`✗`, the HTML report tags the row "informational — not gated" and
excludes it from the *n*/*m* passed header, the evalboard renders an `INFO` pill,
and a below-threshold informational criterion is never sampled as the reason a
suite row failed. The persisted `CriterionResult.gating` field carries this to
every consumer, so no reader needs the original criterion to know whether a low
score mattered.

**Weighted score:** `weighted_score = sum(score * weight) / sum(weight)` — calculated regardless for quality assessment.

### `file_exists`

Checks if a file exists. **Binary scoring.**

```yaml
- type: "file_exists"
  path: "app.py"
  description: "app.py must be created"
```

### `file_contains`

Checks if a file contains (or doesn't contain) specific strings. **Fractional scoring:** average of (includes matched / total) and (excludes absent / total).

```yaml
- type: "file_contains"
  path: "app.py"
  includes:                           # Strings that must be present
    - "Hello"
    - "import datetime"
  excludes:                           # Optional: strings that must NOT be present
    - "TODO"
    - "FIXME"
  description: "File must contain required strings"
  weight: 1.0
  pass_threshold: 0.9
```

### `file_check`

Unified file check that combines existence, string includes/excludes, and regex patterns into a single criterion. **Fractional scoring:** average of active sub-check scores. Replaces common `file_exists` + `file_contains` + `file_matches_regex` combinations.

File existence is implicit — if the file doesn't exist, score is 0.0. If no sub-checks are specified, it behaves as a pure existence check.

```yaml
# Full example with all features
- type: "file_check"
  path: "main.py"
  includes:                           # Strings that must be present
    - "from uipath import UiPath"
    - "def main"
  excludes:                           # Strings that must NOT be present
    - "import os"
  patterns:                           # Regex patterns to check
    - pattern: "def main\\(.*\\):"
      must_match: true                # true = must match (default), false = must NOT match
      flags: 0                        # Regex flags (default: 0)
  description: "main.py exists with correct imports and structure"
  weight: 1.0
  pass_threshold: 0.9

# Minimal: existence-only check (equivalent to file_exists)
- type: "file_check"
  path: "app.py"
  description: "app.py must be created"
```

| Field | Default | Description |
|-------|---------|-------------|
| `path` | *required* | Path to the file (relative to sandbox root) |
| `includes` | `[]` | Strings that must be present |
| `excludes` | `[]` | Strings that must NOT be present |
| `patterns` | `[]` | Regex pattern objects (`pattern`, `must_match`, `flags`) |

**Scoring:** Only active categories (non-empty lists) contribute to the average. For example, specifying only `includes` means the score equals the includes score alone — it is not inflated by absent categories.

### `json_check`

Validates a JSON file: existence, parse-ability, JSON Schema conformance, and JMESPath assertions. **Fractional scoring.**

File existence and valid JSON are implicit — if the file is missing or unparseable, score is 0.0. If no sub-checks are specified, it's a pure "is valid JSON" check.

```yaml
# Minimal: just validate JSON syntax
- type: "json_check"
  path: "data.json"
  description: "data.json is valid JSON"

# Schema validation only
- type: "json_check"
  path: "output.json"
  json_schema: "schemas/output_schema.json"
  description: "Output conforms to expected schema"

# JMESPath assertions only
- type: "json_check"
  path: "report.json"
  assertions:
    - expression: "status"
      expected: "success"
    - expression: "length(results)"
      operator: "gte"
      expected: 1
    - expression: "metadata.version"
      operator: "regex"
      expected: "^\\d+\\.\\d+\\.\\d+$"
  description: "Report has correct structure and values"

# Both schema + assertions
- type: "json_check"
  path: "result.json"
  json_schema: "schemas/result_schema.json"
  assertions:
    - expression: "status"
      expected: "completed"
    - expression: "items[?active].name"
      operator: "exists"
  description: "Result is valid and has expected values"
```

| Field | Default | Description |
|-------|---------|-------------|
| `path` | *required* | Path to the JSON file (relative to sandbox root) |
| `json_schema` | `null` | Path to a JSON Schema file (relative to sandbox root) |
| `assertions` | `[]` | List of JMESPath assertions (see below) |

**Assertion fields:**

| Field | Default | Description |
|-------|---------|-------------|
| `expression` | *required* | JMESPath expression to evaluate |
| `operator` | `"equals"` | One of: `equals`, `not_equals`, `contains`, `gt`, `gte`, `lt`, `lte`, `type`, `regex`, `exists` |
| `expected` | `null` | Expected value (required for all operators except `exists`) |

**Scoring:** Only active categories (schema, assertions) contribute. Schema scoring is binary (1.0/0.0). Assertions score = passed / total. When both are used, final score = average of the two category scores.

### `run_command`

Runs a command and checks the exit code, with optional stdout matching. **Binary scoring.**

```yaml
# Simple exit-code check
- type: "run_command"
  command: "python app.py"
  timeout: 30                         # Timeout in seconds (default: 30)
  expected_exit_code: 0               # Expected exit code (default: 0)
  description: "Script must run successfully"
  weight: 2.0

# With stdout matching (replaces former program_stdout_equals)
- type: "run_command"
  command: "python hello.py"
  expected_stdout: "Hello, World!"    # Optional: check stdout content
  stdout_match: "exact"               # "exact" (default), "contains", or "regex"
  description: "Script must output the correct text"
```

| Field | Default | Description |
|-------|---------|-------------|
| `command` | *required* | Command to execute |
| `timeout` | 30 | Timeout in seconds |
| `expected_exit_code` | 0 | Expected exit code |
| `expected_stdout` | `null` | When set, stdout is also checked |
| `stdout_match` | `"exact"` | Match mode: `exact` (stripped), `contains` (substring), `regex` (pattern) |

### `file_matches_regex`

Checks if file content matches a regular expression pattern. **Binary scoring.**

```yaml
- type: "file_matches_regex"
  path: "config.py"
  pattern: "^API_KEY = ['\"]\\w+['\"]$"
  must_match: true                    # true = must match; false = must NOT match
  flags: 0                            # Regex flags (re.IGNORECASE=2, re.MULTILINE=8)
  description: "Config must define API_KEY"
```

### `reference_comparison`

Compares agent's code with a reference solution using similarity scoring. **Continuous scoring.** Requires a `reference` block at the task level.

```yaml
- type: "reference_comparison"
  agent_file: "solution.py"           # Agent's output file (relative to sandbox)
  comparison_method: "ast"            # Method: "ast", "token", or "complexity"
  similarity_threshold: 0.8           # Minimum similarity (0.0-1.0)
  description: "Solution must match reference structure"
  weight: 2.0
```

**Comparison methods:**
- `ast` — Abstract Syntax Tree similarity (structure-based)
- `token` — Token-based similarity (implementation details)
- `complexity` — Cyclomatic complexity comparison

### `command_executed`

Checks whether the agent executed specific tools/commands during evaluation. Inspects `CommandTelemetry` records from agent turns. **Fractional scoring:** matched commands / `min_count`.

```yaml
- type: "command_executed"
  tool_name: "Bash"                   # Tool name filter (null = any tool)
  command_pattern: "curl.*wttr\\.in"  # Regex to match command parameters (null = any)
  min_count: 1                        # Minimum matching commands required (default: 1)
  require_success: true               # Only count successful commands (default: false)
  description: "Agent must use curl to fetch weather"
```

**Codex limitation.** Codex agents map `Read`, `Grep`, and `Glob` tools to `shell` commands (they execute via bash), so `tool_name: "Read"` on Codex returns no matches. Use `tool_name: "Bash"` or `tool_name: null` (any tool) for Codex-compatible checks. This criterion works correctly on Claude Code agents, which emit separate `Read`/`Grep`/`Glob` telemetry.

### `commands_efficiency`

Scores how economically the agent worked, relative to a budget of expected tool calls. **Continuous scoring:** `score = expected_commands / max(actual_commands, expected_commands)` — so a run at or under budget scores `1.0`, and the score decays as the agent takes more calls than expected (e.g. twice the budget → `0.5`).

```yaml
- type: "commands_efficiency"
  expected_commands: 8      # budget of tool calls to complete the task (>= 1)
  description: "Agent should solve this in ~8 tool calls"
```

| Field | Default | Description |
|-------|---------|-------------|
| `expected_commands` | *required* | Expected number of tool commands to complete the task (integer, `>= 1`). |

This criterion requires an agent run (it reads `CommandTelemetry`). Pair it with a low `weight` if you want efficiency to *inform* the score without gating pass/fail on its own.

### `uipath_eval`

Evaluates a UiPath agent against a named evaluation set. **Fractional scoring:** metrics passed / total metrics.

> The `uipath` CLI must be available **inside the sandbox** (typically declared in the task's own Python deps). This is independent of the host's optional `coder-eval[uipath]` extra — see the install matrix in [README.md](https://github.com/UiPath/coder_eval/blob/main/README.md#quick-start).

```yaml
- type: "uipath_eval"
  agent_name: "my-agent"
  eval_set: "regression-v1"
  thresholds:
    accuracy: 0.8
    f1: 0.75
  description: "Agent must meet accuracy and F1 thresholds"
```

| Field | Default | Description |
|-------|---------|-------------|
| `agent_name` | *required* | Name of the UiPath agent to evaluate |
| `eval_set` | *required* | Evaluation set identifier |
| `thresholds` | *required* | Minimum acceptable value per metric (metric passes if value >= threshold) |

### `llm_judge`

Have an LLM grade the task against a rubric written in the task YAML. **Continuous scoring** from a verdict the judge returns via a forced `submit_verdict` tool call (`{score: 0.0-1.0, rationale: "..."}`) — the model never returns free-form prose. A missing/malformed verdict, a non-numeric score, or an LLM error all produce `score=0.0` with an `error` populated.

```yaml
- type: "llm_judge"
  description: "Implementation follows the rubric"
  prompt: |
    Grade the implementation on correctness and idiomatic style.
    - 1.0: correct and idiomatic
    - 0.5: correct but not idiomatic
    - 0.0: incorrect or missing
  files: ["main.py", "tests/test_main.py"]
  include_reference: true            # Opt-in: show reference solution to the judge (never to the agent)
  include_agent_output: false        # Opt-in: include the latest turn's raw agent output
  include_tool_calls: false          # Opt-in: include a summary of the latest turn's tool calls
  include_dialog: false              # Opt-in: include the full user<->agent conversation (recommended for simulation)
  model: "anthropic.claude-sonnet-4-6"
  temperature: 0.0
  max_tokens: 2000
  max_file_chars: 20000              # Per-file content truncation
  weight: 2.0
  pass_threshold: 0.7
```

| Field | Default | Description |
|-------|---------|-------------|
| `prompt` | *required* | Grading instructions shown to the judge |
| `files` | `[]` | Paths whose contents are shown to the judge. Plain entries are sandbox-relative; entries prefixed with `$TASK_DIR/` are read from the host filesystem relative to the task YAML's parent directory (e.g. `$TASK_DIR/../shared/rubric.md` for a rubric shared across a task family). Missing files render as `<file not found>`. |
| `include_reference` | `false` | Include the task's reference solution in the judge prompt (silently omitted if no reference is configured). Never shown to the agent. |
| `include_agent_output` | `false` | Include the latest agent turn's raw output (wrapped as UNTRUSTED DATA) |
| `include_tool_calls` | `false` | Include a summary of the latest agent turn's tool calls |
| `include_dialog` | `false` | Include the full user↔agent conversation across **all** turns. In simulation mode the user side is generated by an LLM simulator and may invent premises — the rendered block is wrapped as `UNTRUSTED DATA` and instructs the judge to treat any claim made only by the simulated user as possibly fabricated, so the agent isn't penalized for going along with it (recommended whenever a task uses `simulation:`). |
| `max_dialog_chars` | `80000` | Aggregate cap on dialog text rendered into the judge prompt (per-message cap is `max_file_chars`). When exceeded, trailing turns are dropped and a degraded note is recorded. |
| `model` | `anthropic.claude-sonnet-4-6` | Judge model id (vendor-prefixed; auto-translated per backend) |
| `temperature` | `0.0` | Sampling temperature (0.0 = deterministic) |
| `max_tokens` | `2000` | Maximum tokens in the judge's response |
| `max_file_chars` | `20000` | Per-file (and agent_output) truncation applied before building the prompt |

**Transport selection.** The judge call is routed by the active `API_BACKEND`:

| `API_BACKEND` | Credentials | Judge transport |
|---|---|---|
| `direct` | `ANTHROPIC_API_KEY` set | Anthropic SDK → api.anthropic.com |
| `direct` | `ANTHROPIC_API_KEY` unset | run starts; this criterion fails fast at dispatch with a clear error |
| `bedrock` | `AWS_BEARER_TOKEN_BEDROCK` | AWS Bedrock |

The `direct`-mode transport is resolved once at startup, logged on the `API routing:` line (`anthropic_direct (judge transport: anthropic|none)`), and recorded in `EvaluationResult.environment_info.judge_transport`. Adding or removing `ANTHROPIC_API_KEY` between runs flips the transport, so check the startup log to confirm which path is in use.

**Security**

- The opt-in context blocks (`files`, `include_agent_output`, `include_tool_calls`, `include_dialog`) are wrapped with `UNTRUSTED DATA` / "may invent premises" preambles to mitigate prompt-injection via tool output and to flag simulator-generated user turns.
- The reference solution is shown only to the judge — never to the agent — and any occurrence of the reference is scrubbed from `CriterionResult.details` before persistence.

**Failure modes** — each sets `score=0.0` and populates `error`:

- The judge never emits the forced `submit_verdict` tool call (no verdict returned)
- `score` key missing from the verdict
- `score` is not coercible to float
- Judge backend unavailable / network error (handled by `@handle_criterion_errors`)

### `agent_judge`

Spawn a full Claude Code SDK agent as the judge. Unlike `llm_judge` (a single LLM call against a rubric), the judge agent has **tool access** — a read-only toolkit of `Bash`, `Read`, `Glob`, `Grep` by default (no `Write`/`Edit`) — and runs in an isolated copy of the task sandbox. Use it when functional validation requires executing something (`uip rpa get-errors`, `xmllint`, a test suite) rather than just inspecting file content.

```yaml
- type: "agent_judge"
  description: "Judge validates the generated XAML via CLI"
  prompt: |
    Inspect Main.xaml and grade how well it matches the task requirements.

    Do at least these checks using your tools:
    1. Valid XML? (`xmllint --noout Main.xaml` or Python's xml.etree)
    2. Contains the activities required by the task prompt?
    3. Uses VisualBasic expressions (no CSharpValue)?
    4. Variable declarations aligned with the reference?

    Scoring:
    - 1.0: all checks pass, structure aligned with reference
    - 0.7: functional but minor structural deviations
    - 0.4: partially correct — some critical checks fail
    - 0.0: invalid XML or fundamentally wrong structure
  files: ["Main.xaml"]
  include_reference: true
  include_agent_output: false
  include_tool_calls: false
  include_dialog: false              # Opt-in: include the full user<->agent conversation (recommended for simulation)
  max_turns: 5
  turn_timeout: 300
  agent:                              # Nested AgentConfig — same shape as task.agent
    model: "claude-sonnet-4-6"
    permission_mode: "bypassPermissions"
    allowed_tools: ["Bash", "Read", "Grep", "Glob"]
    sdk_options: {effort: low}        # Optional SDK pass-through (e.g. effort)
  weight: 5.0
  pass_threshold: 0.7
```

| Field | Default | Description |
|-------|---------|-------------|
| `prompt` | *required* | Evaluation instructions for the judge agent |
| `files` | `[]` | Paths pre-attached to the prompt. Plain entries are sandbox-relative (the judge also has live access via its working-directory copy); entries prefixed with `$TASK_DIR/` are read from the host filesystem relative to the task YAML's parent directory and are inlined into the prompt only. |
| `include_reference` | `false` | Include the task's reference solution in the judge prompt |
| `include_agent_output` | `false` | Include the latest agent turn's raw output (UNTRUSTED) |
| `include_tool_calls` | `false` | Include summarized tool-call telemetry from the latest agent turn |
| `include_dialog` | `false` | Include the full user↔agent conversation across **all** turns. The rendered block is wrapped as `UNTRUSTED DATA` and warns the judge that simulator-generated user messages may invent premises (recommended whenever a task uses `simulation:`). |
| `max_dialog_chars` | `80000` | Aggregate cap on dialog text (per-message cap is `max_file_chars`). Trailing turns are dropped past the cap. |
| `max_file_chars` | `20000` | Per-file truncation for pre-attached files |
| `max_turns` | `50` | Judge's inner-loop turn limit |
| `turn_timeout` | `300` | Wall-clock timeout (seconds) |
| `agent` | hardened judge defaults | Nested `AgentConfig` — `model`, `permission_mode`, `allowed_tools`, `disallowed_tools`, `ignore_patterns`, `sdk_options`. A partial block (e.g. only `model:`) still applies the judge security defaults for missing fields, and the security floor (`.claude` / `.mcp.json` / `_reference` ignore patterns, `setting_sources=[]`) is always enforced. |

**Security**

The judge runs with the evaluator's API credentials and can execute arbitrary Bash by default. Four attack surfaces:

1. **Malicious generation artifacts** — the coding agent could plant a file that exfiltrates credentials when the judge runs it (e.g. `python generated.py`). Use `llm_judge` for scenarios with adversarial generation.
2. **Prompt injection** — the UNTRUSTED markers on `include_agent_output` / `include_tool_calls` / `include_dialog` are hints, not guarantees. A strong injection could cause the judge to `curl` secrets out before returning a verdict.
3. **Credential exfiltration via Bash** — any network-capable tool can leak credentials. When the task doesn't need Bash (e.g. structural-only inspection), override `allowed_tools: [Read, Grep, Glob]`.
4. **Hooks / MCP servers planted by the main agent** — a compromised generation could drop `.claude/settings.json` or `.mcp.json` into the sandbox. The judge sets `setting_sources=[]` on its SDK config and excludes both paths from the sandbox copy, so neither gets loaded. This closes a pre-LLM-turn hook/MCP surface that the `allowed_tools` gate can't see.

**Reference handling**: The reference solution is shown to the judge verbatim (same as `llm_judge`) and is scrubbed from the persisted `CriterionResult.details` — a misbehaving judge that echoes the reference in its rationale won't leak it into run artifacts.

**Backend support**: Works on both backends (`direct`, `bedrock`) — the checker forwards the orchestrator's `ApiRoute` to the judge sub-agent.

**Operational notes**:

- Each invocation copies the sandbox into a `/tmp/sub_agent_*` directory and removes it when the check completes.
- The judge's token usage and wall-clock duration appear in `CriterionResult.details`.
- `agent_judge` is expensive relative to other criteria. Keep `max_turns` tight and consider running it alongside cheaper structural checks rather than as the sole gate.

**Failure modes** — each sets `score=0.0` and populates `error`:

- Non-JSON final message from the judge (parse failure)
- `score` missing / non-numeric / non-finite
- `TurnTimeoutError` (judge exceeded `turn_timeout`)
- SDK subprocess failure (e.g. `claude` CLI missing)

### `classification_match`

Matches a single label the agent wrote to a file against ground truth — the file-based classifier. Reads the file, normalizes the content (strip, and lowercase unless `case_sensitive`), and compares it to `expected_label`. **Binary scoring:** `1.0` on a match, else `0.0`.

The observed label is the canonical form from `allowed_labels` when the content matches; otherwise `(none)` when the file is missing/empty and `(other)` when the content isn't in the allowed set. Both sentinels are recorded so a suite rollup shows them as real failure classes in the confusion matrix.

```yaml
- type: "classification_match"
  path: "result.txt"                  # file (relative to sandbox) holding the agent's predicted label
  expected_label: "positive"
  allowed_labels: [positive, negative]
  case_sensitive: false               # default: case-insensitive + canonicalized
  description: "Sentiment label matches ground truth"
```

| Field | Default | Description |
|-------|---------|-------------|
| `path` | *required* | File (relative to sandbox) containing the agent's predicted label. |
| `expected_label` | *required* | Ground-truth label for this row (drive it from `${row.…}` on a dataset-backed task). |
| `allowed_labels` | *required* | Canonical label set (≥1). Content not in this set becomes `(other)`. |
| `case_sensitive` | `false` | When `false`, matching is case-insensitive and labels are canonicalized. |

Like `skill_triggered`, this criterion emits a `ClassificationCriterionResult`, so on a [dataset-backed task](#dataset) the suite aggregator computes accuracy / precision / recall / F1 and a confusion matrix — gate them with `suite_thresholds`. Use `classification_match` when the agent writes its answer to a file (labeling/extraction tasks); use `skill_triggered` when the signal is whether a skill fired.

### `skill_triggered`

Binary classifier: **did the agent engage the target skill during the run?** Agent-agnostic — scans the run's `turn_records` for either signal: Claude's explicit `Skill` tool call whose `skill` parameter matches `skill_name` (namespace prefixes like `plugin:skill` are stripped, so `skill_name: uipath-agents` matches `Skill(skill="uipath-coded-agents:uipath-agents")`), or — for an agent with no `Skill` tool, e.g. Codex — a command that reads the skill's files off disk (a parameter contains `skills/<skill_name>/`, matching both the repo path and the `.agents/skills/` symlink).

Observed label is `"yes"` when either signal is found, else `"no"`. Expected label is `"yes"` iff `expected_skill == skill_name`. **Binary scoring:** `1.0` when observed matches expected, else `0.0`.

```yaml
- type: "skill_triggered"
  description: "uipath-agents activation"
  skill_name: uipath-agents          # the skill to detect (Skill call or file read)
  expected_skill: "${row.expected_skill}"   # the row's true skill; "" for negatives
  suite_thresholds:
    recall.yes: 0.70
    precision.yes: 0.80
```

| Field | Default | Description |
|-------|---------|-------------|
| `skill_name` | *required* | The skill to detect — a `Skill` call whose `skill` parameter matches, or a file read under `skills/<skill_name>/` |
| `expected_skill` | *required* | The row's expected skill (after `${row.*}` substitution); empty string `""` for negative rows where the skill should **not** fire |

**Requires agent telemetry.** This criterion reads `turn_records`, so it only works against a real agent run (not a static check). With no turn records it reports `score=0.0` and an `error`.

**Classification metrics.** `skill_triggered` returns a `ClassificationCriterionResult`, so on a [dataset-backed task](#dataset) the suite aggregator computes accuracy / precision / recall / F1 / confusion matrix across all rows. Gate the suite with `suite_thresholds` using any of: `accuracy`, `macro_f1`, `weighted_f1`, `micro_f1`, or per-label `precision.<label>` / `recall.<label>` / `f1.<label>` (labels are `yes` / `no`). The run exits non-zero if any listed metric falls below its minimum.

**Typical pattern.** Label each dataset row with its true skill (`expected_skill`, `""` for negatives) and stack one `skill_triggered` criterion per skill against the same dataset — each gets its own confusion matrix from the same agent traces. This is the natural companion to a skill A/B experiment (skill plugin on vs. off); see the [A/B Experiment Guide](AB_EXPERIMENTS.md#recipe-ab-a-skill).

## Reference Solutions

Define a reference solution for `reference_comparison` criteria:

```yaml
# From a file (relative to task YAML)
reference:
  file: "reference_solution.py"

# Or inline
reference:
  code: |
    def fibonacci(n):
        if n <= 1:
            return n
        return fibonacci(n - 1) + fibonacci(n - 2)
```

## Pre-Run Commands

Run shell commands inside the sandbox **after setup completes but before the agent starts**.
Use them to seed databases, start background services, or prepare the environment.

By default (`fail_on_error: true`), a non-zero exit code, timeout, or exception **aborts
the evaluation** with `FinalStatus.ERROR` — the agent should not run against a broken
environment. Set `fail_on_error: false` per command for optional/informational steps.

```yaml
pre_run:
  - command: "python seed_db.py"
    timeout: 30
    # fail_on_error defaults to true — if seeding fails, abort

  - command: "start-mock-server.sh &"
    # shell exits 0 immediately; background daemon keeps running

  - command: "python check_connectivity.py"
    fail_on_error: false    # warn but don't abort if connectivity check fails
```

| Field | Default | Description |
|-------|---------|-------------|
| `command` | *required* | Shell command to execute (supports pipes, redirects, `&` for background) |
| `timeout` | 30 | Maximum seconds to wait (1–300) |
| `fail_on_error` | `true` | When true, failure aborts evaluation with `FinalStatus.ERROR` |

Commands run sequentially with `cwd` set to the sandbox directory. stdout and stderr are
captured in `pre_run_results` on the evaluation result (truncated to 100KB each). When a
command fails with `fail_on_error: true`, remaining commands are skipped.

**Execution order:**

1. Sandbox setup (template sources applied, venv/node packages installed)
2. **Pre-run commands** ← here
3. Agent evaluation loop
4. Success criteria checks
5. Post-run commands (if configured)

**Experiment-level defaults:**

Set `defaults.pre_run` in an experiment YAML to seed the environment before every task.
Experiment defaults are **prepended** before the task's own `pre_run` (baseline setup first):

```yaml
defaults:
  pre_run:
    - command: "docker-compose up -d"
      timeout: 60
```

## Post-Run Commands

Run shell commands inside the sandbox after evaluation completes. Post-run commands are **informational only** — they never affect pass/fail status. Use them for artifact generation, data extraction, validation reports, or cleanup.

```yaml
post_run:
  - command: "python3 validate_flow.py output.flow 12 4"
    timeout: 60

  - command: "cat results.json | jq '.summary'"

  - command: "tar czf /tmp/artifacts.tar.gz ."
    timeout: 120
```

| Field | Default | Description |
|-------|---------|-------------|
| `command` | *required* | Shell command to execute (supports pipes, redirects, etc.) |
| `timeout` | 30 | Maximum seconds to wait (1–300) |

Commands run sequentially with `cwd` set to the sandbox directory. stdout and stderr are captured on the `post_run_results` field of the evaluation result (truncated to 100KB each).

**Experiment-level defaults:**

Set `defaults.post_run` in an experiment YAML to run cleanup or extraction after every task.
Experiment defaults are **appended** after the task's own `post_run` (task-specific work first, then defaults):

```yaml
defaults:
  post_run:
    - command: "rm -rf node_modules .npm-prefix"
      timeout: 30
```

The shipped `experiments/*.yaml` use this to drop sandbox-scoped npm dirs (introduced by [PR #250](https://github.com/UiPath/coder_eval/pull/250) for MST-9674) so preserved-sandbox artifacts stay small.

## Simulation (Multi-Turn User Dialog)

Optional `simulation` block. When present and enabled, the orchestrator replaces the single-shot iteration loop with a multi-turn dialog between the coding agent and a simulated user (a second LLM with a persona and goal). Use this for tasks where the real usage pattern is conversational — clarifying questions, incremental requirements, mid-task corrections — rather than a single fire-and-forget prompt.

> This section is the field reference. For when to use dialog mode, how to design a persona and
> goal, trials and variance, grading, and cost, see **[Dialog Mode](DIALOG_MODE.md)**.

```yaml
simulation:
  enabled: true                        # Master switch; when false, simulation is skipped entirely.

  # Persona and goal (required).
  persona: |
    A non-technical business analyst who knows the outcome they want
    but not how automation works. Mildly impatient.
  goal: |
    Build a flow that reads invoice PDFs from an Outlook folder,
    extracts vendor/amount/date, and posts to Google Sheets.
    Do NOT volunteer the Google Sheets requirement unless asked.
  constraints:                         # Optional behavioral rules.
    - "Do not paste code — you cannot read code."
    - "If the agent goes silent for two turns, ask 'are you still there?'."

  # Termination.
  max_turns: 12                        # Hard cap on user↔agent exchanges.
  stop_token: "<<<END>>>"              # Simulator emits this when it judges the task complete.
  stop_on_criteria_pass: true          # End early when all success criteria pass.
  max_total_tokens: 150000             # Optional budget across the whole dialog.

  # Sampling (variance analysis).
  n_trials: 3                          # Run N independent dialogs per (task, variant).

  # Criteria timing.
  check_criteria: every_turn           # One of: end_of_dialog | every_turn | both.
                                       # Required to be 'every_turn' or 'both' when
                                       # stop_on_criteria_pass is True.
```

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `false` | When false, simulation is skipped; task runs in single-shot mode. |
| `persona` | *required* | Who the simulator is roleplaying. |
| `goal` | *required* | What the simulated user wants. |
| `constraints` | `[]` | Behavioral rules the simulator must follow. |
| `max_turns` | `8` | Hard cap on user↔agent exchanges (1–100). |
| `stop_token` | `"<<<END>>>"` | Sentinel the simulator emits to end the dialog. |
| `stop_on_criteria_pass` | `false` | End when all criteria pass (requires per-turn checking). |
| `max_total_tokens` | *unset* | Optional dialog-wide token budget (simulator **plus** agent). Distinct from [`run_limits.max_total_tokens`](#run-limits) — see below. |
| `n_trials` | `1` | Independent dialog trajectories per (task, variant). |
| `check_criteria` | `end_of_dialog` | `end_of_dialog`, `every_turn`, or `both`. |

The simulator runs as a tools-disabled Claude Code agent sharing the coding agent's `ApiRoute` — model/temperature/sampling are resolved at the route level (same `-b` flag as the coding agent), so they are not configured on this block.

**Semantics:**

- The task's `initial_prompt` is the user's *opening* message; the simulator picks up from turn 2.
- `max_turns` is the intra-dialog cap (the worst-case agent call budget per trial). Use `n_trials` for variance sampling.
- The `reference` solution, if present, is hidden from the simulator (same security posture as for the coding agent).
- When `n_trials > 1`, each trial becomes its own `ResolvedTask` with its own zero-padded replicate directory (`runs/<ts>/<variant_id>/<task_id>/<NN>/`) and its own `task.json` — the same fan-out mechanism as experiment `repeats`, which `n_trials` takes precedence over when simulation is enabled. Trial-level metadata appears under `simulation.replicate_index` / `simulation.n_trials` on the `EvaluationResult`.

**Termination precedence** (evaluated after each exchange, first match wins): `run_limits` breach →
`stop_on_criteria_pass` → `max_turns` → `max_total_tokens` → `stop_token`. The stop token is checked
**last**, on the simulator's next utterance, which is only solicited if nothing above fired — so a
turn that hits `max_turns` or the token budget ends the dialog before the simulator can emit it.
A raising simulator call ends the dialog with `stop_reason: error`.

> **`simulation.max_total_tokens` vs. `run_limits.max_total_tokens`.** This one covers the **whole
> dialog** (simulator + agent) and ends the conversation gracefully with `stop_reason='budget'`, so
> the task is still scored. [`run_limits.max_total_tokens`](#run-limits) covers the **subject agent
> only** and **aborts** the task with `FinalStatus.TOKEN_BUDGET_EXCEEDED`. They compose — set both
> for a hard ceiling on a simulated task.

**Grading simulated dialogs.** When a task uses `simulation:`, set `include_dialog: true` on any `llm_judge` / `agent_judge` criterion. Without it, the judge sees only the agent's outputs and may flag a fabricated-but-conceded premise as a hallucination by the agent. The dialog block is rendered with a rubric guard telling the judge to treat any claim made only by the simulated user as possibly invented, and not to penalize the agent for going along with it unless the grading prompt contradicts it.

**Experiment variants** can override any simulation field (persona, goal, constraints, n_trials, etc.) by setting a partial `simulation:` block on a variant — it is shallow-merged onto the task's simulation block. Useful experiment axes: simulator persona (terse vs. chatty), goal withholding, and n_trials for budget/quality tradeoffs.

## Command Telemetry

The framework automatically tracks all agent commands. No configuration needed.

**What's tracked:**
- Tool name and parameters
- Duration (millisecond precision)
- Status (success, error, unknown)
- Execution sequence within each turn

**Token usage** is also tracked (input/output tokens per turn) for cost analysis.

Results include aggregated statistics:

```json
{
  "command_stats": {
    "total_commands": 42,
    "commands_by_tool": { "Read": 15, "Write": 12, "Bash": 10 },
    "total_command_time_ms": 8543.2,
    "success_rate": 0.95,
    "slowest_commands": [...]
  }
}
```

## Complete Example

A full-featured task definition using most features:

```yaml
task_id: "calculator_agent"
description: "Create a calculator agent using LangGraph"

initial_prompt: |
  Create a calculator agent using StateGraph that performs
  basic arithmetic operations (+, -, *, /).

agent:
  type: "claude-code"
  permission_mode: "acceptEdits"
  allowed_tools: ["Read", "Write", "Bash"]

sandbox:
  driver: "tempdir"
  python:
    env_packages:
      - pytest
      - pylint>=3.0
  template_sources:
    - type: "template_dir"
      path: "../templates/python-starter"

success_criteria:
  - type: "file_exists"
    path: "main.py"
    description: "main.py must exist"
    weight: 0.5

  - type: "file_contains"
    path: "main.py"
    includes: ["StateGraph", "BaseModel"]
    description: "Must use required libraries"
    weight: 2.0

  - type: "run_command"
    command: "python -m py_compile main.py"
    timeout: 10
    description: "Valid Python syntax"

  - type: "reference_comparison"
    agent_file: "main.py"
    comparison_method: "ast"
    similarity_threshold: 0.7
    description: "Code structure matches reference"
    weight: 2.5

  - type: "command_executed"
    tool_name: "Bash"
    command_pattern: "python.*main\\.py"
    min_count: 1
    description: "Agent must run the script"

reference:
  code: |
    from pydantic import BaseModel
    from langgraph.graph import StateGraph, START, END

    class Input(BaseModel):
        a: float
        b: float
        operator: str

    class Output(BaseModel):
        result: float

    def calculate(state: Input) -> Output:
        ops = {"+": lambda: state.a + state.b,
               "-": lambda: state.a - state.b,
               "*": lambda: state.a * state.b,
               "/": lambda: state.a / state.b if state.b != 0 else 0}
        return Output(result=ops.get(state.operator, lambda: 0)())

    builder = StateGraph(state_schema=Input, input=Input, output=Output)
    builder.add_node("calculate", calculate)
    builder.add_edge(START, "calculate")
    builder.add_edge("calculate", END)
    graph = builder.compile()
```
