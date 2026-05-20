---
description: Create a structured implementation plan for a feature or change in the coder_eval codebase
---

## Context

- Current git status: !`git status --short`
- Current branch: !`git branch --show-current`
- Recent commits: !`git log --oneline -5`

## Your task

Create a detailed, phased implementation plan that could be executed from a fresh session — with no memory of this conversation. The input may be:

- A direct description of a feature or change
- A file path containing requirements, a spec, or a design doc — read it first
- A list of bugs or issues to fix
- A combination of the above

Follow these steps:

1. **Gather input** — If the user references a file, read it in full before proceeding. If the input is a list of bugs/issues, enumerate each one. If the input is a direct description, use it as-is.

2. **Understand the requirement** — Restate the goal in one or two sentences to confirm understanding. If the input contains multiple items (e.g. a bug list), summarize the scope and list each item.

3. **Research the codebase** — Read all relevant files to understand the current state. Identify existing patterns, conventions, and code that will be affected. Do not skip this step. Pay special attention to:
   - `coder_eval/models/` — Pydantic data models (all importable from `coder_eval.models`)
   - `coder_eval/criteria/` — Plugin registry with auto-discovery via `@register_criterion`
   - `coder_eval/agents/` — Agent ABC implementations
   - `coder_eval/orchestration/` — Batch execution and experiment resolution
   - `coder_eval/cli/` — Typer + Rich CLI commands
   - `coder_eval/streaming/` — Real-time LLM event streaming via `StreamCallback` protocol

4. **Think through the design** — Before writing phases, reason explicitly about:
   - Does this change touch the evaluation flow? (CLI -> ExperimentRunner -> run_batch -> Orchestrator -> Sandbox + Agent + SuccessChecker)
   - Does this affect the 5-layer config merge? (default.yaml -> experiment defaults -> task YAML -> variant -> CLI flags)
   - If adding a new criterion: does it fit the existing `BaseCriterion` / `@register_criterion` / discriminated union pattern?
   - If adding a new agent: does it follow the `Agent` ABC contract (start, communicate, stop, get_state)?
   - Does this change affect task YAML schema? If so, what happens to existing task files?
   - Are there edge cases in sandbox isolation, snapshot timing, or agent lifecycle?
   - Does this introduce new dependencies? Prefer what's already in the project (pydantic, typer, rich, anyio).
   - Could this break existing experiments or evaluation results?

5. **Write the plan** — Break the work into sequential phases. Each phase should be a logical, independently testable unit of work. When the input is a bug list, group related bugs into the same phase where it makes sense; keep unrelated fixes in separate phases. Make sure the plan is consistent with the architectural patterns in CLAUDE.md. Before writing phases: scan for existing helpers, constants, or patterns this work would duplicate — extract rather than copy. If the plan replaces existing functionality, explicitly list what gets deleted. Before finalising the phase sequence, check: what breaks if any two adjacent phases are swapped? Note inter-phase dependencies explicitly. For each phase, specify:
   - **What**: Files to create or modify, with concrete details (model fields, function signatures, class names, etc.)
   - **Edge Cases**: Specific failure modes, boundary conditions, or unusual inputs this phase must handle correctly — for each, explicitly consider: can any new field be None? Does any comparison need normalization? Could concurrent sandbox operations conflict? Additionally, check for these patterns that have caused bugs in past PRs:
     - **Ripple effects**: If a model field, config key, or CLI flag is added/removed/renamed, trace every reference — task YAMLs, experiment YAMLs, slash command templates in `.claude/commands/`, docs, and `experiments/default.yaml`. List every file that must be updated.
     - **Allowlist over denylist**: When classifying statuses or filtering values, prefer explicit allowlists (`in (A, B, C)`) over denylists (`not in (X, Y)`) so new enum values don't silently fall into the wrong bucket.
     - **Shell safety**: Any command built via f-string that runs in a sandbox must use `shlex.quote()` or argument lists — never embed scripts in bare double-quoted strings.
   - **Tests to Write**: Cover: happy path; invalid input; error paths; boundary conditions. For features that touch the orchestrator or evaluation flow, include integration tests. Watch for these test pitfalls from past PRs:
     - Don't hardcode magic values from config files (e.g., `assert timeout == 300`); read the expected value from the source (`default_exp.defaults.turn_timeout`) so the test survives config changes.
     - Write at least one test that exercises the exact edge case the feature handles (e.g., if a criterion handles dotted imports, test `foo.bar` not just `foo`).
   - **Tests to Run**: Specific pytest commands or markers to validate the phase (e.g., `uv run pytest tests/test_criteria.py -v`)
   - **Acceptance Criteria**: Observable, verifiable conditions that confirm the phase is complete

