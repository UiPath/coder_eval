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
├── reports_optimize.py            # The optimize gate's PRESENTATION half — every markdown block the skill prints. Stays in the reports family, NOT in `optimize/`. No filesystem and no runtime import of the gate — pinned by a test, and that boundary is what makes the split real. Its ONE runtime statistics import is `reports_stats.bootstrap_p_floor`, which the same test REQUIRES: CE040 makes that value derived rather than respelled.
├── optimize/                      # The `/coder-eval:optimize-skill` DECISION family, ranks 0-4 plus the ladder-exempt `store.py`. `__init__.py` is a docstring and NOTHING else — read it for the no-facade rule and the two sensors holding it. The RANKS are declared in exactly ONE place: `_OPTIMIZE_RANKS` in `tests/test_optimize_layering.py`, whose `pkgutil`-derived module set asserts every module here HAS a rank. A SPECIFICATION, not a derivation — `activation -> execution` is acyclic and still wrong. Inside the package a name two modules share is PUBLIC (CE059).
│   ├── load.py                    # Rank 0 — reads a finalized run tree: loading, pairing, provenance, reconciliation (CE053), the row primitives and rule attribution. Decides nothing. Rationale in the module docstring.
│   ├── gate.py                    # Rank 1 — the primitives BOTH tracks share: the gate constants (four WATCHED by the estimator ledger, plus `FLOOR_RESOLUTION`), the notes both tracks emit, the ONE `holm_rejections` call site, `decide_family` (the ONE promotion loop, and therefore the ONE `promoted` conjunction — each track supplies only a `decide(verdict, FamilyFacts) -> TrackDecision` hook), `floor_preflight` (the three guards both noise floors open with, in the ONE order that is correct), `FirstCause` (the first-cause-wins refusal sink), and Stage C's shared classifier.
│   ├── activation.py              # Rank 2 — the activation track: does a candidate DESCRIPTION get the skill engaged? `f1.yes` over `skill_triggered`, paired cluster bootstrap. Owns the discreteness floor, the sibling checks, the cross-split refusal and seed stability.
│   ├── execution.py               # Rank 2, beside activation and importing nothing from it — does a candidate BODY produce better outcomes? Per-row `weighted_score` through `reports_stats.paired_comparison`. Owns the sign rule, the integrity checks, dead weight and Stage C.
│   ├── fronts.py                  # Rank 3 — `arm_row_scores` and the three fronts over the Stage A matrix (Pareto = DISCARD, instance-best = MERGE shortlist, cost/quality = ADVISORY), plus `headroom_ceiling`. All treat a hole as absent, never zero.
│   ├── search.py                  # Rank 3, beside fronts — `search_compare` (the accept/revert decision, emphatically NOT a gate) and `candidate_leaks` + `skill_text`, the anti-memorization preflight that reads a whole skill DIRECTORY.
│   │                              #   **Both tracks mean ONE thing by `promoted`:** Holm rejected AND `separated` AND no refusal AND every check list passing — `failed_vetoes` is the one declaration of which lists veto, so a failed guardrail FORCES False and the field alone is safe to ship on. Rationale on the models' fields and in `reports_optimize._headline`.
│   ├── api.py                     # Rank 4 — the ONE module `SKILL.md` imports, and the only one allowed to import `reports_optimize`. 18 composites over the skill's fences: each absorbs that fence's guards, fallbacks and track branch, returns the markdown block the skill prints, and DECIDES nothing. `record_*` writes; `*_report` does not. **CE066** is what makes the surface declared rather than "whatever the snippet binder resolves". Rationale: `.claude/decisions/2026-08-20-the-skill-facing-api.md`.
│   ├── store.py                   # The `measurements.json` sidecar and its ONE reader/writer: a cache (noise floors, per key) and a corpus (regression rows, append-only) at once. Outside the ladder; imports `coder_eval.models` alone.
├── analysis.py                    # Command statistics aggregation
├── logging_config.py              # Structured logging setup
├── path_utils.py                  # Run ID generation, path utilities
├── fs_permissions.py              # set_permissions: stacked chmod window (via Sandbox.set_permissions)
├── pricing.py                     # Model pricing / cost calculation (ModelPricing, calculate_cost, register_pricing)
├── litellm_cost.py                # Join proxy-captured ACTUAL per-call cost/cache onto turns (LiteLLM backend; apply_actual_cost)
├── suite_fingerprint.py           # The SUITE half of instrument provenance: a SHA-256 over every criterion's CONCRETE subclass dump (`scoring_dump`, minus a reason-carrying denylist of ONE, `description`), the prompt TEMPLATE as authored, the EXPANDED rows the round scored (id + prompt + substituted criteria, sorted by id) and the whole `run_limits` block. Read by `optimize.store.suite_changed`, the three-valued twin of `grader_changed`. Its own module because the store PERSISTS and this COMPUTES (`leak_detection.py` / `reports_optimize.py`'s precedent) — not a cycle, which does not exist. Takes the UNEXPANDED suite task as its FIRST argument and the expanded rows as its second; the two are different things and mixing them is what the signature prevents. DIGEST only, never the pre-image, because `measurements.json` is committed. **Why the rows are load-bearing, why a denylist is the safe direction here, why `run_limits` is hashed whole, and what the length-prefixing is actually worth: .claude/decisions/2026-08-20-instrument-provenance.md.**
├── leak_detection.py              # Verbatim-leak primitive: `LEAK_LOCATOR_FIELDS`, `LEAK_MIN_CHARS`, `string_leaves`, `graded_strings`. ONE declaration, THREE consumers pointing in different directions — CE036, CE057 and `optimize.search.candidate_leaks`. A second copy would agree on ordinary input and diverge exactly where either was written for. Its own module rather than three names on `optimize.gate`, because a task-lint rule importing from the optimize gate inverts the dependency. `graded_strings(drop_type=)` is the one behavioural difference between the consumers.
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
│   ├── copy_with.py               # `copy_with(model, /, **updates)` — the validated replacement for `model_copy(update={...})`, which does NOT check the update's keys: a mistyped one lands as a bare instance attribute, absent from `model_dump()` entirely, with the intended field left at its default and nothing raised (`extra="forbid"` does not help — that governs validation, and `model_copy` skips it). It closes the KEY hole only, deliberately not re-validating values and deliberately not accepting a dict, because literal keywords are what pyright and a reader see. **CE048** keeps the call shape from coming back. Its first parameter is POSITIONAL-ONLY, and that is load-bearing: `NoiseFloor` has a field literally named `model`. A cycle-free leaf under `models/` rather than beside `pricing.py`, since CE001 routes every consumer through `coder_eval.models`.
│   ├── judge_defaults.py          # DEFAULT_JUDGE_MODEL constant (cycle-free leaf)
│   ├── optimize.py                # `/coder-eval:optimize-skill` records: GateVerdictBase (the 18 fields both Stage B verdicts share, declared once; each track's subclass adds only its own extras, and every re-declaration of a base field is licensed with its reason in `_FIELD_OVERRIDES`, which is the ONE place that set is written down), ActivationGateVerdict, NoiseFloor, ArmRowScores, RoundScores, RegressionRow, OptimizeMeasurements — plus TARGET_LABEL and the two `NoiseFloor.metric` values (ACTIVATION_FLOOR_METRIC / EXECUTION_FLOOR_METRIC), cycle-free leaf constants on the same precedent as judge_defaults.py (NoiseFloor.metric defaults to f"f1.{TARGET_LABEL}", and this module cannot import optimize.gate, which imports it)
│   ├── mutations.py               # PromptMutation variants (prefix/suffix/replace/template)
│   ├── results.py                 # CriterionResult (+ ClassificationCriterionResult), TurnRecord, EvaluationResult, EarlyStopInfo/EarlyStopReason, CriterionAggregate, ThresholdCheck, SuiteRollup
│   ├── row_selection.py           # RowSelection (`split` / `max_rows` / `sample_per_stratum`) + ROW_SELECTOR_FLAGS (field -> CLI flag). A cycle-free leaf embedded by BOTH `orchestration.config.BatchRunConfig` (the request) and `models.results.RunSummary` (the record, persisted into run.json). Deliberately declares NO `extra="forbid"`: it nests under a container that does and one that does not, and run.json must stay readable when written by a NEWER coder-eval, so a future fourth selector is an ignored key rather than a hard parse failure of the whole report — the config-side typo risk is covered by pyright at the single construction site. ROW_SELECTOR_FLAGS lives here rather than under `cli/` because `reports.py` renders those flag names and a report module importing from `coder_eval.cli` is the upward dependency CE004 forbids.
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
│   ├── plan_command.py            # `coder-eval plan` (also expands datasets: previews total/selected row counts, takes all three row selectors — `--split` / `--sample` / `--sample-per-stratum`, shared with `run` via `cli/row_selectors.py` and pinned by CE043 — names WHICH selector narrowed the set from `RowSelectionOutcome.applied` rather than re-deriving the win-order, prints a per-stratum breakdown keyed by `task_loader.stratum_key`, and warns on partial labelling and on an unseeded stratified draw)
│   ├── report_command.py          # `coder-eval report`
│   ├── row_selectors.py           # The three row-selector CLI HELP strings, shared by `run` and `plan` so the two surfaces cannot describe one flag two ways (CE043 asserts `is` identity). The field -> flag MAP is deliberately elsewhere (`models/row_selection.py::ROW_SELECTOR_FLAGS`) because `reports.py` renders those names and a report module importing from `coder_eval.cli` inverts the layering. Two strings, two jobs: the model's field `description` documents the RECORDED VALUE, these document the SELECTOR ACTION.
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
plugins/coder-eval/                # The published Claude Code plugin: `.claude-plugin/plugin.json` (its `version` is a derived pin of pyproject's), `skills/<name>/SKILL.md` × 7 — `/coder-eval:init`, `/coder-eval:check-skill`, `/coder-eval:optimize-skill`, `/coder-eval:task`, `/coder-eval:lint-tasks`, `/coder-eval:analyze`, `/coder-eval:ci` — and `reference/`. **Everything a skill reads must live here**, since an installed plugin is copied to ~/.claude/plugins/cache/ WITHOUT its parent dirs (address it via `${CLAUDE_PLUGIN_ROOT}`). `reference/criteria.md` is generated (`make plugin-reference`, CE033); `reference/run-layout.md` mirrors `.claude/shared/run-layout.md`; `reference/templates/` holds the two suites the skills copy into a user's repo, plus `outcome-grader/`, the execution track's measuring instrument — whose contract (exit 0 on every failure, params validated, a NOT-APPLICABLE verdict triggered by the ROW and never the artifact) is in its own module docstring, and which ships BESIDE `outcome-fixture/` because everything under a mounted fixture is copied into every sandbox. `reference/task-rubric.md`, `reference/optimize-method.md` and `reference/repo-layout.md` are the shared surfaces the skills read; each names its own readers. Every skill must appear in all four `SKILL_DOC_SURFACES` (derived test), and their combined frontmatter `description` length is capped, because the skill listing's budget is shared with every skill the user has installed. **Skill naming is verb-first imperative** — a bare verb where unambiguous (`init`, `analyze`), otherwise `<verb>-<object>` (`lint-tasks`, `check-skill`, `optimize-skill`), never `<object>-<verb>`. `task` and `ci` predate the rule and stay: renaming a published skill breaks muscle memory for no gain, since activation keys on the `description`. Distinct from `.claude/commands/`, which is repo-local contributor tooling.
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
- **Dataset fan-out**: `TaskDefinition.dataset` (inline rows or JSONL path) expands one task into N row-tasks with `${row.<field>}` substitution in `initial_prompt` and `success_criteria` strings. Expansion runs in `task_loader.expand_dataset` **before** variant resolution, so variants cannot override the dataset — and so row selection takes part in no merge layer and needs no `MergeField` strategy. Selection is **filter-then-sample**, and that order is a correctness requirement rather than a preference: sampling first would leave an unpredictable number of rows per split, destroying the train/test comparison. `--split <name>` filters first; `--sample N` (fixed seed) overrides `--sample-per-stratum N` / `dataset.sample_per_stratum` (stratified, **nondeterministic by default** so the nightly broadens coverage — set `dataset.sample_seed` to pin it). A *labelled* task with no matching row raises `SplitSelectorError`, which `resolve_all_tasks` **re-raises** rather than demoting to `skipped_tasks`: it describes a malformed INVOCATION applied to every task, not a malformed file. Partial labelling stays legal but warns, and CE060 forbids it in-repo. **The selection is persisted**: all three selectors are one model, `models/row_selection.py::RowSelection`, embedded by both `BatchRunConfig` (the request) and `RunSummary` (the record), so `run.json` carries it and `optimize.gate` can refuse a pair whose arms recorded different splits. `RunSummary.row_selection is None` means **not recorded**, which is deliberately not the same as a recorded `split: null`. Full reference: docs/TASK_DEFINITION_GUIDE.md; the pre-`row_selection` flat aliases and their removal in **0.11.0** are documented on `BatchRunConfig` itself.
- **Per-criterion aggregation**: Each `BaseCriterion` subclass exposes `aggregate(criterion, per_row_results) -> CriterionAggregate | None`. Default emits `count / mean / median / std / min / max` so every criterion is suite-thresholdable for free. Classification-style criteria return `ClassificationCriterionResult` (subclass of `CriterionResult`) and layer accuracy / P/R/F1 / confusion via the shared `overlay_classification_metrics` utility. `BaseSuccessCriterion.suite_thresholds` gates the suite on those metrics; CLI exits non-zero on any gate failure.
- **Sub-agent token accounting**: there is NO separate per-sub-agent field. Every sub-agent generation is a `parent_tool_use_id`-tagged `AssistantMessage` in the turn transcript, so per-sub-agent usage is DERIVED by grouping on that id. Claude bubbles intermediate generations into the parent stream natively and the **terminal** one (delivered as the Agent tool result, never streamed) is synthesized via `_synthesize_subagent_terminal_message`; Codex reconstructs all child generations from the child rollout. The turn total already includes sub-agent cost. `CommandTelemetry.result_summary` is stored **untruncated** so sub-agent returns are preserved whole. `CODER_EVAL_RAW_SDK_LOG=1` dumps every raw SDK event to the task log.
- **Reconciliation message (stream self-reconciles to the turn total)**: the per-message stream consistently UNDER-reports the authoritative turn total — a fixed prompt slice (~512 input tokens on Claude) is billed on no SDK-emitted message, and sub-agent input/cache only partially bubbles up. So `EventCollector.build_turn_record` appends one synthetic `ReconciliationMessage` per turn carrying the per-bucket residual. **The invariant: summing the four token buckets across `TurnRecord.messages` equals `token_usage` exactly**, on every backend. That is what lets the evalboard SUM the message stream as the source of truth instead of reading a separate aggregate. Booked at the single `EventCollector` seam, so it is agent-agnostic; carries no cost; excluded from generation/turn counts and the cost simulator. The LiteLLM actual-cost join writes cost at the TURN level only and does not touch message buckets, so `EventCollector` stays the single writer and the invariant holds. Details in that module's docstrings.
- **Reference solutions are directory-only, and shielded (partially) from the agent**: `task.reference` is a single required `directory:` (relative to the task YAML) — the inline `code:` / single-file `file:` forms are gone, because a directory is the only shape that can be permission-gated as a unit; a `model_validator(mode="before")` gives the removed forms a migration error. The orchestrator stages a **per-run private copy** (`orchestration/evaluation.py::stage_reference_dir`, symlinks stripped) into a tempdir, removed in `_cleanup` via `path_utils.rmtree_restrictive` (keyed on `_reference_staging_root`, recorded BEFORE the copy so a failed copy still cleans up; `rmtree(ignore_errors=True)` silently declines on a tree left at 000) and deliberately never preserved into `run_dir/artifacts`. That copy is held at mode `000` for the whole of every `agent.communicate` call via **`Sandbox.set_permissions`**, the driver-aware wrapper over `fs_permissions.py::set_permissions`. Windows **stack**: exiting restores the *enclosing* window's mode, only the outermost exit restores the pre-window mode — that is what makes a mid-turn re-grant (`mode=READ_ONLY_MODE`) expressible, and it covers two windows at the same mode so no refcount is needed. The window is enforced **only inside a docker container** (`Sandbox.enforces_permission_windows`) and is a no-op on the host, where the agent shares our uid. **That gate keys on the `CODER_EVAL_IN_CONTAINER` env var, NOT `sandbox.driver`** — `run_task_internal_command` rewrites `driver: docker` → `tempdir` before building the in-container Orchestrator, so a driver-based gate would silently disable the anti-cheat on exactly the path that needs it (regression-guarded by `TestSandboxDriverGate`); `resolve_reference_dir` gates its `/work/references` branch on the same var for the same reason. The task directory is **not** shielded (`:ro` mount → EROFS, and the same YAML is readable at `/work/input`). Criteria address reference files with the `$REFERENCE_DIR` token (same resolver as `$TASK_DIR`) and the `REFERENCE_DIR` env var for `run_command`; `reference_comparison` names one file via `reference_file`. Docker mounts a throwaway **read-write** copy at `/work/references` (a `:ro` mount cannot be chmod'd — EROFS), masks the in-task-dir original with an empty tmpfs, and drops `DAC_OVERRIDE`/`DAC_READ_SEARCH`. `FOWNER`/`CHOWN` are deliberately **NOT** dropped: the in-container orchestrator that applies the window is the same root process with the same caps, so dropping `FOWNER` breaks *the harness's own* chmod wherever the bind mount preserves a non-root owner (native Linux — verified: `chmod: Operation not permitted`), i.e. exactly where the drop would otherwise bite. A window that cannot be applied is now a hard error, not a warning: `Sandbox.set_permissions` passes `strict=True` whenever it enforces, so an unprotected run fails instead of producing a normal-looking score. **KNOWN GAP — this is defense-in-depth, not a boundary**: (a) `chmod(2)` is gated on owner-or-`CAP_FOWNER` and the container runs as root owning the copy, so a deliberate `chmod 755 /work/references` restores access; (b) the window spans `agent.communicate` only, and nothing reaps agent child processes at turn end, so a backgrounded read loop succeeds once the window closes. The **write** half of (b) is closed — `path_utils.digest_tree` hashes the tree at staging and `Orchestrator._verify_reference_integrity` re-checks before grading, raising `ReferenceTamperedError` (→ `FinalStatus.ERROR`) on a mismatch so an agent cannot overwrite the reference to drive `reference_comparison` to 1.0. Passive reads are blocked; an adversarial agent is not. Full containment requires running the agent as a non-root uid AND holding the window for the agent's whole lifetime — follow-up. `tasks/anti_cheat_reference` probes the passive-read half.
- **Harness run-limit parity**: a shared `BaseAgentConfig` field must mean the same thing on every backend, so a divergence is either fixed or documented — never silent. `run_limits.max_turns` counts VISIBLE turns on Codex/Antigravity (one `communicate()` is a single SDK turn there, so a native counter would clamp at 1) while claude-code keeps its native SDK cap: **the same number is not the same budget across harnesses.** Enforced on the same loop boundary as the cooperative early stop, finalizing cleanly as `max_turns_exhausted`. Known unfixed divergences and their rationale: docs/agents/HARNESS_PARITY.md.
- **sandbox isolation**: Tasks that don't need MCP servers should set `setting_sources: []` in their `agent:` block to isolate the sandbox from the host project's CLAUDE.md and settings. Without this, the host project's CLAUDE.md (often 20 KB+) is injected into every API call, inflating cache-creation tokens and cost significantly.
- **Run-time caps (non-criterion enforcement)**: `TaskDefinition.run_limits` is the single namespace for all *task-level* caps — `max_turns` / `task_timeout` / `turn_timeout` (structural) and `max_input_tokens` / `max_output_tokens` / `max_total_tokens` / `max_usd` (cumulative budget). Token and USD breaches abort with `TOKEN_BUDGET_EXCEEDED` / `COST_BUDGET_EXCEEDED`. Set from the CLI via `-D run_limits.<field>=…` or YAML; layered config uses field-merge, so a variant overrides individual keys without replacing the block. The one *per-criterion* cap, `stop_early.decide_within`, deliberately lives on `LiveSuccessCriterion` instead: the watcher must attribute a decision-step timeout to a specific criterion, which `RunLimits` (task-scoped, criterion-agnostic) cannot express.
- **Early stop on criterion (opt-in, per-criterion arming)**: a `stop_early:` block on a criterion ends a single-shot run early once the run's **armed** criteria decide the outcome. The block's PRESENCE is the arming — there is no run-level master switch; `run_limits.stop_early: false` is a kill switch that force-disarms every block, and `true` is a hard `EarlyStopConfigError`. It exists on `LiveSuccessCriterion` only, so arming an unobservable criterion is unrepresentable. Keys: `on_pass: stop` (pass-stop the moment it live-passes) and `decide_within: N` (still undecided after N **completed** tool calls latches an effective fail, reported as `decision_budget_exceeded`). The stop rule is WEIGHTED, not strict-boolean, via `run_limits.stop_early_gate_threshold` (default `1.0`, reproducing strict-AND exactly). Gating is **FIRED-ONLY**: a run the watcher actually cut gates on the armed subset, a run that completes naturally gates strict-AND — so adding a block never changes the verdict of a run it did not cut. A fail-stop is verdict-preserving; a pass-stop can miss a *later* distractor misfire, so authoritative P/R/F1 comes from a kill-switched run. Driven by `orchestration/early_stop.py::EarlyStopWatcher` through the agent's cooperative `should_stop` seam — tool-call granularity, no SIGKILL — and a runtime verdict bug **fails open** to a full run. Every resolution-time guardrail violation is a hard error at plan *and* run. **The full rationale — why `completed` is load-bearing, why parallel dispatch needs `_pending_tool_ids`, why a fail-stop defers while any pass-capable criterion is undecided, and what `live_decidable_polarities()` is for — is in that module's docstrings and docs/TASK_DEFINITION_GUIDE.md § `stop_early`.** No blocks anywhere ⇒ behavior byte-for-byte unchanged.

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

