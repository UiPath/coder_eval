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
├── fs_permissions.py              # set_permissions: stacked chmod window (via Sandbox.set_permissions)
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
│   ├── criteria.py                # 15 success criterion types + base + union
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
│   ├── evaluation.py              # Reference dir resolution + per-run private staging
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
.claude-plugin/marketplace.json    # Makes this repo a Claude Code plugin marketplace (`/plugin marketplace add UiPath/coder_eval`); lists the one plugin below.
plugins/coder-eval/                # The published Claude Code plugin: `.claude-plugin/plugin.json` (its `version` is a derived pin of pyproject's, bumped by release.yml, guarded by tests/test_action_version_pin.py), `skills/<name>/SKILL.md` × 6 (`/coder-eval:init`, `/coder-eval:check-skill`, `/coder-eval:task`, `/coder-eval:lint-tasks`, `/coder-eval:analyze`, `/coder-eval:ci`), and `reference/` — everything a skill reads must live here, since an installed plugin is copied to ~/.claude/plugins/cache/ WITHOUT its parent dirs (address it via `${CLAUDE_PLUGIN_ROOT}`). `reference/criteria.md` is generated (`make plugin-reference`, CE033); `reference/run-layout.md` is a verbatim mirror of `.claude/shared/run-layout.md`; `reference/task-rubric.md` is the shared task-quality rubric that `task` and `lint-tasks` both read (plugin-only — no repo-side twin); `reference/repo-layout.md` is the eval-tree DISCOVERY policy every skill reads (`SKILL_NEEDS_EVAL_ROOT_DISCOVERY`, which a new skill must declare a stance in) — glob for `task_id:` files and `run.json`, never assume `tasks/`/`runs/latest` — as distinct from `run-layout.md`, which describes what is inside a run directory. Every skill must appear in all four surfaces in `SKILL_DOC_SURFACES` (derived test), and their combined frontmatter `description` length is capped (`SKILL_LISTING_BUDGET_CHARS`) because the skill listing's budget is shared with every skill the user has installed. **Skill naming is verb-first imperative** — a skill is a command you issue (`/coder-eval:<name>`) and every one of them takes an action, so name it for the action: a bare verb where that is unambiguous (`init`, `analyze` — the object comes from the argument), otherwise `<verb>-<object>` (`lint-tasks`, `check-skill`). Never `<object>-<verb>`: `skill-check` was renamed to `check-skill` precisely because it read backwards next to `lint-tasks`. `task` and `ci` predate the rule and stay — renaming a published skill breaks every user's muscle memory for no functional gain, since activation keys on the `description`, never the name. Distinct from `.claude/commands/`, which stays repo-local contributor tooling.
action.yml                         # Published composite GitHub Action (coder-eval as a CI gate). release.yml's `release` job maintains its `version:` default; its `promote` job (gated on publish-pypi) moves the `v<major>` tag + cuts the Release, so nothing consumer-visible moves before the wheel is on PyPI. verify-published-action.yml then verifies the published composite (tag/pin/PyPI/Marketplace parity, plus a real consumer run) after each Release and nightly. Runbook: CONTRIBUTING.md § Releasing.
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
- **Reference solutions are directory-only, and shielded (partially) from the agent**: `task.reference` is a single required `directory:` (relative to the task YAML) — the inline `code:` / single-file `file:` forms are gone, because a directory is the only shape that can be permission-gated as a unit; a `model_validator(mode="before")` gives the removed forms a migration error. The orchestrator stages a **per-run private copy** (`orchestration/evaluation.py::stage_reference_dir`, symlinks stripped) into a tempdir, removed in `_cleanup` via `path_utils.rmtree_restrictive` (keyed on `_reference_staging_root`, recorded BEFORE the copy so a failed copy still cleans up; `rmtree(ignore_errors=True)` silently declines on a tree left at 000) and deliberately never preserved into `run_dir/artifacts`. That copy is held at mode `000` for the whole of every `agent.communicate` call via **`Sandbox.set_permissions`**, the driver-aware wrapper over `fs_permissions.py::set_permissions`. Windows **stack**: exiting restores the *enclosing* window's mode, only the outermost exit restores the pre-window mode — that is what makes a mid-turn re-grant (`mode=READ_ONLY_MODE`) expressible, and it covers two windows at the same mode so no refcount is needed. The window is enforced **only inside a docker container** (`Sandbox.enforces_permission_windows`) and is a no-op on the host, where the agent shares our uid. **That gate keys on the `CODER_EVAL_IN_CONTAINER` env var, NOT `sandbox.driver`** — `run_task_internal_command` rewrites `driver: docker` → `tempdir` before building the in-container Orchestrator, so a driver-based gate would silently disable the anti-cheat on exactly the path that needs it (regression-guarded by `TestSandboxDriverGate`); `resolve_reference_dir` gates its `/work/references` branch on the same var for the same reason. The task directory is **not** shielded (`:ro` mount → EROFS, and the same YAML is readable at `/work/input`). Criteria address reference files with the `$REFERENCE_DIR` token (same resolver as `$TASK_DIR`) and the `REFERENCE_DIR` env var for `run_command`; `reference_comparison` names one file via `reference_file`. Docker mounts a throwaway **read-write** copy at `/work/references` (a `:ro` mount cannot be chmod'd — EROFS), masks the in-task-dir original with an empty tmpfs, and drops `DAC_OVERRIDE`/`DAC_READ_SEARCH`. `FOWNER`/`CHOWN` are deliberately **NOT** dropped: the in-container orchestrator that applies the window is the same root process with the same caps, so dropping `FOWNER` breaks *the harness's own* chmod wherever the bind mount preserves a non-root owner (native Linux — verified: `chmod: Operation not permitted`), i.e. exactly where the drop would otherwise bite. A window that cannot be applied is now a hard error, not a warning: `Sandbox.set_permissions` passes `strict=True` whenever it enforces, so an unprotected run fails instead of producing a normal-looking score. **KNOWN GAP — this is defense-in-depth, not a boundary**: (a) `chmod(2)` is gated on owner-or-`CAP_FOWNER` and the container runs as root owning the copy, so a deliberate `chmod 755 /work/references` restores access; (b) the window spans `agent.communicate` only, and nothing reaps agent child processes at turn end, so a backgrounded read loop succeeds once the window closes. The **write** half of (b) is closed — `path_utils.digest_tree` hashes the tree at staging and `Orchestrator._verify_reference_integrity` re-checks before grading, raising `ReferenceTamperedError` (→ `FinalStatus.ERROR`) on a mismatch so an agent cannot overwrite the reference to drive `reference_comparison` to 1.0. Passive reads are blocked; an adversarial agent is not. Full containment requires running the agent as a non-root uid AND holding the window for the agent's whole lifetime — follow-up. `tasks/anti_cheat_reference` probes the passive-read half.
- **Harness run-limit parity**: a shared `BaseAgentConfig` field must mean the same thing on every backend, so a divergence is either fixed or documented — never silent. **`run_limits.max_turns` on Codex/Antigravity counts VISIBLE turns** (resolved tool calls, read live off the shared `EventCollector.visible_turn_count`, the same list `TurnRecord.commands` holds) because one `communicate()` is a single SDK turn on both, so a native counter would clamp at 1; claude-code keeps its native SDK cap, whose unit (an agent-loop turn) absorbs arbitrarily many parallel calls — the same number is NOT the same budget across harnesses. The cap is enforced on the same loop boundary as the cooperative early stop and finalizes cleanly as `max_turns_exhausted` (no crash, no retry); on Antigravity that boundary lives in `_drain()`, so the background-work poll loop honors it too. Known unfixed divergences: `permission_mode` on Codex and Antigravity (both run unconfined — the sandbox driver is the isolation boundary), `disallowed_tools` on Codex (forwarded, not SDK-enforced), `allowed_tools`/`disallowed_tools` on Antigravity (not read at all), and `turn_timeout` on Antigravity (bounded by an earlier internal poll deadline at 80% of it). Full table + rationale: docs/agents/HARNESS_PARITY.md.
- **sandbox isolation**: Tasks that don't need MCP servers should set `setting_sources: []` in their `agent:` block to isolate the sandbox from the host project's CLAUDE.md and settings. Without this, the host project's CLAUDE.md (often 20 KB+) is injected into every API call, inflating cache-creation tokens and cost significantly.
- **Run-time caps (non-criterion enforcement)**: `TaskDefinition.run_limits` (`RunLimits` model) is the single namespace for all *task-level* run-time caps — `max_turns` / `task_timeout` / `turn_timeout` (structural) and `max_input_tokens` / `max_output_tokens` / `max_total_tokens` / `max_usd` (cumulative budget). Token/USD breaches abort with `FinalStatus.TOKEN_BUDGET_EXCEEDED` or `COST_BUDGET_EXCEEDED` (both `category == "failed"`). Structural caps are set from the CLI via `-D run_limits.max_turns=…` / `-D run_limits.task_timeout=…` / `-D run_limits.turn_timeout=…` (field-merged into `run_limits`); budget caps via `-D run_limits.max_usd=…` etc. or YAML. Layered config uses field-merge — a variant block overrides individual keys without replacing the task's block. The one *per-criterion* cap, `stop_early.decide_within`, deliberately lives on `LiveSuccessCriterion` instead (see below) — the watcher must attribute a decision-step timeout to a specific criterion, which `RunLimits` (task-scoped, criterion-agnostic) cannot express.
- **Early stop on criterion (opt-in, per-criterion arming)**: a `stop_early:` block (`StopEarlyPolicy`) on a criterion ends a single-shot run early once the run's **armed** criteria decide the outcome, so a raised `max_turns` isn't wasted on the smoke flavor. The block's PRESENCE is the arming and alone activates the watcher — there is **no run-level master switch**: `run_limits.stop_early: false` is the run-level KILL SWITCH that force-disarms every block (the one-line experiment-variant/`-D` override for an authoritative full run), and `run_limits.stop_early: true` (the removed master arm) is a hard `EarlyStopConfigError` at resolution. The block exists on `LiveSuccessCriterion` only (currently `skill_triggered`, `command_executed` — so arming an unobservable criterion is unrepresentable, a pydantic extra-forbid error). Arming carries one implicit trigger (a native live-fail may fail-stop the run); its keys refine it: `on_pass: stop` (pass-stop the moment the criterion live-passes; default `continue` just latches) and `decide_within: N` (still undecided after N tool-call steps latches an **effective fail**, fed through the same fail-stop rule, reported as `decision_budget_exceeded` — an ordinary weighted fail, NOT a gate-bypassing force-fail; cumulative across retry attempts of the same turn). A trigger whose polarity the instance can't decide (per the abstract, checker-independent `live_decidable_polarities()`, a pure function of the criterion's own fields, paired with the checker's `live_verdict` override by lint rule CE025, a registry-based whole-tree check) is **inert by design** — one dataset-fanned YAML line serves both positive rows (pass/timeout live) and distractor rows (fail live). Verdicts **latch**: once a criterion decides, its `live_verdict` is never polled again. Stop rule is weighted, not strict-boolean: `run_limits.stop_early_gate_threshold` (default `1.0`, reproducing strict-AND behavior exactly) is the minimum weighted score (`Σ weight·score / Σ weight` over the armed subset) required to pass; a fail-stop fires once the armed set's **ceiling** (best case for everything still undecided) can no longer reach the threshold — so a low-weight fail or timeout that can't doom the gate is absorbed and the run continues — and is **deferred while any pass-capable armed criterion is undecided** (a distractor misfire never truncates a positive row's recall signal); a pass-stop fires once the `on_pass: stop` subset's **floor** (worst case) already meets the threshold, and is symmetrically **deferred while any pass-capable armed criterion outside the `on_pass: stop` subset is undecided** (so an early pass never freezes a sibling `on_pass: continue` criterion's signal out of the trajectory). A fail-stop is therefore verdict-preserving; a pass-stop can miss a *later* distractor misfire, so authoritative P/R/F1 comes from a kill-switched (`stop_early: false`) run. Driven by `orchestration/early_stop.py::EarlyStopWatcher` (built when `early_stop_active(task)`: ≥1 armed criterion, kill switch not thrown) through the agent's cooperative `should_stop` seam (tool-call granularity, no SIGKILL); live verdicts only *trigger* the stop — the standard `check_all_async` on the frozen trajectory is authoritative. Gating is **FIRED-ONLY**: a run the watcher actually cut gates on the **armed subset** via the weighted `EvaluationResult.armed_criteria_passed`; a run that completes naturally — armed or not — gates strict-AND via `all_criteria_passed`, so adding a block never changes the verdict of a run it didn't cut. Note the gate keys on the watcher having FIRED (`result.early_stop is not None`), not on confirmed truncation — an agent that ignores `should_stop`, or a stop firing on the final message, still gates armed-only. Every resolution-time guardrail violation is a hard error at resolution (plan *and* run); the one load-time case — a `stop_early:` block on a non-live criterion — is a pydantic schema error at task load, which the run surface reports as a skipped task like any other malformed task. A runtime verdict bug **fails open** to a full run. Surfaces: `EarlyStopInfo` (incl. `gate_threshold` at stop time), report notes/badges, `stopped_early` run.json rows, `EarlyStopped`/`EarlyStopReason` telemetry dims. Worked rationale: docs/TASK_DEFINITION_GUIDE.md § `stop_early`. No blocks anywhere ⇒ behavior byte-for-byte unchanged.

## Success Criteria (15 types)

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
| `cli_called` | Binary | Structured match over a JSON Lines invocation log: verb (or `verb_any_of` alternation) / positional / per-flag predicates, with min_count/max_count bounds |
| `commands_efficiency` | Continuous | Agent tool-call efficiency relative to expected budget |
| `uipath_eval` | Fractional | UiPath agent evaluation results |
| `classification_match` | Binary | File-based label match (observed vs expected) with `(none)`/`(other)` sentinels; emits `ClassificationCriterionResult` for suite-level P/R/F1 |
| `skill_triggered` | Binary | Did the agent engage the target skill? Agent-agnostic — Claude's `Skill` tool call, or (Codex) reading the skill's files off disk. Emits `ClassificationCriterionResult` for suite-level P/R/F1 |
| `llm_judge` | Continuous | LLM grades artifacts + optional trajectory + optional reference; routes through the run's backend (Bedrock / Anthropic) |
| `agent_judge` | Continuous | Spawns a Claude Code SDK agent in an isolated sandbox copy; judge uses tools (Bash/Read/Grep/…) to investigate and returns a JSON verdict. Expensive; runs with evaluator credentials — see SECURITY note in the criterion docstring. |

All criteria support `weight` (default 1.0) and `pass_threshold` (default 0.9), plus (on live criteria only) a `stop_early:` block (`on_pass`, `decide_within`) that arms the criterion for early stop by its presence. On dataset-backed tasks, criteria may also set `suite_thresholds: {metric: min_value}` — the suite gate passes iff every listed metric (from the criterion's `aggregate()` output) meets its minimum.

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

# The JS half (evalboard/). Separate because it needs a Node/pnpm toolchain,
# but gated in CI by the `evalboard` job just like the Python side.
make evalboard-verify   # tsc --noEmit + vitest + next build

# Regenerate a generated surface (both are CE-guarded; never hand-edit the output)
make docs-indexes      # README/docs index tables from the mkdocs nav (CE028)
make plugin-reference  # the plugin's bundled criteria reference from the models (CE033)
```

Editing `src/coder_eval/pricing.py` means editing `evalboard/lib/pricing.ts` too — it is a hand-copied mirror, and `evalboard/lib/__tests__/pricing-parity.test.ts` fails the build on drift in either direction.

Recent additions, each traceable to a shipped defect: **CE036** (no unreferenced module-level private helper in `src/` — a helper whose docstring documents a bug the live code still has is worse than none), **CE037** (in an `@asynccontextmanager`, the acquire must sit INSIDE the `try` whose `finally` releases it — `asyncio.shield` protects the inner task, NOT the await, so a cancel on `__aenter__` skips the unwind while the work completes), **CE038** (a criterion checker must not return a gating `score=0.0` from an `except OSError` over a path the *task author* named — that books an eval-config error as an agent failure; raise `CheckerMisuseError` instead, and `# noqa: CE038` the cases that really are the agent's).

When fixing a bug, ask: *could a custom lint rule have prevented this?* If the root cause is a mechanically detectable pattern (e.g., "always import from `coder_eval.models`", "never call blocking IO in async"), add a rule to `tests/lint/rules/` following the CE001+ pattern and wire it up in `tests/lint/runner.py`. This turns a one-time fix into permanent enforcement. See `tests/test_custom_lint.py` for how rules are tested. (Doc-surface / whole-tree rules that reason over Markdown/YAML or the entire `src/` tree rather than one `.py` AST at a time — CE026–CE031, CE033, CE034, CE035 — are not `BaseRule`s in the runner; they are wired as dedicated `@pytest.mark.lint` test classes. CE035 resolves every `steps.<id>.outputs.<key>` / `needs.<job>.outputs.<key>` reference in `.github/workflows/**` to a writer that actually produces that key — GitHub expands an unwritten output to the empty string, so a typo degrades a gate silently and actionlint models `steps.*.outputs` as an open string map. CE034 scans `tasks/` and forces an armed, live-*passable* `command_executed` to set `require_success` — a crashed invocation would otherwise latch a live PASS, fire `on_pass: stop`, and let FIRED-ONLY armed gating report SUCCESS without ever consulting the unarmed criteria (negative assertions are fail-only and are exempt). CE033 keeps the plugin's bundled `reference/criteria.md` in parity with the `SuccessCriterion` union that generates it (`make plugin-reference` writes it; the rule re-renders and diffs — never hand-edit the file). CE031 guards against dead config: a behavior-driving field on `SimulationConfig`/`RunLimits`/`Dataset` that no code reads by name. CE026 keeps the GitHub Action's onboarding surfaces honest — `README.md`, `docs/CI_GATE.md`, `docs/tutorials/02-ci-pipeline.md`, and the plugin's `ci` skill, whose emitted workflow users copy into their own repos: a page's *first* Action snippet must show the agent-runtime prerequisite steps (pinned to the `action-dogfood` job that proves them in CI), a zero-install absolute next to such a snippet must name the channel it means, every `github.com/marketplace/actions/<slug>` link plus the shields badge label must match `action.yml`'s `name:`, and every `with:` key on a snippet's action step must be a real `action.yml` input (GitHub ignores unknown inputs, so a rename would silently degrade every copied workflow). Renaming an action input or changing its runtime prerequisites therefore means updating the skill too.)

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

- Communication style: Use ASD-STE-100 when you speak to the user.
- When doing code review, reach out to gemini-3 and codex through multi mcp server
- Any temporary files should be created in `tmp/` folder, NOT `/tmp` folder
- All models are importable from `coder_eval.models` — don't import from submodules directly
- The `criteria/` package uses auto-discovery; new checkers just need the `@register_criterion` decorator
