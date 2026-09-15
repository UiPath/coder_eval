# CLAUDE.md - AI Assistant Guide

Working reference for AI assistants on the `coder_eval` codebase.

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

```
coder_eval/
├── agent.py                  # Agent ABC (start, communicate, stop, get_state)
├── config.py                 # Settings via pydantic-settings (.env loading)
├── sandbox.py                # Sandbox manager (tempdir, venv, templates, adopt)
├── orchestrator.py           # Main evaluation loop
├── reports.py                # Markdown/JSON run reports + per-suite rollups
├── reports_experiment.py     # Cross-variant experiment reports
├── reports_junit.py          # JUnit XML from a finalized run dir (CI ingestion)
├── reports_html.py           # Single-file HTML report (the evalboard's static twin)
├── reports_stats.py          # Shared report statistics + ungraded rendering helpers
├── formatting.py             # Number/duration formatting shared by the renderers
├── analysis.py               # Command statistics aggregation
├── logging_config.py         # Structured logging setup
├── path_utils.py             # Run IDs, path utilities, atomic writes, tree digests
├── fs_permissions.py         # set_permissions: stacked chmod window
├── pricing.py                # Model pricing (mirrored by evalboard/lib/pricing.ts)
├── litellm_cost.py           # Join proxy-captured actual per-call cost onto turns
├── timing.py                 # TurnClock + turn decomposition (single subtraction seam)
├── invocation_log.py         # record_cli recording shim + JSON Lines reader
├── argv_match.py             # Structured argv matcher (STDLIB-ONLY sidecar — CE057)
├── telemetry.py              # App Insights / OpenTelemetry emission
├── isolation/                # driver: docker — one container per task
├── harbor/                   # Harbor export + coder-eval as a Harbor agent
├── optimize/                 # Prompt/config optimization helpers
├── utils.py                  # Version info helpers
│
├── agents/                   # Agent implementations (claude_code, codex, antigravity,
│                             #   opencode, pi, noop) + registry, watchdog
│
├── models/                   # Pure Pydantic data models (see __init__ for exports)
│   ├── enums.py              # AgentKind, AgentState, FinalStatus, ApiBackend
│   ├── criteria.py           # 15 success criterion types + base + union
│   ├── experiment.py         # ExperimentDefinition, ExperimentVariant, ResolvedTask
│   ├── cli_match.py          # FlagMatch + CliMatch (cycle-free leaf)
│   ├── container_paths.py    # IN_CONTAINER_ENV + container path constants (CE056)
│   ├── mutations.py          # PromptMutation variants
│   ├── results.py            # CriterionResult, TurnRecord, EvaluationResult, rollups
│   ├── routing.py            # ApiRoute (DirectRoute/BedrockRoute)
│   ├── sandbox.py            # SandboxConfig, ResourceLimits, RecordedCli, CliResponse
│   ├── tasks.py              # TaskDefinition, AgentConfig, Dataset, RunLimits
│   ├── telemetry.py          # CommandTelemetry, TokenUsage, TranscriptMessage
│   └── templates.py          # RepoSource, TemplateDirSource, StarterFilesSource
│
├── criteria/                 # Criterion checker plugins (one file per type)
│   ├── __init__.py           # CriterionRegistry with auto-discovery
│   └── base.py               # BaseCriterion + @handle_criterion_errors
│
├── evaluation/               # checker.py (SuccessChecker), judge_context, judge_verdict,
│                             #   sub_agent, summaries
├── orchestration/            # batch, config, config_merge, early_stop, evaluation,
│                             #   regrade, experiment, overrides, task_loader
├── cli/                      # Typer commands; each has a plain-Python twin (CE048)
├── scoring/                  # AST / token / signature / complexity / quality similarity
├── streaming/                # Event protocol, EventCollector, renderers
├── simulation/               # Multi-turn user simulation (dialog mode)
└── resources/                # Package resources

experiments/   tasks/   tests/   docs/   templates/   evalboard/
plugins/coder-eval/            # Published Claude Code plugin (six skills)
action.yml                     # Published composite GitHub Action
.claude-plugin/marketplace.json
```

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

## Success Criteria (15 types)