Recent additions, each traceable to a shipped defect: **CE037** (no unreferenced module-level private helper in `src/` — a helper whose docstring documents a bug the live code still has is worse than none), **CE038** (in an `@asynccontextmanager`, the acquire must sit INSIDE the `try` whose `finally` releases it — `asyncio.shield` protects the inner task, NOT the await, so a cancel on `__aenter__` skips the unwind while the work completes), **CE039** (a criterion checker must not return a gating `score=0.0` from an `except OSError` over a path the *task author* named — that books an eval-config error as an agent failure; raise `CheckerMisuseError` instead, and `# noqa: CE039` the cases that really are the agent's).

When fixing a bug, ask: *could a custom lint rule have prevented this?* If the root cause is a mechanically detectable pattern (e.g., "always import from `coder_eval.models`", "never call blocking IO in async"), add a rule and turn a one-time fix into permanent enforcement. **Each rule's own module docstring is the authority on what it does, what it deliberately does NOT catch, and what it cost** — that boundary is part of the rule, so read it before trusting a green `make lint`.

- **Where a rule lives.** An AST check over one `.py` file at a time is a `BaseRule` in `tests/lint/rules/`, wired in `tests/lint/runner.py`. A rule reasoning over Markdown, YAML, a resolved Typer signature, resolved model metadata or the whole `src/` tree is a `@pytest.mark.lint` class instead — roughly a third of them are. Detection bodies too large for either live beside `tests/lint/skip_guards.py` as shared readers.
- **Where its tests live.** `tests/lint_tests/`, grouped by what the rule reasons over. `make lint` selects on the MARKER, so a new module or class is covered the moment it is marked.
- **Claiming a number.** `tests/lint/runner.py`'s id-uniqueness assert covers `ALL_RULES` ONLY, so a class-wired id can collide with a `BaseRule`'s — or with a number that is RESERVED (CE056) or RETIRED (CE044) — without failing anything. Grep `tests/` and `.claude/harness-candidates.md` first; `test_a_reserved_or_retired_id_is_not_live` is what keeps the register honest.
- **Not everything under `tests/lint/` is a numbered rule.** `cli_flags.py`, `markdown_tables.py`, `import_resolution.py` and `task_yaml_discovery.py` are shared readers; `estimator_ledger.py` carries no CE number deliberately, because it is the **estimator-change protocol** — a diff-based gate that cannot run in `make verify` (a working tree has no base ref) and runs as the `pull_request`-only `estimator-protocol` job. It demands a row in `docs/REPORT_SCHEMA.md`'s `## Estimator changes` table whenever a PR moves a constant in `WATCHED_CONSTANTS` or modifies a pinned rendered-number fixture, because a rendered statistic can step for IDENTICAL data and nothing in a run artifact tells that apart from a real change in the measurement.
- **One derived sentence lives here rather than in a rule docstring**, because a test binds both directions of it. CE036 forbids a dataset row's prompt from containing, verbatim, a string one of its criteria grades it on. Location fields (`path`, `agent_file`, `file_path`, `command`, `skill_name`) are exempt: naming WHERE to write removes filename nondeterminism from the measurement without revealing WHAT is graded — and `skill_name` is a locator for the same reason, naming WHICH skill must engage while the graded thing is the engagement EVENT, which no prompt can supply. The list is `LEAK_LOCATOR_FIELDS` in `src/coder_eval/leak_detection.py` and `test_ce036_exemption_list_matches_claude_md` fails if either side is edited alone.

Adding a user-facing field to one of the models CE030 tracks (`TaskDefinition`, `RunLimits`, `Dataset`, `SimulationConfig` — see `tests/lint/doc_schema_parity.py`) means documenting it in its guide (mention the field name as inline code) or adding an `EXEMPT` entry with a reason it is not user-authored. `make lint` fails otherwise.

**Docs index SSOT.** `nav:` plus `extra.docs_index` (blurbs) in `mkdocs.yml` are the single source of truth for the flat index surfaces — `README.md`'s Documentation table, `docs/index.md`'s "Where to go next" table, and the `## Docs` / `## Tutorials` sections of `docs/llms.txt`. Regenerate all three with `make docs-indexes`; **CE028** fails the build if any drifts, if a nav page lacks a blurb (or vice-versa), or if a `docs/*.md` page is missing from the nav. The website sidebar derives from the same `nav:`. When adding or renaming a docs page, edit `nav:` + `extra.docs_index` and run `make docs-indexes` — never hand-edit the generated tables (they sit between `<!-- docs-index:start -->` / `<!-- docs-index:end -->` markers).

**Anchor slugger convention.** The docs are rendered by three sluggers (GitHub, Starlight/github-slugger on coder-eval.com, and python-markdown/mkdocs), which disagree on headings containing `&` or punctuation (`api-routing--benchmarking` vs `api-routing-benchmarking`). Prefer punctuation-free headings so all three agree; if a heading needs `&`, add a GitHub-form `<a id="…"></a>` shim above it and link that form. Verify a new intra-doc anchor link resolves in the built HTML (`mkdocs build`), not by eye.

## Configuration

- **ruff**: line-length=120, target py313, select E/F/I/N/W/UP/B/SIM/RUF + `C90` (mccabe `max-complexity = 30`). The ceiling is a RATCHET set one above the worst function in the tree, so it costs no refactor and still fails a new one above it; there are deliberately **no** `C901` per-file-ignores, because a second entry in such a list means the ceiling is wrong rather than that a file is special. Lowering it toward 20 is tracked in `.claude/harness-candidates.md`.
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
