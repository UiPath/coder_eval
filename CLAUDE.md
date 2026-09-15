# CLAUDE.md - AI Assistant Guide

Working reference for AI assistants on the `coder_eval` codebase.

**Communication style:** use ASD-STE-100 when you speak to the user, and when you edit
this file.

Design *rationale* — why a subsystem is shaped the way it is, and which shipped defect
shaped it — lives under **`.claude/notes/`**, which is not auto-loaded; start at
[`.claude/notes/README.md`](.claude/notes/README.md). Read it before changing grading,
resume, early stop, timing, the reference anti-cheat, or an agent adapter.

User-facing documentation lives in [`docs/`](docs/index.md): start with the
[User Guide](docs/USER_GUIDE.md) for CLI behaviour and the
[Task Definition Guide](docs/TASK_DEFINITION_GUIDE.md) for task YAML. The full docs
index is generated from the `mkdocs.yml` nav (see **Docs index SSOT** below) and is
deliberately not copied here.

## Project Overview

**coder_eval** evaluates AI coding agents with sandboxing, reproducibility, and
data-driven analysis.

- **Python**: >=3.13 · **License**: Apache 2.0
- **Entry point**: `coder_eval.cli:app` (command: `coder-eval`)

## Directory Structure

`src/coder_eval/` — run `ls` for the current layout. What a filename does not tell you:

- **`models/`** is the pure-Pydantic layer: the dependency arrow runs `agents` →
  `models`, so it may reach `agents` / `plugins` only lazily (CE017). All core models
  import from `coder_eval.models`, never from its submodules.
- **`criteria/`** auto-discovers one checker per type via `pkgutil`.
- **`cli/`** holds Typer commands; each has a plain-Python twin (CE048).
- **`timing.py`** owns the single subtraction seam (CE063).
- **`argv_match.py`** is a STDLIB-ONLY sidecar copied beside the recorder (CE057).
- **`fs_permissions.py`** is `set_permissions`, the stacked chmod window.
- **`path_utils.py`** owns run ids, atomic writes and tree digests — and every run-record
  filename literal (CE053).
- **`models/container_paths.py`** owns `IN_CONTAINER_ENV` (CE056).
- **`pricing.py`** is the rate SSOT; `evalboard/lib/pricing.generated.ts` is generated
  from it by `make pricing-mirror`, and CE065 fails the build on drift.
- **`reports/`** is a LEAF rendering layer (markdown, html, experiment, junit +
  helpers): it may import anywhere, and core may import only its public writers
  (CE066). `reports/html.py` is the evalboard's static twin.
- **`result_metrics.py`** holds `EvaluationResult` metrics the ORCHESTRATOR reads
  mid-run; **`stats.py`** is distribution-free statistics, dependency-free by
  contract; **`run_record.py`** is the `run.json` task-row serializer, not a report.
- **`durations.py`** is `format_ms`, split from `formatting.py` so the reports layer
  does not reach through an SDK-shaped module for it.
- **`isolation/`** is `driver: docker`, one container per task.
- **`streaming/`** is the event protocol and `EventCollector`.

Outside the package: `tasks/`, `experiments/`, `templates/`, `tests/`, `docs/`,
`evalboard/`, `plugins/coder-eval/` (the published plugin), `action.yml` (the published
composite Action).

## Key Architectural Patterns

Each entry is a pointer. Full rationale: `.claude/notes/` (index: `.claude/notes/README.md`).

- **Discriminated unions** for criteria types and template sources.
- **Plugin registry**: `criteria/` auto-discovers via `pkgutil` + `@register_criterion`.
- **Strategy pattern**: `Agent` ABC, implementations in `agents/`.
- **Separation of concerns**: `models/` is pure Pydantic; logic lives in `criteria/`,
  `evaluation/`, `orchestration/`.
- **Callback streaming**: the agent is the sole emitter of the event protocol;
  `EventCollector` reduces the stream into a `TurnRecord`. Never hand-assemble one.
- **All core models import from `coder_eval.models`** — never from submodules.
- **Single declarative merge resolver**: all five config layers (default → experiment
  defaults → task → variant → CLI) merge through `orchestration/config_merge.py`.
  Fields declare their strategy once via `MergeField`. CE014 enforces it for lists.
- **Generic CLI overrides (`-D`/`--set`)**: layer 5, schema-validated against the
  resolved `TaskDefinition`. Only `--model`, `--driver`, `--type` survive as aliases.
