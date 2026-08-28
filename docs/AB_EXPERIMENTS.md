---
description: >-
  A/B-test AI coding agents with Coder Eval's experiment layer — Claude Code vs.
  Codex vs. Gemini, model vs. model, skill on vs. off, prompt vs. prompt — on
  identical tasks.
---

# A/B Experiment Guide

How to run the same tasks across multiple configuration variants ("arms") and
compare them — model A vs. model B, skill on vs. off, prompt X vs. prompt Y.

The experiment layer sits **above** the single-task pipeline as a pre-processing
config resolver. It expands `tasks × variants` into fully-resolved tasks, runs
them through the normal batch path, and emits a cross-variant report. No changes
to the orchestrator are involved.

## Table of Contents

- [Quick Start](#quick-start)
- [Experiment YAML Structure](#experiment-yaml-structure)
- [The 5-Layer Config Merge](#the-5-layer-config-merge)
- [What a Variant Can Override](#what-a-variant-can-override)
- [Recipe: A/B a Skill](#recipe-ab-a-skill)
- [Recipe: A/B a Model](#recipe-ab-a-model)
- [Recipe: A/B a Prompt](#recipe-ab-a-prompt)
- [Recipe: Smoke vs. e2e Flavors (Early Stop)](#recipe-smoke-vs-e2e-flavors-early-stop)
- [Replicates (Statistical Power)](#replicates-statistical-power)
- [Measuring the Difference](#measuring-the-difference)
- [CLI Reference](#cli-reference)
- [Reading the Report](#reading-the-report)

## Quick Start

```bash
# Run every task under tasks/ against the variants in an experiment:
coder-eval run -e experiments/model-comparison.yaml

# Scope to specific tasks:
coder-eval run -e experiments/plugin-comparison.yaml tasks/agents/claude_*.yaml

# Bare experiment name resolves to experiments/<name>.yaml:
coder-eval run -e model-comparison tasks/hello_date.yaml
```

With no `-e`, `experiments/default.yaml` is applied as a single implicit variant
(`default`). Adding `-e` is what turns a run into an A/B comparison.

## Experiment YAML Structure

An experiment file lives in `experiments/` and has three top-level keys:

```yaml
experiment_id: my-comparison # kebab-case, required
description: "What this experiment measures"

defaults: # optional — applied to every variant
  agent:
    type: claude-code
    permission_mode: bypassPermissions
    allowed_tools: ["Skill", "Bash", "Read", "Write", "Edit", "Glob", "Grep"]
  repeats: 3 # default replicate count for all variants

variants: # required — at least 1, unique variant_id
  - variant_id: arm-a
    description: "Baseline"
    agent:
      plugins: []
  - variant_id: arm-b
    description: "Treatment"
    agent:
      plugins:
        - type: "local"
          path: "../skills"
```

`defaults` is an `ExperimentDefaults` block; each entry under `variants` is an
`ExperimentVariant`. Both carry **partial** `agent` dicts — you only specify the
keys that differ from the layers below.

## The 5-Layer Config Merge

Every resolved task is built by merging five layers, **later wins**:

| #   | Layer               | Source                                                    |
| --- | ------------------- | --------------------------------------------------------- |
| 1   | Baseline defaults   | `experiments/default.yaml`                                |
| 2   | Experiment defaults | `defaults:` block in your experiment YAML                 |
| 3   | Task config         | `tasks/<task>.yaml` (`agent:`, `run_limits:`, …)          |
| 4   | Variant overrides   | the per-variant block in your experiment YAML             |
| 5   | CLI flags           | `-D path=value` / `--set` and its two aliases (`--model`, `--driver`) — always wins |

Merge semantics: all five layers merge through **one** resolver
(`orchestration/config_merge.py`), where each field declares *how it merges* once
on the model. A given field merges identically regardless of which layer supplied
it (the unification invariant). The per-field strategy is:

- **scalars** (`agent.model`, `agent.permission_mode`, `run_limits.*`) and
  **most lists** (`allowed_tools`, `disallowed_tools`, `plugins`, …) — **replace**
  (last layer wins; a variant's `allowed_tools: ["Read"]` replaces the lower list
  entirely). `run_limits` is per-field replace, so a variant setting
  `run_limits.max_turns` leaves the task's `task_timeout` intact.
- **nested models** (`sandbox.docker`, `python`, `node`, `limits`) and **free-form
  dicts** (`agent.sdk_options`) — **deep**-merge: a higher layer touching one
  sub-key (e.g. `docker.network`) preserves siblings set below it (e.g.
  `docker.image`).
- **append lists** — overlays accumulate. `sandbox.docker.env_passthrough_extra`
  appends in layer order (`default → exp-defaults → task → variant`).
  `sandbox.template_sources` appends **task-first** (the task's base templates,
  then experiment-defaults and variant overlays after — "appended after task's
  base templates").

Variants set the sandbox driver via `driver:` and add templates via
`template_sources:` (top-level fields); they don't set a full `sandbox:` block.

## What a Variant Can Override

From `ExperimentVariant` (`coder_eval/models/experiment.py`):

| Field                 | Type               | Use                                                                                    |
| --------------------- | ------------------ | -------------------------------------------------------------------------------------- |
| `variant_id`          | str                | Unique arm identifier (required)                                                       |
| `description`         | str                | Human-readable label shown in reports                                                  |
| `agent`               | dict               | Partial `AgentConfig` overrides (model, plugins, tools, system_prompt, sdk_options, …) |
| `simulation`          | dict               | Partial `SimulationConfig` overrides (persona, goal, `n_trials`, … per arm) — see [Dialog Mode](DIALOG_MODE.md) |
| `repeats`             | int                | Replicate count for this arm (overrides experiment default)                            |
| `template_sources`    | list               | Extra templates appended after the task base (e.g. a docs overlay)                     |
| `prompt_mutations`    | list               | Ordered mutations applied to `initial_prompt`                                          |
| `initial_prompt`      | str                | Full prompt replacement (mutually exclusive with the two below)                        |
| `initial_prompt_file` | str                | Prompt replacement loaded from a file                                                  |
| `run_limits`          | block              | Per-key cap overrides (`max_turns`, `task_timeout`, token/USD budgets)                 |
| `driver`              | `tempdir`/`docker` | Sandbox driver — enables tempdir-vs-docker arms                                        |
| `checker_context`     | dict               | Backend/model override for the evaluation side (judge, simulator) — see [Checker Context](TASK_DEFINITION_GUIDE.md#checker-context); **not** currently `-D`-reachable |

The `agent` dict is the lever for most A/B tests. Anything on `AgentConfig` is
fair game: `model`, `permission_mode`, `allowed_tools`, `disallowed_tools`,
`plugins`, `system_prompt` / `system_prompt_file`, `setting_sources`,
`claude_settings`, `sdk_options`.

> **`system_prompt_mode` is a poor A/B lever.** A `replace` arm sends no default
> Claude Code prompt *at all*, so the delta measures the missing behavioral guidance
> (tool-call batching, conciseness), not your prompt text. To A/B prompt *content*,
> vary `system_prompt` and leave both arms on `append`.

> **Path-resolution gotcha.** Relative file paths in variant config resolve
> against _different_ base directories depending on the field:
>
> - `initial_prompt_file` (a top-level variant field) → relative to the
>   **experiment YAML**.
> - `agent.system_prompt_file` (inside the `agent` dict) → relative to the
>   **task YAML** (it's an `AgentConfig` field, resolved at task load).
>
> Bare experiment names passed to `-e` resolve under `experiments/<name>.yaml`
> at the repo root, not relative to your CWD.

## Recipe: A/B a Skill

Skills are loaded **only** via the `plugins` field — the SDK's internal `skills`
option is framework-owned and can't be set from YAML. So the canonical skill A/B
is "plugin absent vs. plugin present":

```yaml
experiment_id: my-skill-impact
description: "Does the uipath-agents skill improve task completion?"

defaults:
  agent:
    type: claude-code
    permission_mode: bypassPermissions
    model: claude-sonnet-5
    allowed_tools: ["Skill", "Bash", "Read", "Write", "Edit", "Glob", "Grep"]

variants:
  - variant_id: bare
    description: "No skill — baseline"
    agent:
      plugins: [] # skill unavailable

  - variant_id: with-skill
    description: "uipath-agents skill loaded"
    agent:
      plugins:
        - type: "local"
          path: ".." # PLUGIN ROOT holding skills/ — see note below
```

Notes:

- Keep `"Skill"` in `allowed_tools` for **both** arms — the baseline simply has no
  plugin to expose, so the tool is never invokable. This keeps the only variable
  the skill itself.
- Loading a plugin only **offers** the skill; the model decides whether to invoke
  it. Pair the experiment with a [`skill_triggered`](TASK_DEFINITION_GUIDE.md#skill_triggered)
  criterion to measure _whether it fired_ alongside your real success criteria
  that measure _whether outcomes improved_.
- **`path` must be a plugin ROOT — a directory holding `skills/`** — so the skill
  resolves at `<path>/skills/<name>/SKILL.md`. Point one level deeper, at the
  directory of skill directories, and claude-code loads **nothing**: the `with-skill`
  arm then silently matches the baseline and the A/B compares two identical arms.
  Codex and Antigravity accept either depth, so this fails on claude-code alone —
  see [Harness parity](agents/HARNESS_PARITY.md#agentpluginspath-accepts-different-depths-per-harness).
- Plugin paths are environment-dependent. The shipped example expects a
  `$PLUGIN_PATH` env var pointing at your plugin **root**. See
  `experiments/plugin-comparison.yaml`.

Run it:

```bash
coder-eval run -e experiments/my-skill-impact.yaml tasks/agents/create/*.yaml
```

## Recipe: A/B a Model

```yaml
experiment_id: model-comparison
description: "Sonnet vs. Opus on the same tasks"

variants:
  - variant_id: sonnet
    agent: { model: claude-sonnet-5 }
  - variant_id: opus
    agent: { model: claude-opus-5 }
```

## Recipe: A/B a Prompt

Use `prompt_mutations` (transform the task prompt) or `initial_prompt` /
`initial_prompt_file` (replace it wholesale). All three are mutually exclusive
per variant — setting two raises at load.

```yaml
experiment_id: prompt-phrasing
description: "Terse vs. detailed instructions"

variants:
  - variant_id: terse # uses the task's prompt unchanged
  - variant_id: detailed
    prompt_mutations:
      - type: suffix
        content: "Think step by step and validate your work before finishing."
```

### The mutation catalog

Four mutation types, applied to the base `initial_prompt` at **variant
resolution time** — before the task ever reaches the orchestrator:

| `type`     | Fields                                            | Defaults              |
| ---------- | ------------------------------------------------- | --------------------- |
| `prefix`   | `content` (required), `separator`                 | `separator: "\n\n"`   |
| `suffix`   | `content` (required), `separator`                 | `separator: "\n\n"`   |
| `replace`  | `pattern`, `replacement` (both required), `regex`  | `regex: false`        |
| `template` | `variables` (mapping, required)                   | —                     |

- **Ordered.** `prompt_mutations` is a list and each mutation operates on the
  result of the one before it. A `replace` listed after a `suffix` will rewrite
  the appended text too, which is occasionally what you want and more often a
  surprise — keep the list short and ordered deliberately.
- **`prefix` / `suffix`** join with `separator`, default `"\n\n"` (write it
  quoted in YAML so the escape is interpreted).
- **`replace`** is a literal `str.replace` by default. With `regex: true` it
  becomes `re.sub`, so `pattern` is a Python regular expression and
  `replacement` follows `re.sub` semantics — `\1` backreferences work, and an
  unanchored pattern can match far more than you intended.
- **`template`** substitutes literal `{name}` occurrences with the mapped value
  via plain string replacement — **not** `str.format`, and unrelated to the
  `${row.<field>}` syntax used by [datasets](DATASETS.md). A `{name}` with no
  matching entry in `variables` is left in the prompt verbatim rather than
  raising, so a typo in a variable name fails silently at the *prompt* level.
  Read the rendered prompt in the report to confirm.

Every mutation model sets `extra="forbid"`, so a misspelled field (`text:`
instead of `content:`) is a hard `ValidationError` at load — never a silently
ignored no-op.

When both `defaults.prompt_mutations` and a variant's `prompt_mutations` are set,
they **compose** — the experiment-defaults mutations apply first, then the
variant's, on the already-mutated prompt.

## Recipe: Smoke vs. e2e Flavors (Early Stop)

Run the **same** task file as both a fast `smoke` flavor and a full `e2e` flavor
with a one-line kill switch on the reference variant. Arm the criteria
that define "the interesting thing happened" with `stop_early:` blocks in the
task file;
the `smoke` variant cuts off as soon as they're decided, while `e2e` runs to
completion. Because the field merge is per-key, the variant sets only
`stop_early` (the run-level kill switch) without disturbing the task's `max_turns`.

```yaml
experiment_id: early-stop-ab
description: "Smoke vs. e2e from one file via opt-in early stop"

variants:
  - variant_id: e2e
    run_limits:
      stop_early: false # kill switch: force-disarm the blocks (the reference flavor)
  - variant_id: smoke
    # no override needed: the task's stop_early: blocks arm the watcher
```

The task file supplies the arming (`stop_early:` blocks on the criteria that gate the
flavor) and a `max_turns` generous enough for `e2e`; see
[`stop_early`](TASK_DEFINITION_GUIDE.md#stop_early-opt-in-early-stop). This recipe
ships as `experiments/early-stop-ab.yaml`.

Expect the `smoke` variant significantly lower on turns, duration, and tokens.
Verdict parity with `e2e` is **one-sided**: an early-stopped run is gated on the
armed subset only (non-armed criteria become advisory, clearly marked in the
report), a **fail-stop** is verdict-preserving (it is deferred until every
pass-armed criterion has resolved), but a **pass-stop** cuts the run once the
positives are decided — a distractor that would only misfire on a later tool
call is never observed, so the frozen row scores as a clean pass. Authoritative
precision/recall therefore belongs on the `e2e` (`stop_early: false`) variant.

## Replicates (Statistical Power)

Agents are stochastic — a single run per arm is noise, not signal. Set `repeats`
to run each `(task, variant)` pair N times; the report aggregates per-replicate
scores so you can see variance, not just a point estimate.

```yaml
defaults:
  repeats: 5 # every arm runs 5×
variants:
  - variant_id: bare
  - variant_id: with-skill
    repeats: 10 # this arm overrides to 10×
```

CLI `--repeats N` overrides both.

## Measuring the Difference

The experiment report ranks variants by **weighted score** per task plus
cross-task aggregates (win rate, average score, average duration, tokens). That
covers "which arm did better overall."

For skill experiments specifically, add a `skill_triggered` criterion to your
task. On a **dataset-backed** task it produces suite-level classification metrics
you can gate on with `suite_thresholds`:

```yaml
success_criteria:
  - type: skill_triggered
    description: "uipath-agents activation"
    skill_name: uipath-agents
    expected_skill: "${row.expected_skill}" # "" for rows where it shouldn't fire
    suite_thresholds:
      recall.yes: 0.70 # fired on ≥70% of rows that needed it
      precision.yes: 0.80 # ≤20% false activations
```

Available metric keys (from the classification overlay): `accuracy`, `macro_f1`,
`weighted_f1`, `micro_f1`, and per-label `precision.<label>` / `recall.<label>` /
`f1.<label>` (labels are `yes` / `no`). A suite gate fails the run (non-zero exit)
if any listed metric is below its minimum.

## CLI Reference

| Flag                                                                                                                                                              | Effect                                                                                                 |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `-e, --experiment <path\|name>`                                                                                                                                   | Experiment YAML. Bare name → `experiments/<name>.yaml`.                                                |
| `--sample N`                                                                                                                                                      | For dataset-backed tasks, use a fixed-seed random N-row sample (reproducible, unbiased across paths; cheap smoke test). |
| `--repeats N`                                                                                                                                                     | Run each `(task, variant)` N times; overrides YAML `repeats`.                                          |
| `--driver tempdir\|docker`                                                                                                                                        | Override sandbox driver for all tasks.                                                                 |
| `-j, --max-parallel N`                                                                                                                                            | Run up to N tasks concurrently.                                                                        |
| `-t, --tags` / `--exclude-tags`                                                                                                                                   | Filter which tasks run.                                                                                |
| `-D path=value` / `--set` | Generic layer-5 override of any resolved task-config field, applied to **every** variant — e.g. `-D agent.model=opus -D run_limits.max_turns=30`. Repeatable; schema-validated. |
| `--model`, `--driver` | Thin aliases for `-D` (`--model` ≡ `-D agent.model`, `--driver` ≡ `-D sandbox.driver`). All other task-config knobs (permission mode, turn/timeout limits, tools, plugins, SDK options) are set via `-D`. Layer-5 overrides apply to **every** variant (use sparingly — they erase the contrast between arms). |
| `--type` | Dedicated flag for agent type, applied to every variant (re-parses the agent discriminated union). |

Layer-5 flags win over variant config, so overriding the very thing you're
A/B-testing (e.g. `--model` on a model-comparison experiment) collapses all arms
to the same value. Set the variable in the variant block, not on the CLI.

## Reading the Report

Each run writes to `runs/<timestamp>/`. Per-task artifacts are nested
**variant-first**: `runs/<ts>/<variant_id>/<task_id>/<NN>/`, where `<NN>` is the
zero-padded replicate index (`00`, `01`, …). There is no `<experiment_id>`
segment — the experiment-level report lives at the run root.

The reports (generated by `reports_experiment.py`) are:

- **`experiment.md` / `experiment.json`** (run root) — the cross-variant summary:
  each task's per-variant score, the `best_variant`, the `score_spread`, and
  per-variant aggregates (tasks run/succeeded/failed, average score, average
  duration, total tokens, and — when `repeats > 1` — `per_replicate_scores` so you
  can see variance, not just a point estimate). `experiment.html` renders the same
  data for browsing.
- **`<variant_id>/variant.md` / `variant.json`** — per-variant rollup (plus
  `variant.html`).
- **`run.md` / `run.json`** (run root) — the flat batch log across all
  task × variant runs.

---

**See also:** [TASK_DEFINITION_GUIDE.md](TASK_DEFINITION_GUIDE.md) for task and
criterion reference, including [`skill_triggered`](TASK_DEFINITION_GUIDE.md#skill_triggered).
