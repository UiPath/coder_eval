---
description: Create a structured, phased implementation plan for a feature or change in the coder_eval codebase, executable from a fresh session by /coder-eval-implement-plan
---

## Context

- Current git status: !`git status --short`
- Current branch: !`git branch --show-current`
- Recent commits: !`git log --oneline -5`

## Your task

Produce a detailed, phased plan that a **fresh session with no memory of this conversation** can execute via `/coder-eval-implement-plan`. The plan is the contract between you and the implementer — it must be self-contained. The implementer drives directly off these parts, so each must be concrete:

- **Per phase**: `Changes`, `Edge Cases`, `Tests to Write`, `Tests to Run` (real scoped `uv run pytest …` commands), `Acceptance Criteria` (verifiable, with checkboxes), and a **`Risk` tag** (Low / High — sets the implementer's review depth).
- **Global**: `Patterns to Mirror` (actual code snippets from this repo, not descriptions), `Design Context`, `Master Acceptance Checklist`, `Confidence Score`.

The input may be a feature description, a file path to a spec/design doc (read it first), a bug list, or a combination.

Follow these steps:

1. **Gather input** — If the user references a file, read it in full first. If it's a bug list, enumerate each item. If it's a direct description, use it as-is.

2. **Understand & classify** — Restate the goal in one or two sentences. If there are multiple items, summarize scope and list each. Then classify:

   | Level | Indicators |
   |-------|-----------|
   | Small | 1-3 files, follows existing patterns, <100 new lines, single phase |
   | Medium | 3-10 files, one new criterion / one CLI flag group / one model addition, 2-4 phases |
   | Large | 10+ files, new agent or cross-module interaction (orchestrator + models + criteria), 5+ phases |
   | XL | Architectural change, new subsystem, migration of an existing contract — split into multiple plans |

   For **Small**, collapse to one phase (or a flat task list). For **XL**, stop now — present the classification rationale and a proposed split, and wait for user confirmation before any further research or planning.

3. **Research the codebase** — Read all relevant files to understand the current state. Implement by mirroring and re-using existing patterns, not inventing. Pay special attention to:
   - `coder_eval/models/` — Pydantic data models (all importable from `coder_eval.models`; declared once, consumed everywhere — SSOT)
   - `coder_eval/criteria/` — Plugin registry with auto-discovery via `@register_criterion`; `SuccessCriterion` discriminated union in `models/criteria.py`
   - `coder_eval/agents/` + `coder_eval/plugins.py` — Agent ABC implementations registered through the **`coder_eval.plugins` entry-point SPI** against `AgentRegistry`. `agent.type` is an open string; `Orchestrator._create_agent` delegates to the registry's `create_agent()` factory (`agents/registry.py`) instead of dispatching by kind, so you add an agent via a `register(registry)` hook — **not** by editing `_create_agent` or the `AgentKind` enum (`models/enums.py`, which lists only the known built-in kinds)
   - `coder_eval/orchestration/` — Batch execution, experiment resolution, and the single declarative merge resolver (`config_merge.py::resolve_root`)
   - `coder_eval/cli/` — Typer + Rich CLI commands; generic `-D`/`--set` overrides (`orchestration/overrides.py`)
   - `coder_eval/streaming/` — Real-time agent event streaming; the agent is the sole emitter and `EventCollector` is the single TurnRecord capture seam
   - `tests/lint/rules/` — custom architectural lint rules (CE001–CExxx), the project's harness for mechanically-enforced invariants

   Compile findings into **Patterns to Mirror** with actual snippets and `file:line` references, each tagged `APPLIES_TO: Phase N`.

4. **Think through the design** — Before writing phases, reason explicitly about:
   - Does this change touch the evaluation flow? (CLI → ExperimentRunner → run_batch → Orchestrator → Sandbox + Agent + SuccessChecker)
   - Does this affect the 5-layer config merge? (default.yaml → experiment defaults → task YAML → variant → CLI flags). Each list/dict field must declare its `MergeField` strategy (lint rule CE014).
   - If adding a new criterion: does it fit `BaseCriterion` / `@register_criterion` / the `SuccessCriterion` discriminated union? Does it need a custom `aggregate()` for suite thresholds?
   - If adding a new agent: does it follow the plugin SPI (a `BaseAgentConfig` subclass + `Agent` ABC + a `register(registry)` hook exposed via the `coder_eval.plugins` entry-point group)? Does it use the shared turn lifecycle (`_begin_turn`/`_end_turn_ok`/`_mark_stopped`) and emit the standardized event protocol?
   - Does this change the task YAML schema? If so, what happens to existing task files in `tasks/`?
   - Are there edge cases in sandbox isolation, agent lifecycle, retry/crash recovery, or token accounting?
   - Does this introduce new dependencies? Prefer what's already in the project (pydantic, typer, rich, anyio, anthropic).
   - Could this break existing experiments or evaluation results?

5. **Simplicity & reuse gate (KISS / DRY / SSOT / YAGNI)** — Before writing phases, audit the intended design against the project's Design Principles (in `CLAUDE.md`) and resolve tensions *with the user*, not silently:
   - **KISS / YAGNI** — Is any phase introducing an abstraction, config layer, or generality the *current* requirement doesn't need? Is there a simpler shape (a constant instead of a config field, a direct call instead of a new layer, mirroring an existing criterion instead of a new base class)?
   - **DRY** — Does any new field type, constant, validation rule, helper, or model duplicate something that already exists? (You scanned for helpers during research — now decide reuse-vs-extract for each.)
   - **SSOT** — Is each piece of knowledge (enum values, field constraints, criterion discriminators, pricing rates, default config) defined in exactly one canonical place (a Pydantic model, the registry, `experiments/default.yaml`) and *consumed* everywhere else?

   For each genuine tension where more than one reasonable design exists — "extract a shared helper now vs. inline it", "new criterion vs. extend an existing one", "new model field vs. reuse an existing one" — **stop and ask the user with `AskUserQuestion`**, leading with your recommended (principle-compliant) option. Record each resolution in the plan (in **Scope** or the phase's **Changes**) so the implementer inherits the decision.

6. **Harness Impact (feedforward → sensor)** — This project enforces invariants with custom lint rules (`tests/lint/rules/`, CE001–CExxx). For each phase, ask: *does it introduce a new convention, invariant, or failure mode that a lint rule could enforce more cheaply than a reviewer re-checking it every time?* When a phase establishes a rule that should hold *everywhere, forever* (a new layering boundary, a new "every X must have Y" requirement, a new always-present field/decorator), make the guardrail a **phase deliverable**:
   - Prefer a **computational** sensor (cheapest, deterministic): a new CExxx lint rule (+ its test, wired into `tests/lint/runner.py`), a Pydantic validator, or a unit/registry test.
   - Reserve **prose** (CLAUDE.md / Master Acceptance Checklist) for invariants that need semantic judgement a rule can't express.
   - State the chosen guardrail in the owning phase's **Changes** (and **Tests to Write** if it ships a rule test). If a phase introduces a new invariant but you decide *not* to add a sensor, say why in one clause (e.g. "single call site, not a recurring class"). Do not silently leave a new invariant prose-only.

7. **Write the plan** — Break work into sequential, independently testable phases. Group related bugs into one phase; keep unrelated fixes separate. Before writing phases: scan for existing helpers/constants/patterns this work would duplicate — extract rather than copy; if the plan replaces existing functionality, list explicitly what gets deleted. Check inter-phase dependencies: what breaks if any two adjacent phases are swapped? Note dependencies explicitly. For each phase, applying this thinking:
   - **Risk** — tag each phase **Low** or **High** using the **Risk triggers** below. State the trigger in one clause — this sets the implementer's per-phase review depth, so be honest.
   - **Edge Cases** — for each change consider: can any new field be `None` (validation *and* implementation must both handle it)? Does any comparison need normalization? Could concurrent sandbox operations conflict? Then check these patterns that have caused bugs in past PRs:
     - **Ripple effects**: If a model field, config key, or CLI flag is added/removed/renamed, trace every reference — task YAMLs in `tasks/`, experiment YAMLs in `experiments/`, `experiments/default.yaml`, slash-command templates in `.claude/commands/`, docs, and `models/__init__.py`. List every file that must be updated.
     - **Allowlist over denylist**: When classifying statuses or filtering values, prefer explicit allowlists (`in (A, B, C)`) over denylists (`not in (X, Y)`) so new enum values don't silently fall into the wrong bucket (lint rule CE018 guards `FinalStatus` denylists).
     - **Shell safety**: Any command built via f-string that runs in a sandbox must use `shlex.quote()` or argument lists — never embed scripts in bare double-quoted strings.
   - **Tests to Write** — list before the implementation step (the implementer writes the failing test first, confirms the right failure, then implements). Cover: happy path; invalid input (one per required field/enum); error paths; boundary conditions. For features touching the orchestrator or evaluation flow, include integration tests. Watch these test pitfalls from past PRs:
     - Don't hardcode magic values from config files (e.g. `assert timeout == 300`); read the expected value from the source (`default_exp.defaults.turn_timeout`) so the test survives config changes.
     - Write at least one test that exercises the exact edge case the feature handles (e.g. if a criterion handles dotted imports, test `foo.bar` not just `foo`).
     - Tests use **Haiku or at most Sonnet** — never Opus (cost).

   Keep the plan consistent with the architectural patterns and Design Principles in `CLAUDE.md`.

8. **Flag risks and open questions** — Ambiguities, edge cases needing a design decision, anything needing user input before starting.

9. **Save the plan** — Store the plan to `c/YYYY-MM-DD-<kebab-case-name>.md` (unless the user specifies otherwise). Do NOT implement — produce the plan and wait for approval.

10. **Self-review for standalone, implement-readiness & principles** — Launch a sub-agent (`Agent`, `subagent_type: "general-purpose"`) to cold-read the saved file. In the prompt: give the absolute path, instruct a full read, and ask it to answer three questions:
    - *Standalone:* "Could you implement every phase from a fresh session with no prior context? Flag anything needing a clarifying question: vague file paths, implied model shapes, missing enum values, unspecified import paths, or steps assuming unwritten knowledge."
    - *Implement-ready (consumed by `/coder-eval-implement-plan`):* "Does every phase have a Risk tag, runnable `uv run pytest …` Tests to Run, and Acceptance Criteria that are objectively verifiable (not vibes)? Is each Patterns-to-Mirror snippet real code with a `file:line` source? Flag any phase missing these."
    - *Principles (KISS / DRY / SSOT — re-read against `CLAUDE.md` Design Principles):* "Flag any phase that introduces an abstraction the stated scope doesn't require (KISS/YAGNI); duplicates an existing helper/constant/model/criterion instead of reusing it (DRY); or re-declares knowledge (enum values, discriminators, default config) in more than one place instead of a single canonical source (SSOT). For each flag, name the simpler or canonical alternative."

    Return a numbered list of gaps, or "No gaps found". Fix each gap by editing the plan. If a principles gap has a clear compliant fix, apply it. If resolving a gap needs a design decision from the user, ask with `AskUserQuestion`; if it can't be resolved now, move it to **Open Questions**.

## Risk triggers

**Defined in `.claude/shared/review-rubric.md` → "Risk triggers"** (read it) — the single source of truth shared with `/coder-eval-implement-plan`, which uses your per-phase `Risk:` tag to scale review depth. In short: **High** for new behaviour (criterion/agent/CLI/evaluation path), a model/schema change existing YAMLs depend on, an agent-lifecycle or config-merge change, token/pricing, an untrusted-prompt surface, or multi-module edits; otherwise **Low**.

## Output format

Use this structure for section headings and order. Within conditional sections ("if applicable"), include only relevant items and omit the rest:

```
## Goal
<one-sentence summary>

## Scope
**In scope:** <bullet list>
**Out of scope:** <bullet list — be explicit about what is NOT being built>

## Affected Files & Modules
<flat list of every file created or modified — path and one-line reason>

## Patterns to Mirror

Code patterns from the codebase the implementer must follow. Actual snippets — not descriptions. Tag each with the phase it serves.

### <Pattern name>   — APPLIES_TO: Phase N
# SOURCE: <file>:<lines>
[actual code]

Include only the patterns actually relevant to the plan.

## Design Context
<narrative covering any of the following that apply:>
- Which part of the evaluation flow is affected (CLI, experiment resolution, orchestrator loop, sandbox, agent, criteria, reports, streaming)
- New or changed Pydantic models (field names, types, defaults, validators) — all must be importable from `coder_eval.models`
- New or changed discriminated unions (criteria types, template sources, routes) — must use `Annotated[..., Field(discriminator="type")]`
- Plugin registry changes (new `@register_criterion` checkers, new agents via the `coder_eval.plugins` SPI, new pricing via `register_pricing`)
- Config merge implications (which layer? does it need a new default in `experiments/default.yaml`? does a new list/dict field need an explicit `MergeField` strategy — CE014?)
- New CLI commands or flags (Typer signatures, help text) vs. a `-D`-only override
- Task YAML schema changes (backward compatibility with existing files in `tasks/`)
- Streaming/callback changes (new event types, renderer/collector updates)
- New dependencies (justify each — prefer existing: pydantic, typer, rich, anyio, anthropic)

## Phase 1: <title>

**Risk:** Low | High — <one-clause trigger, e.g. "High — adds a new criterion + changes the SuccessCriterion union">

### Changes
- <file>: <what changes and why, with enough detail to implement without ambiguity>

### Edge Cases
- <specific scenario>: <how it should be handled>

### Tests to Write
- <test description and what it verifies>

### Tests to Run
- `uv run pytest <specific test file or marker> -v`  (scoped to this phase — do NOT run the full suite per phase)
- <any integration test relevant to this phase, with the exact command>

### Acceptance Criteria
- [ ] <observable, verifiable condition 1>
- [ ] <observable, verifiable condition n>
- [ ] Scoped tests pass (commands in Tests to Run)
- [ ] If this phase added/changed a model field, config key, or CLI flag: every ripple reference updated (task/experiment YAMLs, `experiments/default.yaml`, `.claude/commands/`, docs, `models/__init__.py`)

## Phase 2: <title>
...

## Master Acceptance Checklist

### Code Quality
- [ ] `make format` passes (ruff format)
- [ ] `make check` passes (ruff check — E/F/I/N/W/UP/B/SIM/RUF, line-length=120)
- [ ] `make typecheck` passes (pyright)
- [ ] `make test` passes (pytest with coverage)
- [ ] `make lint` passes (custom CExxx architectural rules)
- [ ] `make verify` passes (all of the above + 80% coverage threshold)

### Design Principles
- [ ] No unused imports or dead code introduced
- [ ] No duplicated logic — shared helpers extracted where appropriate
- [ ] Consistent naming with existing codebase conventions
- [ ] KISS / DRY / YAGNI: no unnecessary abstractions or "just in case" code

### Models & Types
- [ ] New Pydantic models use proper field types with defaults and descriptions
- [ ] New models are exported from `coder_eval/models/__init__.py`
- [ ] Discriminated unions updated and use `Field(discriminator="type")` if new criterion/template/route types added
- [ ] Config models consuming YAML/CLI input declare `model_config = ConfigDict(extra="forbid")`
- [ ] No `Any` escape hatches without justification

### Registry & Plugins (if applicable)
- [ ] New criterion uses `@register_criterion` and is in the `SuccessCriterion` union
- [ ] New agent registers via the `coder_eval.plugins` entry-point SPI (no closed-enum dispatch) and uses the shared turn lifecycle + event protocol
- [ ] New model pricing registered via `register_pricing` in the plugin's `register()` hook

### Config Merge (if applicable)
- [ ] New list/dict fields declare their `MergeField` strategy (CE014)
- [ ] New `ResolvedTask` / `AgentConfig` fields have coverage across all 5 merge layers and a matching `-D` override path

### Error Handling
- [ ] Sandbox cleanup happens even on failure (try/finally or fixtures)
- [ ] No silent failures — errors handled or propagated with context
- [ ] Crash/timeout paths preserve the partial `crashed=True` TurnRecord and reset cross-retry state

### Testing
- [ ] Happy path covered for every new function/class
- [ ] Invalid input, error paths, and boundary conditions (empty lists, None, missing files) covered
- [ ] Tests use Haiku/Sonnet, never Opus
- [ ] All existing tests still pass; coverage stays above 80%

## Open Questions
- <question>: <why it matters, what the options are>

## Confidence Score

**Score**: X/10 — likelihood of one-pass implementation success.

**Rationale**:
- <what is well-understood>
- <what is uncertain or not yet researched>

A score below 7 means the plan is not ready. Still save and present it, but add a prominent callout at the very top of the plan file:
`> **Warning:** Confidence X/10 — not ready for implementation. Resolve Open Questions first.`
`/coder-eval-implement-plan` will refuse to start while the score is below 7.
```

Do NOT start implementing. Only produce the plan and wait for user approval.