| Type | Scoring | Description |
|------|---------|-------------|
| [`file_exists`](docs/TASK_DEFINITION_GUIDE.md#file_exists) | Binary | File must exist |
| [`file_contains`](docs/TASK_DEFINITION_GUIDE.md#file_contains) | Fractional | String presence/absence |
| [`file_check`](docs/TASK_DEFINITION_GUIDE.md#file_check) | Fractional | Unified file existence + content + regex check |
| [`json_check`](docs/TASK_DEFINITION_GUIDE.md#json_check) | Fractional | JSON validation + JSON Schema + JMESPath assertions |
| [`run_command`](docs/TASK_DEFINITION_GUIDE.md#run_command) | Binary / Continuous | Exit code + optional stdout matching or float scoring |
| [`file_matches_regex`](docs/TASK_DEFINITION_GUIDE.md#file_matches_regex) | Binary | Regex match on file |
| [`reference_comparison`](docs/TASK_DEFINITION_GUIDE.md#reference_comparison) | Continuous | AST/token/complexity similarity |
| [`command_executed`](docs/TASK_DEFINITION_GUIDE.md#command_executed) | Fractional | Agent tool usage verification |
| [`cli_called`](docs/TASK_DEFINITION_GUIDE.md#cli_called) | Binary | Structured match over the `record_cli` invocation log |
| [`commands_efficiency`](docs/TASK_DEFINITION_GUIDE.md#commands_efficiency) | Continuous | Tool-call efficiency against an expected budget |
| [`uipath_eval`](docs/TASK_DEFINITION_GUIDE.md#uipath_eval) | Fractional | UiPath agent evaluation results |
| [`classification_match`](docs/TASK_DEFINITION_GUIDE.md#classification_match) | Binary | File-based label match; emits suite-level P/R/F1 |
| [`skill_triggered`](docs/TASK_DEFINITION_GUIDE.md#skill_triggered) | Binary | Did the agent engage the target skill? Agent-agnostic |
| [`llm_judge`](docs/TASK_DEFINITION_GUIDE.md#llm_judge) | Continuous | LLM grades artifacts + optional trajectory/reference |
| [`agent_judge`](docs/TASK_DEFINITION_GUIDE.md#agent_judge) | Continuous | Sandboxed SDK agent investigates with tools. Expensive |

All criteria support `weight` (default 1.0) and `pass_threshold` (default 0.9). Live
criteria also accept `stop_early:`. Dataset-backed tasks may set `suite_thresholds:`
(see [Suite-level scoring](docs/DATASETS.md#suite-level-scoring)).

Each type above links to its own section in the
[Task Definition Guide](docs/TASK_DEFINITION_GUIDE.md#success-criteria), which is the
authoritative per-field reference. Path and env-var resolution inside a checker is
[Checker Context](docs/TASK_DEFINITION_GUIDE.md#checker-context). The plugin ships a
generated copy at `plugins/coder-eval/reference/criteria.md` — regenerate it with
`make plugin-reference`; never hand-edit it (CE033).

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

make docs-budget       # per-file comment budget + docstring essay check (fails `make verify`)
```

Editing `src/coder_eval/pricing.py` means editing `evalboard/lib/pricing.ts` too — it is
a hand-copied mirror, and `evalboard/lib/__tests__/pricing-parity.test.ts` fails the
build on drift in either direction.

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
unavoidable, not when a shared helper would do. Candidates not yet promoted to rules
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

### Adding a New Criterion

1. Define the model in `models/criteria.py` inheriting `BaseSuccessCriterion`.
2. Add it to the `SuccessCriterion` union.
3. Create the checker in `criteria/` inheriting `BaseCriterion`, decorated with
   `@register_criterion` — auto-discovered at runtime.
4. If it is a live criterion, add `ContractCase`s (CE036) and run `make plugin-reference`.

Worked example: [Custom success criteria](docs/EXTENDING.md). Document the new type in
the [Task Definition Guide](docs/TASK_DEFINITION_GUIDE.md#success-criteria).

### Adding a New Agent

Agents register through the plugin SPI (entry-point group `coder_eval.plugins`) — there
is no closed enum or dispatch to edit. In-tree and third-party agents use the same path.
Full walkthrough: [Extending Coder Eval](docs/EXTENDING.md). Per-agent setup and
credentials: [Claude Code](docs/agents/CLAUDE_CODE.md), [Codex](docs/agents/CODEX.md),
[Antigravity](docs/agents/ANTIGRAVITY.md), [OpenCode](docs/agents/OPENCODE.md),
[Pi](docs/agents/PI.md). A new agent must also be added to every onboarding surface
CE047 tracks, and its run-limit behaviour recorded in
[Run-Limit Parity](docs/agents/HARNESS_PARITY.md).

1. Define a `BaseAgentConfig` subclass (its own `type: Literal["your-kind"]`) and
   implement the `Agent` ABC.
2. Bind them with `registry.register("your-kind", YourConfig)(YourAgent)` inside a
   `register(registry)` hook exposed via a `coder_eval.plugins` entry point.
3. Use the shared turn lifecycle on the base class — `self._begin_turn()`,
   `self._end_turn_ok()`, `self._mark_stopped()`. Do not reimplement it.
4. Before raising on a mid-turn failure, set `self.pending_turn` to a `crashed=True`
   `TurnRecord`, then raise `AgentCrashError` or `TurnTimeoutError` bare.
5. Emit the standardized event protocol and fan it through an internal `EventCollector`
   plus the caller's `stream_callback`. One `AgentStartEvent` and one matching
   `AgentEndEvent` on *every* exit path, from `finally`.
6. If the agent shells out or holds OS resources, implement real `stop()` / `kill()` /
   `kill_sync()`. `kill_sync()` runs on a non-asyncio thread and must not await.

### Registering Model Pricing (plugins)

Call `register_pricing(YOUR_RATES)` from the same `register(registry)` hook — there is
no separate entry-point group. Keys are bare model ids; vendor/Bedrock prefixes are
normalized off at lookup. Registration is idempotent for identical rates and raises on a
conflicting rate for an existing key, so plugin load order can never silently reprice a
model. `coder_eval_uipath/pricing.py` is the worked example; see also
[Model pricing](docs/EXTENDING.md).

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
- **Comments are a last resort** — default to ZERO comments. Names, types and small
  functions carry the meaning. A comment is allowed ONLY when it records something the
  code cannot say
- **A docstring states the contract, not the history** — what a caller must know to call
  it correctly. Why the design is this shape belongs in `.claude/notes/`; what it used to
  be belongs in git. `make docs-budget` enforces two rules, both self-adjusting: a file's
  own-line comments may not exceed `MAX(20, 0.15 × its length)`, and no docstring may
  exceed 150 words of PROSE (an `Args:`/`Returns:`/`Raises:` block is structure, not
  prose; an `@abstractmethod` is exempt because its docstring IS the interface contract).
  There is no tree-wide total to hand-maintain — delete code and the budget shrinks with it

## Notes for AI Assistants

- Communication style: use ASD-STE-100 when you speak to the user.
- Temporary files go in `tmp/`, not `/tmp`.
- Read `.claude/notes/` before changing grading, resume, early stop, timing, the
  reference anti-cheat, or any significant parts of this code's architecture. 
