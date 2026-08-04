# CLAUDE.md - AI Assistant Guide

Project reference for AI assistants working on the `coder_eval` codebase.

## Project Overview

**coder_eval** is a framework for evaluating AI coding agents with sandboxing, reproducibility, and data-driven analysis.

- **Python**: >=3.13
- **License**: Apache 2.0
- **Entry point**: `coder_eval.cli:app` (command: `coder-eval`)

## Directory Structure

```
coder_eval/
├── agent.py                       # Agent ABC (start, communicate, stop, get_state)
├── config.py                      # Settings via pydantic-settings (.env loading)
├── sandbox.py                     # Sandbox manager (tempdir, venv, templates)
├── orchestrator.py                # Main evaluation loop
├── reports.py                     # Markdown/JSON report generation (run-level + per-suite rollup via write_suite_rollups)
├── reports_experiment.py          # Experiment/cross-variant report generation
├── reports_junit.py               # JUnit XML report from a finalized run dir (run.json spine; for CI test-report ingestion)
├── analysis.py                    # Command statistics aggregation
├── logging_config.py              # Structured logging setup
├── path_utils.py                  # Run ID generation, path utilities
├── pricing.py                     # Model pricing / cost calculation (ModelPricing, calculate_cost, register_pricing)
├── litellm_cost.py                # Join proxy-captured ACTUAL per-call cost/cache onto turns (LiteLLM backend; apply_actual_cost)
├── utils.py                       # Version info helpers
│
├── agents/
│   └── claude_code_agent.py       # Claude Code SDK agent implementation
│
├── models/                        # Pydantic data models (subpackage)
│   ├── __init__.py                # Unified exports for all models
│   ├── enums.py                   # AgentKind, AgentState, FinalStatus, ApiBackend
│   ├── criteria.py                # 14 success criterion types + base + union
│   ├── experiment.py              # ExperimentDefinition, ExperimentVariant, ResolvedTask, result models
│   ├── judge_defaults.py          # DEFAULT_JUDGE_MODEL constant (cycle-free leaf)
│   ├── mutations.py               # PromptMutation variants (prefix/suffix/replace/template)
│   ├── results.py                 # CriterionResult (+ ClassificationCriterionResult), TurnRecord, EvaluationResult, EarlyStopInfo/EarlyStopReason, CriterionAggregate, ThresholdCheck, SuiteRollup
│   ├── routing.py                 # ApiRoute (DirectRoute/BedrockRoute)
│   ├── sandbox.py                 # SandboxConfig, ResourceLimits
│   ├── tasks.py                   # TaskDefinition, AgentConfig, Dataset (dataset fan-out + sample)
│   ├── telemetry.py               # CommandTelemetry, CommandStatistics, TokenUsage, ProviderCallCost, ReconciliationMessage, TranscriptMessage
│   └── templates.py               # RepoSource, TemplateDirSource, StarterFilesSource
│
├── criteria/                      # Criterion checker plugins (one file per type)
│   ├── __init__.py                # CriterionRegistry with auto-discovery
│   ├── base.py                    # BaseCriterion (async _check_impl_async is primary; sync _check_impl derives from it, or vice versa) + @handle_criterion_errors(_async)
│   ├── _classification_aggregate.py  # Shared overlay: accuracy / P/R/F1 / confusion matrix
│   ├── classification_match.py    # File-based label matcher
│   ├── command_executed.py
│   ├── commands_efficiency.py
│   ├── file_check.py
│   ├── file_contains.py
│   ├── file_exists.py
│   ├── file_matches_regex.py
│   ├── json_check.py
│   ├── llm_judge.py
│   ├── reference_comparison.py
│   ├── run_command.py
│   ├── skill_triggered.py         # Binary: did the agent engage the target skill (Skill tool / file read)?
│   └── uipath_eval.py
│
├── evaluation/                    # Evaluation orchestration
│   ├── checker.py                 # SuccessChecker (dispatches to criteria/)
│   ├── judge_context.py           # JudgeContextBuilder + shared scrub/truncate/format_details for both judges
│   ├── judge_verdict.py           # parse_judge_verdict + span walker (shared verdict parser)
│   ├── sub_agent.py               # SubAgentRunner: sandbox-copy + ClaudeCodeAgent lifecycle for judge-style sub-agents
│   └── summaries.py               # summarize_commands (shared by orchestrator + llm_judge)
│
├── errors/                        # Error handling system
│   ├── agent.py                   # AgentCrashError + format_timeout_reason / truncate_crash_message helpers
│   ├── categories.py              # Error categorization
│   ├── categorization.py          # Error classification logic
│   ├── executor.py                # Execution with error context (+ on_attempt_error hook)
│   ├── retry.py                   # Retry logic with exponential backoff
│   └── timeout.py                 # Timeout handling (TurnTimeoutError carries optional partial TurnRecord)
│
├── orchestration/                 # Batch execution utilities
│   ├── batch.py                   # Parallel task execution (run_batch + run_batch_resolved)
│   ├── config.py                  # Batch run configuration
│   ├── early_stop.py              # validate_early_stop guardrails + EarlyStopWatcher (armed live-verdict observer)
│   ├── evaluation.py              # Evaluation helpers
│   ├── experiment.py              # ExperimentRunner, resolve_task_for_variant, load_experiment
│   └── task_loader.py             # YAML task loading
│
├── cli/                           # CLI commands (Typer + Rich)
│   ├── __init__.py                # Typer app setup (core commands)
│   ├── run_command.py             # `coder-eval run`
│   ├── plan_command.py            # `coder-eval plan`
│   ├── report_command.py          # `coder-eval report`
│   ├── run_helpers.py             # CLI helper functions
│   ├── console.py                 # Rich console instance
│   └── utils.py                   # CLI utilities
│
├── scoring/                       # Code similarity scoring
│   ├── ast_similarity.py          # AST-based comparison
│   ├── token_similarity.py        # Token-based comparison
│   ├── signature_similarity.py    # Function signature comparison
│   ├── complexity.py              # Cyclomatic complexity comparison
│   ├── quality.py                 # Quality metrics (annotations, docstrings)
│   └── similarity.py              # Unified similarity interface
│
├── streaming/                     # Real-time agent event streaming (agent is sole emitter)
│   ├── __init__.py                # Unified exports
│   ├── callbacks.py               # StreamCallback protocol, TaskScopedCallback, CompositeStreamCallback, safe_emit
│   ├── events.py                  # Event protocol: Agent/Turn/Tool Start+End + status enums (Pydantic)
│   ├── collector.py               # EventCollector: reduces the event stream into a TurnRecord (task.json capture)
│   └── renderers.py               # RichStreamRenderer + LoggingStreamRenderer (task.log; both event-driven)
│
├── simulation/                    # Multi-turn user simulation (dialog-mode evaluation)
│   ├── __init__.py                # Unified exports (UserSimulator, DialogStopReason, evaluate_stop)
│   ├── user_simulator.py          # LLM-driven user simulator (Anthropic + Bedrock backends)
│   └── termination.py             # Dialog-termination predicate + stop-token handling
│
└── resources/                     # Package resources

experiments/                        # Experiment definition YAML files
tasks/                             # Task definition YAML files
tests/                             # Test suite
docs/                              # Documentation
templates/                         # Sandbox template directories
action.yml                         # Published composite GitHub Action (coder-eval as a CI gate). release.yml maintains its `version:` default + the moving `v<major>` tag.
```