6. **Flag risks and open questions** — Call out ambiguities, edge cases that need a design decision, or anything that needs user input before starting.

7. **Save the plan** — Store the plan to the `c/YYYY-MM-DD-plan-name.md` file (unless specified otherwise) with a descriptive filename. Do not implement any code; only produce the plan and wait for user approval.

8. **Self-review for standalone clarity** — Re-read the saved plan file from disk. Verify that a fresh session — with no memory of this conversation — could implement every phase without asking clarifying questions. Check: are file paths concrete? Are Pydantic model fields spelled out with types? Are function signatures and import paths exact? If anything relies on context from this conversation that isn't in the plan, update the plan with that information. If you cannot resolve an ambiguity yourself, add it to the Open Questions section.

## Output format

Use this structure exactly:

```
## Goal
<one-sentence summary>

## Scope
**In scope:** <bullet list>
**Out of scope:** <bullet list — be explicit about what is NOT being built>

## Affected Files & Modules
<flat list of every file that will be created or modified — include path and one-line reason>

## Design Context
<narrative covering any of the following that apply:>
- Which part of the evaluation flow is affected (CLI, experiment resolution, orchestrator loop, sandbox, agent, criteria, reports)
- New or changed Pydantic models (field names, types, defaults, validators) — all must be importable from `coder_eval.models`
- New or changed discriminated unions (criteria types, template sources)
- Plugin registry changes (new `@register_criterion` checkers, new agent kinds)
- Config merge implications (which layer does this affect? does it need a new default in `experiments/default.yaml`?)
- New CLI commands or flags (Typer command signatures, help text)
- Task YAML schema changes (backward compatibility with existing task files)
- Streaming/callback changes (new `StreamEvent` types, renderer updates)
- New dependencies (justify each — prefer existing: pydantic, typer, rich, anyio, anthropic)

## Phase 1: <title>

### Changes
- <file>: <what changes and why, with enough detail to implement without ambiguity>

### Edge Cases
- <specific scenario>: <how it should be handled>

### Tests to Write
- <test description and what it verifies>

### Tests to Run
- `uv run pytest <specific test file or marker>`
- `make verify` (at minimum for final phase)

### Acceptance Criteria
- [ ] <observable, verifiable condition>

## Phase 2: <title>
...

## Master Acceptance Checklist

### Code Quality
- [ ] `make format` passes (ruff format)
- [ ] `make check` passes (ruff check — E/F/I/N/W/UP/B/SIM/RUF, line-length=120)
- [ ] `make typecheck` passes (pyright strict mode)
- [ ] `make test` passes (pytest with coverage)
- [ ] `make verify` passes (all of the above + 80% coverage threshold)

### Design Principles
- [ ] No unused imports or dead code introduced
- [ ] No duplicated logic — shared helpers extracted where appropriate
- [ ] Consistent naming with existing codebase conventions
- [ ] KISS: no unnecessary abstractions or "just in case" code
- [ ] DRY: no repeated field descriptions, validation rules, or logic
- [ ] YAGNI: nothing added that isn't needed right now

### Models & Types
- [ ] New Pydantic models use proper field types with defaults and descriptions
- [ ] New models are exported from `coder_eval/models/__init__.py`
- [ ] Discriminated unions updated if new criterion/template types added
- [ ] No `Any` escape hatches without justification

### Error Handling
- [ ] Every async operation has proper error handling
- [ ] Sandbox cleanup happens even on failure
- [ ] No silent failures — errors are either handled or propagated with context

### Testing
- [ ] Happy path covered for every new function/class
- [ ] Invalid input and error paths covered
- [ ] Boundary conditions covered (empty lists, None values, missing files)
- [ ] All existing tests still pass
- [ ] Coverage stays above 80%

### Feature Spec (for larger features)
- [ ] Concise feature spec saved to `docs/features/YYYY-MM-DD-feature-name.md` — covering what the feature does, how to configure it, and where it fits in the evaluation flow (not an implementation plan — a user-facing reference)

## Open Questions
- <question>: <why it matters, what the options are>
```

Do NOT start implementing. Only produce the plan and wait for user approval.