- **Dataset fan-out**: `TaskDefinition.dataset` expands one task into N row-tasks
  before variant resolution. See [Bring Your Own Dataset](docs/DATASETS.md) and
  [`dataset`](docs/TASK_DEFINITION_GUIDE.md#dataset).
- **Per-criterion aggregation**: every criterion is suite-thresholdable via
  `aggregate()`; classification criteria layer accuracy / P/R/F1.
- **Reconciliation message**: summing token buckets across `TurnRecord.messages`
  equals `token_usage` exactly, on every backend. `EventCollector` is the single writer.
- **Timing has one subtraction seam** (`timing.py`); agents must not do their own
  (CE063). An unmeasured duration is `None`, never `0.0` (CE058).
- **Reference solutions are directory-only** and chmod-shielded during `communicate`.
  Defense-in-depth, not a boundary — the known gaps are documented in the notes.
  Authoring reference: [Reference Solutions](docs/TASK_DEFINITION_GUIDE.md#reference-solutions).
- **Harness run-limit parity**: a shared config field must mean the same thing on every
  backend, or the divergence is documented. Table:
  [Run-Limit Parity](docs/agents/HARNESS_PARITY.md). Caps are authored under
  [Run Limits](docs/TASK_DEFINITION_GUIDE.md#run-limits).
- **Execute vs. run**: `execute` is `run` with grading off — rows finalize as
  `NOT_GRADED` and leave both sides of every rate. Per-command behaviour:
  [CLI Commands](docs/USER_GUIDE.md#cli-commands).
- **Detached grading**: `evaluate <run_dir>` re-grades a finished run from its own
  recorded config, in place. The recorded config is untrusted input.
- **`--resume` is command-relative**: "finished" depends on what the resuming command
  still owes the task.
- **Early stop on criterion**: an opt-in `stop_early:` block arms a live criterion.
  Gating is fired-only. See the [Task Definition Guide](docs/TASK_DEFINITION_GUIDE.md)
  § `stop_early`.
- **Sandbox isolation**: tasks that don't need MCP servers should set
  `setting_sources: []` in their `agent:` block, to isolate the sandbox from the host
  project's CLAUDE.md and settings. Without it the host CLAUDE.md is injected into every
  API call, inflating cache-creation tokens and cost. See
  [Agent Configuration](docs/TASK_DEFINITION_GUIDE.md#agent-configuration) and
  [Sandbox Configuration](docs/TASK_DEFINITION_GUIDE.md#sandbox-configuration).
- **Container isolation**: `driver: docker` runs one container per task — see
  [Docker Isolation](docs/DOCKER_ISOLATION.md).
- **Dialog mode**: `simulation/` drives a multi-turn LLM user — see
  [Dialog Mode](docs/DIALOG_MODE.md).

## Success Criteria

Every criterion type registers in `criteria/` and is listed by
`CriterionRegistry.list_types()`. The authoritative per-field reference is
[Task Definition Guide § Success criteria](docs/TASK_DEFINITION_GUIDE.md#success-criteria);
path and env-var resolution inside a checker is
[Checker Context](docs/TASK_DEFINITION_GUIDE.md#checker-context). The plugin ships a
generated copy at `plugins/coder-eval/reference/criteria.md` — regenerate it with
`make plugin-reference`; never hand-edit it (CE033).

All criteria support `weight` (default 1.0) and `pass_threshold` (default 0.9). Live
criteria also accept `stop_early:`. Dataset-backed tasks may set `suite_thresholds:`
(see [Suite-level scoring](docs/DATASETS.md#suite-level-scoring)).

## Evaluation Flow

```
CLI → ExperimentRunner (task × variant, 5-layer merge) → run_batch → Orchestrator
      → Sandbox + Agent + SuccessChecker

Per-task (single iteration; simulation mode runs a multi-turn dialog):
  1. Orchestrator._communicate_with_retry(prompt, iteration) → TurnRecord
     (wraps agent.communicate with retry, per-attempt turn_timeout, and
      on_attempt_error → preserves crashed=True partial TurnRecords)
  2. SuccessChecker.check_all_async() → List[CriterionResult]

Cleanup: stop agent, save EvaluationResult, generate reports.
```

What the run writes on disk — `run.json`, `task.json`, `suite.json` and their fields —
is specified in [Report Schema](docs/REPORT_SCHEMA.md); the run directory layout itself
is `.claude/shared/run-layout.md`.

## Development Commands

If you run one command, run `make verify`. `make test` does not run the lint rules.

```bash
# MANDATORY: run after every implementation phase
make format      # ruff format
make check       # ruff check (lint)
make typecheck   # pyright
make test        # pytest (excludes live + lint markers)
make lint        # custom architectural lint rules (CE000+)
make verify      # all of the above + coverage check (CI equivalent)

make evalboard-verify   # the JS half: tsc --noEmit + vitest + next build

# Regenerate a generated surface — never hand-edit the output
make docs-indexes      # README/docs index tables from the mkdocs nav (CE028)
make plugin-reference  # the plugin's criteria reference from the models (CE033)
make pricing-mirror    # the evalboard's rate table from pricing.py (CE065)

make docs-budget       # per-file comment budget + docstring essay check (fails `make verify`)
```

`src/coder_eval/pricing.py` is the single source of truth for rates on both halves of
the repo. The evalboard's table (`evalboard/lib/pricing.generated.ts`) is generated from
it by `make pricing-mirror` — regenerate and commit after a reprice; **CE065** fails the
build on drift. Never hand-edit the generated file. A rate flagged
`per_request_billing` is deliberately omitted from the mirror: the provider bills per
request, so the board shows the captured actual per-call cost instead of a static estimate.

## Custom Lint Rules (CE000+)

Rules live in `tests/lint/rules/` and are wired in `tests/lint/runner.py`. Doc-surface
and whole-tree rules — those reasoning over Markdown/YAML or the entire `src/` tree
rather than one AST at a time — are instead `@pytest.mark.lint` classes in
`tests/test_custom_lint.py`.

**Every rule carries its own rationale in its module docstring**, including the defect
that motivated it and its known blind spots. That docstring is the authoritative
explanation; read it before editing, suppressing, or widening a rule. Run `make lint` —
`make test` deliberately excludes these.

When fixing a bug, ask: *could a custom lint rule have prevented this?* If the root
cause is a mechanically detectable pattern, add a rule following the CE000+ pattern and
wire it up. See `tests/test_custom_lint.py` for how rules are tested. Prefer removing
the sharp edge over guarding it: a rule is right when the pattern is genuinely
unavoidable, not when a shared helper would do. A cap rule sets its limit below the
current value, never at it. Candidates not yet promoted to rules
are collected in `.claude/harness-candidates.md`.

A few rules constrain routine edits, so they are worth knowing before you start:

- **CE036** requires a new live criterion to ship `ContractCase`s in the same change
  (`tests/lint/live_verdict_contract.py`).
- **CE030** — adding a user-facing field to one of the models CE030 tracks (`TaskDefinition`, `RunLimits`, `Dataset`, `SimulationConfig`, `RecordedCli`, `CliResponse`; `tests/lint/doc_schema_parity.py` is the SSOT) means documenting it in
  its guide, or adding an `EXEMPT` entry with a reason it is not user-authored. This
  list is a convenience copy kept honest by a test — it must name every tracked model.
- **CE026** keeps the GitHub Action's onboarding surfaces honest, including the
  `/coder-eval:ci` skill whose emitted workflow users copy into their own repos.
  Renaming an action input means updating that skill too — the user-facing contract is
  [CI Gate: GitHub Action & JUnit reports](docs/CI_GATE.md).
- **CE047** requires every onboarding surface to name every built-in `AgentKind`.
- **CE065** diffs `evalboard/lib/pricing.generated.ts` against `pricing.py`; the table was
  a hand-copy whose exemption set let four heavily-used models render `—` for cost.
  Regenerate with `make pricing-mirror`; never hand-edit the generated file.
- **CE066** lets the core layer import only the `reports/` package's public *writers*. A
  metric, statistic or serializer pulled out of `reports*` is what put `turn_time_buckets`
  and the run.json serializer in a rendering module; they now live in `result_metrics.py`,
  `stats.py` and `run_record.py`.

**Docs index SSOT.** `nav:` plus `extra.docs_index` in `mkdocs.yml` are the single
source of truth for `README.md`'s Documentation table, `docs/index.md`'s "Where to go
next", and the `## Docs` / `## Tutorials` sections of `docs/llms.txt`. Regenerate with
`make docs-indexes`; **CE028** fails the build on drift, on a nav page without a blurb,
or on a `docs/*.md` page missing from the nav. Never hand-edit between the
`<!-- docs-index:start -->` / `<!-- docs-index:end -->` markers.

**Anchor slugger convention.** Three sluggers render these docs and disagree on
headings containing `&` or punctuation. Prefer punctuation-free headings; if a heading
needs `&`, add a GitHub-form `<a id="…"></a>` shim above it and link that form. Verify a
new intra-doc anchor resolves in the built HTML (`mkdocs build`), not by eye.

**Plugin skills.** The plugin ships six skills (`/coder-eval:init`,
`/coder-eval:check-skill`, `/coder-eval:task`, `/coder-eval:lint-tasks`,
`/coder-eval:analyze`, `/coder-eval:ci`). Each must appear in all four surfaces in
`SKILL_DOC_SURFACES`, and their combined frontmatter `description` length is capped
(`SKILL_LISTING_BUDGET_CHARS`) because the listing budget is shared with every skill the
user has installed. Skill names are verb-first imperative (`init`, `analyze`,
`lint-tasks`, `check-skill`); `task` and `ci` predate the rule and stay. User-facing
documentation: [Claude Code plugin](docs/PLUGIN.md).

## Configuration

- **ruff**: line-length=120, target py313, select E/F/I/N/W/UP/B/SIM/RUF
- **pyright**: standard mode, includes `coder_eval/` only, excludes tests
- **pytest**: asyncio_mode=auto, strict markers, coverage source=coder_eval
- **Coverage threshold**: 80% (enforced in CI)

## Extension Points

**A new criterion**: model in `models/criteria.py` → the `SuccessCriterion` union →
checker in `criteria/` decorated `@register_criterion`, auto-discovered via `pkgutil`.
A live criterion also needs `ContractCase`s (CE036) and `make plugin-reference`.

**A new agent**: agents register through the plugin SPI (entry-point group
`coder_eval.plugins`) — there is no closed enum or dispatch to edit, and in-tree and
third-party agents take the same path. A new agent must be named on every onboarding
surface CE047 tracks, and its run-limit behaviour recorded in
[Run-Limit Parity](docs/agents/HARNESS_PARITY.md).

**Model pricing**: `register_pricing(YOUR_RATES)` from the same `register(registry)`
hook — no separate entry-point group.

Steps, checklists and worked examples: [Extending Coder Eval](docs/EXTENDING.md).
Document a new criterion type in the
[Task Definition Guide](docs/TASK_DEFINITION_GUIDE.md#success-criteria). Per-agent setup
and credentials: [Claude Code](docs/agents/CLAUDE_CODE.md) · [Codex](docs/agents/CODEX.md)
· [Antigravity](docs/agents/ANTIGRAVITY.md) · [OpenCode](docs/agents/OPENCODE.md) ·
[Pi](docs/agents/PI.md).

## Task Definition

Tasks are YAML. Full reference: [Task Definition Guide](docs/TASK_DEFINITION_GUIDE.md).

| Topic | Doc |
|-------|-----|
| Task YAML, criteria, run limits, sandbox | [Task Definition Guide](docs/TASK_DEFINITION_GUIDE.md) |
| Variants across the same tasks (A/B) | [A/B Experiments](docs/AB_EXPERIMENTS.md) |
| Dataset fan-out and suite thresholds | [Bring Your Own Dataset](docs/DATASETS.md) |
| Multi-turn user simulation | [Dialog Mode](docs/DIALOG_MODE.md) |
| `driver: docker`, custom images | [Docker Isolation](docs/DOCKER_ISOLATION.md) |
| Run/report JSON consumed downstream | [Report Schema](docs/REPORT_SCHEMA.md) |

## Dependencies

**Runtime**: pydantic, pydantic-settings, pyyaml, typer, rich, python-dotenv, anthropic,
claude-agent-sdk, anyio, radon, tqdm, jmespath, jsonschema

**Runtime (optional, `[uipath]` extra)**: uipath — for local sandbox parity with tasks
that invoke `uv run uipath eval ...`. Base installs run end-to-end without it;
UiPath-dependent paths fail at dispatch with a clear install hint.

**Dev**: pytest, pytest-asyncio, pytest-mock, pytest-cov, ruff, pyright, pip-audit,
bandit, pre-commit, mcp

## Design Principles

- **DRY** — field descriptions, validation, and docs defined once in Pydantic models
- **Single source of truth** — schema models are authoritative for parameter definitions
- **Type safety** — full checking with Pydantic and Pyright
- **YAGNI** — don't add complexity until actually needed
- **KISS** — keep it simple
- **Clean code** — no dead code, all imports used, all tests passing
- **Greenfield project** — no backward-compatibility burden
- **Delete before you guard** — before you add a lint rule, doc paragraph, criterion
  type or config field, try to delete the pattern that needs it. A new type that
  subsumes an old one removes the old one in the same change (no back-compat burden)
- **Comments are a last resort** — default to ZERO comments. Names, types and small
  functions carry the meaning. A comment is allowed ONLY when it records something the
  code cannot say
- **A docstring states the contract, not the history** — what a caller must know to call
  it correctly. Why the design is this shape belongs in `.claude/notes/`; what it used to
  be belongs in git. `make docs-budget` enforces two rules, both self-adjusting: a file's
  own-line comments may not exceed `MAX(20, 0.15 × its length)`, and no docstring may
  exceed 150 words of PROSE (an `Args:`/`Returns:`/`Raises:` block is structure, not
  prose; an `@abstractmethod` is exempt because its docstring IS the interface contract).

## Notes for AI Assistants

- Temporary files go in `tmp/`, not `/tmp`.
- Read `.claude/notes/` before changing grading, resume, early stop, timing, the
  reference anti-cheat, or any significant parts of this code's architecture. 