## Key Architectural Patterns

- **Discriminated Unions**: Criteria types and template sources use Pydantic discriminated unions
- **Plugin Registry**: `criteria/` uses auto-discovery via `pkgutil` + `@register_criterion` decorator
- **Strategy Pattern**: `Agent` ABC with implementations in `agents/`
- **Separation of Concerns**: Data models (`models/`) are pure Pydantic; logic lives in `criteria/`, `evaluation/`, etc.
- **Callback Streaming**: `StreamCallback` protocol with `TaskScopedCallback` wrapper for real-time LLM event output
- **Experiment Layer**: Pre-processing config resolver (`ExperimentRunner`) that resolves task × variant combinations via 5-layer merge (default → experiment defaults → task → variant → CLI) before passing to `run_batch`. For running A/B comparisons (model vs. model, skill on vs. off, prompt vs. prompt), see [docs/AB_EXPERIMENTS.md](docs/AB_EXPERIMENTS.md).
- **Single declarative merge resolver**: All five config layers merge through ONE engine (`orchestration/config_merge.py::resolve_root`) for the three `-D`-reachable roots (`agent`/`run_limits`/`sandbox`). Each field declares *how it merges* once, on the model, via `MergeField(strategy="deep"|"append"|"replace")` (or a type-aware default: nested `BaseModel`/free-form `dict` → `deep`; `list`/scalar → `replace`). `resolve_task_for_variant` (layers 1–4) and `apply_overrides` (layer 5) build `Layer` lists and call the same `resolve_root`, so a field merges identically regardless of which layer supplied it (the unification invariant, enforced by `tests/test_merge_unification.py`). Lint rule CE014 forces every list field to declare its strategy explicitly.
- **Generic CLI overrides (`-D`/`--set`)**: Layer 5 is a thin wrapper (`orchestration/overrides.py`) over the resolver above. `coder-eval run -D agent.model=opus -D run_limits.max_turns=30` overrides any field on the resolved `TaskDefinition` (`agent`/`run_limits`/`sandbox` roots), schema-validated with did-you-mean. Only `--model` (→ `agent.model`) and `--driver` (→ `sandbox.driver`) survive as active thin aliases that emit the equivalent `-D` entry; an alias and `-D` targeting the same path is a hard error. `--type` (→ `agent.type`) is a separate, lighter alias that does NOT route through that collision check — `--type` and `-D agent.type=…` last-win rather than hard-error (the `-D` value wins). Tools, plugins, and SDK options are `-D`-only.
- **All core models importable from `coder_eval.models`** regardless of submodule
- **Dataset fan-out**: `TaskDefinition.dataset` (inline rows or JSONL path) expands a single task into N row-tasks with `${row.<field>}` substitution in `initial_prompt` and `success_criteria` string fields. Expansion runs in `task_loader.expand_dataset` **before** variant resolution, so variants cannot override the dataset. Row sampling: CLI `--sample N` (fixed-seed uniform-random N over the whole dataset) overrides `--sample-per-stratum N` / `dataset.sample_per_stratum` (stratified random N-per-stratum, keyed on `stratify_field`, default `expected_skill` — for classification suites like activation). Stratified sampling (whether the N-per-stratum count comes from the **CLI** `--sample-per-stratum` flag or **YAML** `dataset.sample_per_stratum`) is **nondeterministic** by default — it re-draws each run (so the nightly activation suite broadens coverage over time). Set `dataset.sample_seed` to pin a reproducible sample; an explicit seed always wins. (Only `--sample N` uses a fixed seed, since a smoke test wants the same N rows each run.)
- **Per-criterion aggregation**: Each `BaseCriterion` subclass exposes `aggregate(criterion, per_row_results) -> CriterionAggregate | None`. Default emits `count / mean / median / std / min / max` so every criterion is suite-thresholdable for free. Classification-style criteria return `ClassificationCriterionResult` (subclass of `CriterionResult`) and layer accuracy / P/R/F1 / confusion via the shared `overlay_classification_metrics` utility. `BaseSuccessCriterion.suite_thresholds` gates the suite on those metrics; CLI exits non-zero on any gate failure.
- **Sub-agent token accounting**: There is NO separate per-sub-agent field. Every sub-agent generation is captured as a `parent_tool_use_id`-tagged `AssistantMessage` in the turn transcript, so per-sub-agent usage is derived by grouping those messages on that id (the evalboard's `aggregateSubAgentUsage` does exactly this). Claude bubbles its sub-agent's intermediate generations into the parent stream natively, and the **terminal** generation (delivered as the Agent tool result, never streamed) is synthesized into one via `_synthesize_subagent_terminal_message` from `tool_use_result.usage`. Codex reconstructs all child generations from the child rollout (`_recover_subagent_tool_calls`). The turn total already includes sub-agent cost — Claude via the SDK's cumulative `model_usage`; Codex via `_fold_subagent_tokens`, which folds the child messages (their real per-generation tokens) into the parent total. `CommandTelemetry.result_summary` is stored **untruncated** (no 200-char cap) so sub-agent returns are preserved whole. Set `CODER_EVAL_RAW_SDK_LOG=1` to dump every raw SDK event to the task log for inspection.
- **Reconciliation message (stream self-reconciles to the turn total)**: The per-message stream consistently under-reports the authoritative turn total — a fixed prompt slice (~512 input tokens on Claude) is billed on no SDK-emitted message, and sub-agent input/cache only partially bubbles up. So `EventCollector.build_turn_record` appends one synthetic `ReconciliationMessage` (`role="reconciliation"`, in the `TranscriptMessage` union) per turn, carrying the per-bucket residual = `token_usage` − Σ(assistant message buckets). The invariant: **summing the four token buckets across `TurnRecord.messages` (assistant + reconciliation) equals `token_usage` exactly**, for both Claude and Codex (Codex's stream is already complete after `_recover_subagent_tool_calls`, so its residual is usually 0 and no entry is emitted). This is what lets the evalboard SUM the message stream as the source of truth instead of reading a separate aggregate ("agent tokens"): `selectTokenTotals` returns the stream sum whenever a reconciliation entry is present, and the timeline renders it as its own row. It is agent-agnostic (booked at the single `EventCollector` seam), carries no cost (cost stays on `token_usage`), and is excluded from generation/turn counts and the cost simulator. The LiteLLM open-weight actual-cost join (`litellm_cost.apply_actual_cost`) deliberately writes cost at the TURN level only (`token_usage.total_cost_usd` = the real OpenRouter bill) plus the per-call `TurnRecord.provider_call_costs` audit record; it does NOT touch the message token buckets, so `EventCollector` stays the single writer and this invariant holds on every backend. The Python `token_usage`/`total_token_usage` aggregate is unchanged and still authoritative for budget/judges/reports.
- **sandbox isolation**: Tasks that don't need MCP servers should set `setting_sources: []` in their `agent:` block to isolate the sandbox from the host project's CLAUDE.md and settings. Without this, the host project's CLAUDE.md (often 20 KB+) is injected into every API call, inflating cache-creation tokens and cost significantly.
- **Run-time caps (non-criterion enforcement)**: `TaskDefinition.run_limits` (`RunLimits` model) is the single namespace for all *task-level* run-time caps — `max_turns` / `task_timeout` / `turn_timeout` (structural) and `max_input_tokens` / `max_output_tokens` / `max_total_tokens` / `max_usd` (cumulative budget). Token/USD breaches abort with `FinalStatus.TOKEN_BUDGET_EXCEEDED` or `COST_BUDGET_EXCEEDED` (both `category == "failed"`). Structural caps are set from the CLI via `-D run_limits.max_turns=…` / `-D run_limits.task_timeout=…` / `-D run_limits.turn_timeout=…` (field-merged into `run_limits`); budget caps via `-D run_limits.max_usd=…` etc. or YAML. Layered config uses field-merge — a variant block overrides individual keys without replacing the task's block. The one *per-criterion* cap, `max_steps_to_decide`, deliberately lives on `LiveSuccessCriterion` instead (see below) — the watcher must attribute a decision-step-budget breach to a specific criterion, which `RunLimits` (task-scoped, criterion-agnostic) cannot express.
- **Early stop on criterion (opt-in)**: `run_limits.stop_early` (default off) ends a single-shot Claude run early once the run's **armed** criteria are decided, so a raised `max_turns` isn't wasted on the smoke flavor. A criterion is armed by `stop_when: pass|fail|decided|auto`; only criteria that can decide from a partial trajectory may arm — "is this criterion type live-observable" is `models.LiveSuccessCriterion` subclassing (currently `skill_triggered`, `command_executed`), the single source of truth `validate_early_stop`/`EarlyStopWatcher` check directly via `isinstance`; each subclass implements the abstract, checker-independent `live_decidable_polarities()` (a pure function of its own fields) alongside the checker's `live_verdict` override, and lint rule CE025 (`tests/test_custom_lint.py::TestCE025LiveVerdictConsistency`, a registry-based whole-tree check, not a per-file AST rule) keeps the two paired. `decided` arms **both** polarities; `auto` arms whichever polarities **this instance** can decide — the value for dataset-fanned criteria whose positive/distractor role flips per row. Stop rule is weighted, not strict-boolean: `run_limits.stop_early_gate_threshold` (default `1.0`, reproducing pre-weighting behavior byte-for-byte) is the minimum weighted score (`Σ weight·score / Σ weight` over the armed subset) required to pass; a fail-stop fires once the armed subset's **ceiling** (best case for everything still undecided) can no longer reach the threshold, a pass-stop once the pass-armed subset's **floor** (worst case) already meets it — both **deferred while any pass-armed criterion is undecided**, so a distractor misfire never truncates a positive row's recall signal before the positives resolve (or the run continues to the cap). A fail-stop is therefore verdict-preserving; a pass-stop can miss a *later* distractor misfire, so authoritative P/R/F1 comes from a `stop_early: false` run. A per-criterion `max_steps_to_decide` (on `LiveSuccessCriterion` only, requires `stop_when`) caps tool-call steps spent still undecided — cumulative across retry attempts of the same turn — before `EarlyStopReason.DECISION_BUDGET_EXCEEDED` force-fails the run outright, bypassing the weighted gate (nothing to weigh a criterion that never decided against). Driven by `orchestration/early_stop.py::EarlyStopWatcher` through the Claude agent's cooperative `should_stop` seam (tool-call granularity, no SIGKILL); live verdicts only *trigger* the stop — the standard `check_all_async` on the frozen trajectory is authoritative. An early-stopped run gates on the **armed subset** via the weighted `EvaluationResult.armed_criteria_passed`; a run that completes naturally (even with `stop_early: true`) gates on the full set via the strict-AND `all_criteria_passed` — weight magnitude only forgives under the former, so the weighted gate is contingent on the watcher itself firing, not solely on the configured threshold. Every unsupported use is a hard error at resolution (plan *and* run), and a runtime verdict bug **fails open** to a full run. Surfaces: `EarlyStopInfo` (incl. `gate_threshold` at stop time), report notes/badges, `stopped_early` run.json rows, `EarlyStopped`/`EarlyStopReason` telemetry dims. Worked rationale: docs/TASK_DEFINITION_GUIDE.md § `stop_early`. Defaults off ⇒ behavior byte-for-behavior unchanged.

## Success Criteria (14 types)

| Type | Scoring | Description |
|------|---------|-------------|
| `file_exists` | Binary | File must exist |
| `file_contains` | Fractional | String presence/absence |
| `file_check` | Fractional | Unified file existence + content + regex check |
| `json_check` | Fractional | JSON validation + JSON Schema + JMESPath assertions |
| `run_command` | Binary / Continuous | Command exit code + optional stdout matching or float scoring |
| `file_matches_regex` | Binary | Regex match on file |
| `reference_comparison` | Continuous | AST/token/complexity similarity |
| `command_executed` | Fractional | Agent tool usage verification |
| `commands_efficiency` | Continuous | Agent tool-call efficiency relative to expected budget |
| `uipath_eval` | Fractional | UiPath agent evaluation results |
| `classification_match` | Binary | File-based label match (observed vs expected) with `(none)`/`(other)` sentinels; emits `ClassificationCriterionResult` for suite-level P/R/F1 |
| `skill_triggered` | Binary | Did the agent engage the target skill? Agent-agnostic — Claude's `Skill` tool call, or (Codex) reading the skill's files off disk. Emits `ClassificationCriterionResult` for suite-level P/R/F1 |
| `llm_judge` | Continuous | LLM grades artifacts + optional trajectory + optional reference; routes through the run's backend (Bedrock / Anthropic) |
| `agent_judge` | Continuous | Spawns a Claude Code SDK agent in an isolated sandbox copy; judge uses tools (Bash/Read/Grep/…) to investigate and returns a JSON verdict. Expensive; runs with evaluator credentials — see SECURITY note in the criterion docstring. |

All criteria support `weight` (default 1.0) and `pass_threshold` (default 0.9), plus `stop_when` (`pass`/`fail`/`decided`/`auto`, default `null`) which arms the criterion for early stop when `run_limits.stop_early` is set (observable criteria only; `auto` arms the instance's own decidable polarities). On dataset-backed tasks, criteria may also set `suite_thresholds: {metric: min_value}` — the suite gate passes iff every listed metric (from the criterion's `aggregate()` output) meets its minimum.

## Evaluation Flow

```
CLI → ExperimentRunner (resolve task × variant) → run_batch → Orchestrator → Sandbox + Agent + SuccessChecker

ExperimentRunner resolves configs via 5-layer merge:
  1. experiments/default.yaml  (baseline defaults)
  2. experiment defaults       (experiment-wide defaults)
  3. tasks/<task>.yaml         (task-specific config, wins over defaults)
  4. experiment variant        (variant-specific overrides)
  5. CLI flags                 (always wins)

Per-task (single iteration; simulation mode runs a multi-turn dialog):
  1. Orchestrator._communicate_with_retry(prompt, iteration) → TurnRecord
       (shared by single-shot + simulation paths; wraps
        agent.communicate with execute_with_retry, per-attempt
        turn_timeout, and on_attempt_error → preserves crashed=True
        partial TurnRecords on AgentCrashError / TurnTimeoutError)
  2. SuccessChecker.check_all_async() → List[CriterionResult]

Cleanup: Stop agent, save EvaluationResult, generate reports
```

## Development Commands

```bash
# MANDATORY: Run after every implementation phase
make format      # ruff format
make check       # ruff check (lint)
make typecheck   # pyright
make test        # pytest
make lint        # custom architectural lint rules (CE001+)
make verify      # All of the above + coverage check (CI equivalent)
```

When fixing a bug, ask: *could a custom lint rule have prevented this?* If the root cause is a mechanically detectable pattern (e.g., "always import from `coder_eval.models`", "never call blocking IO in async"), add a rule to `tests/lint/rules/` following the CE001+ pattern and wire it up in `tests/lint/runner.py`. This turns a one-time fix into permanent enforcement. See `tests/test_custom_lint.py` for how rules are tested. (Doc-surface / whole-tree rules that reason over Markdown/YAML or the entire `src/` tree rather than one `.py` AST at a time — CE026–CE031 — are not `BaseRule`s in the runner; they are wired as dedicated `@pytest.mark.lint` test classes. CE031 guards against dead config: a behavior-driving field on `SimulationConfig`/`RunLimits`/`Dataset` that no code reads by name. CE026 keeps the GitHub Action's three onboarding surfaces honest: a page's *first* Action snippet must show the agent-runtime prerequisite steps (pinned to the `action-dogfood` job that proves them in CI), a zero-install absolute next to such a snippet must name the channel it means, and every `github.com/marketplace/actions/<slug>` link plus the shields badge label must match `action.yml`'s `name:`.)

Adding a user-facing field to one of the models CE030 tracks (`TaskDefinition`, `RunLimits`, `Dataset`, `SimulationConfig` — see `tests/lint/doc_schema_parity.py`) means documenting it in its guide (mention the field name as inline code) or adding an `EXEMPT` entry with a reason it is not user-authored. `make lint` fails otherwise.

**Docs index SSOT.** `nav:` plus `extra.docs_index` (blurbs) in `mkdocs.yml` are the single source of truth for the flat index surfaces — `README.md`'s Documentation table, `docs/index.md`'s "Where to go next" table, and the `## Docs` / `## Tutorials` sections of `docs/llms.txt`. Regenerate all three with `make docs-indexes`; **CE028** fails the build if any drifts, if a nav page lacks a blurb (or vice-versa), or if a `docs/*.md` page is missing from the nav. The website sidebar derives from the same `nav:`. When adding or renaming a docs page, edit `nav:` + `extra.docs_index` and run `make docs-indexes` — never hand-edit the generated tables (they sit between `<!-- docs-index:start -->` / `<!-- docs-index:end -->` markers).

**Anchor slugger convention.** The docs are rendered by three sluggers (GitHub, Starlight/github-slugger on coder-eval.com, and python-markdown/mkdocs), which disagree on headings containing `&` or punctuation (`api-routing--benchmarking` vs `api-routing-benchmarking`). Prefer punctuation-free headings so all three agree; if a heading needs `&`, add a GitHub-form `<a id="…"></a>` shim above it and link that form. Verify a new intra-doc anchor link resolves in the built HTML (`mkdocs build`), not by eye.

## Configuration

- **ruff**: line-length=120, target py313, select E/F/I/N/W/UP/B/SIM/RUF
- **pyright**: standard mode, includes `coder_eval/` only, excludes tests
- **pytest**: asyncio_mode=auto, strict markers, coverage source=coder_eval
- **Coverage threshold**: 80% (enforced in CI)

## Extension Points

### Adding a New Criterion

1. Define model in `models/criteria.py` inheriting `BaseSuccessCriterion`
2. Add to `SuccessCriterion` union type
3. Create checker file in `criteria/` inheriting `BaseCriterion`
4. Use `@register_criterion` decorator — auto-discovered at runtime

### Adding a New Agent

Agents are registered through the **plugin SPI** (entry-point group
`coder_eval.plugins`) — there is no closed `AgentKind` enum or
`Orchestrator._create_agent` dispatch to edit. `agent.type` is an open string
validated against `AgentRegistry`; in-tree and third-party agents register the
same way. The
`coder_eval_uipath` Delegate SDK agent is the first real **out-of-tree** worked
example of this SPI (entry point → `register()` hook → `AgentRegistry.register`
+ `register_pricing`, with zero base edits).

1. Define a `BaseAgentConfig` subclass (its own `type: Literal["your-kind"]`) and
   implement the `Agent` ABC in `agents/` (or a separate package for a plugin).
2. Bind them with `registry.register("your-kind", YourConfig)(YourAgent)` inside a
   `register(registry)` hook, exposed via an entry point in the
   `coder_eval.plugins` group (built-ins do this via
   `coder_eval = "coder_eval.agents:register_builtins"`).
3. Before raising on any mid-turn failure, set `self.pending_turn` to a
   `crashed=True` `TurnRecord` built from captured telemetry, then raise
   `AgentCrashError` or `TurnTimeoutError` (bare — no payload on the exception).
   The orchestrator's `_on_attempt_failure` callback drains the slot into
   `result.turns` and calls `discard_pending_turn()` to clear it.
5. The turn lifecycle is shared on the `Agent` base class — do NOT reimplement
   it: call `self._begin_turn()` at the top of `communicate()` (resets
   `pending_turn` + bumps the iteration counter), `self._end_turn_ok()` on the
   success path, and `self._mark_stopped()` in `stop()` (after your own resource
   teardown). `discard_pending_turn()` and `get_state()` are concrete on the
   base and need no override.
6. The agent is the SOLE emitter of the standardized event protocol
   (`streaming/events.py`): emit one `AgentStartEvent` at the top of
   `communicate()` and one matching `AgentEndEvent` on EVERY exit path (success,
   crash, timeout — from `finally`), with `TurnStartEvent`/`TurnEndEvent` per
   inner turn and `ToolStartEvent`/`ToolEndEvent` per tool call (close orphaned
   tools with `status=unresolved`). Fan events through an internal
   `EventCollector` (which builds the returned `TurnRecord` — the single,
   agent-agnostic capture path, so do NOT assemble a `TurnRecord` by hand) plus
   the caller's `stream_callback`. The orchestrator is a pure consumer; renderers
   and the task-log handler consume the same stream.
7. If the agent shells out / holds OS resources, implement real `stop()` /
   `kill()` / `kill_sync()` teardown — `kill_sync()` is called from the
   watchdog's non-asyncio thread, so it must not await.

**Registration pattern:** agents register via the `coder_eval.plugins`
entry-point group (`coder_eval/plugins.py::load_plugins`). The built-in agents
travel the same path — `coder_eval/agents/__init__.py::register_builtins` imports
the agent modules so their `@AgentRegistry.register(...)` decorators fire, and it
asserts the built-ins actually registered (rot-protection). `load_plugins` is
called at CLI init; `ensure_plugins_loaded()` is the lazy safety-net for library
use. A failing third-party plugin is logged and skipped; a failing built-in
registration is fatal.

### Registering Model Pricing (plugins)

Plugins that run their own models contribute USD rates through the
`register_pricing` seam — there is **no** separate entry-point group; call it
from the same `register(registry)` hook used for the agent.

1. Define `dict[str, ModelPricing]` rates (import `ModelPricing` from
   `coder_eval.pricing`); the key is the bare model id as it appears in
   `agent.model` (vendor/Bedrock prefixes are normalized off at lookup).
2. Call `register_pricing(YOUR_RATES)` inside the plugin's `register()` hook.
   `calculate_cost` then consults the registered overlay before the built-in
   table, so every existing consumer (agents, reports) prices the model
   transparently.
3. Registration is **idempotent** for identical rates and **raises** on a
   conflicting rate for an existing key (anti-shadow rule — mirrors
   `AgentRegistry`, so plugin load order can never silently reprice a model). An
   all-zero rate is a valid free-model entry (the lookup uses `is not None`, not
   truthiness). Base ships **no** plugin rates.
   `coder_eval_uipath/pricing.py` is the worked example.

## Task Definition

Tasks are YAML files. See [docs/TASK_DEFINITION_GUIDE.md](docs/TASK_DEFINITION_GUIDE.md) for the full reference. To compare configuration variants across the same tasks, see [docs/AB_EXPERIMENTS.md](docs/AB_EXPERIMENTS.md).

## Dependencies

**Runtime (always)**: pydantic, pydantic-settings, pyyaml, typer, rich, python-dotenv, anthropic, claude-agent-sdk, anyio, radon, tqdm, jmespath, jsonschema

**Runtime (optional, `[uipath]` extra)**: uipath — the in-host `uipath` SDK (handy for local sandbox parity with tasks that invoke `uv run uipath eval ...`). Base installs without this extra still run end-to-end; UiPath-dependent paths fail at dispatch with a clear `pip install 'coder-eval[uipath]'` hint. The LLM judge no longer uses the LLM Gateway client — it routes through the run's backend (Bedrock / Anthropic), so `uipath-llmgw-client` is no longer a dependency.

**Dev**: pytest, pytest-asyncio, pytest-mock, pytest-cov, ruff, pyright, pip-audit, bandit, pre-commit, mcp

## Design Principles

- **DRY (Don't Repeat Yourself)**: Field descriptions, validation rules, and documentation defined once in Pydantic models
- **Single Source of Truth**: Schema models are the authoritative source for parameter definitions
- **Type Safety**: Full type checking with Pydantic and Pyright
- **YAGNI**: Don't add complexity until actually needed
- **KISS**: Keep it simple, stupid!
- **Clean Code**: No dead code, all imports used, all tests passing
- **Greenfield project**: No worries about backward compatibility

## Notes for AI Assistants

- When doing code review, reach out to gemini-3 and codex through multi mcp server
- Any temporary files should be created in `tmp/` folder, NOT `/tmp` folder
- All models are importable from `coder_eval.models` — don't import from submodules directly
- The `criteria/` package uses auto-discovery; new checkers just need the `@register_criterion` decorator
