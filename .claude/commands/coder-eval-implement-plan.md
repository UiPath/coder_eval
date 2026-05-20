---
description: Implement a technical plan phase-by-phase with automated code review after each phase
---

## Context

- Current git status: !`git status --short`
- Current branch: !`git branch --show-current`
- Recent commits: !`git log --oneline -5`

## Your task

You are implementing an approved technical plan for the `coder_eval` codebase, with a built-in code review gate after each phase. The plan path is: $ARGUMENTS

## Getting Started

1. Run `git rev-parse HEAD` and store the result in a task note — this is your reference point for the final code review diff
2. Read the plan file completely and check for any existing checkmarks (`- [x]`)
3. Read all files mentioned in the plan — **read files fully**, never use limit/offset, you need complete context
4. If the plan references a ticket or issue, read that too
5. Think deeply about how the pieces fit together within the coder_eval architecture
6. Use `TaskCreate` to build a checklist mirroring the plan's phases and steps
7. Begin implementation only once the plan is fully understood

If no plan path provided, ask for one.

### Resuming Work

If the plan has existing checkmarks:

- Before resuming, run the success criteria for the most recently completed phase (or `make verify`) to confirm the starting state is clean
- Verify if earlier completed steps are correct
- Resume from the first unchecked item
- Re-verify earlier phases only if inconsistencies appear

## Implementation Philosophy

Plans are carefully designed, but reality can be messy. Your job is to:

- Follow the plan's intent while adapting to what you find in the current codebase
- Implement each phase fully before moving to the next
- **Stay within scope** — do not refactor, improve, or extend beyond what the plan specifies
- Verify your work makes sense in the broader codebase context
- Update checkboxes in the plan as you complete sections using Edit

When things don't match the plan exactly, think about why and communicate clearly:

```
Issue in Phase [N]:
Expected: [what the plan says]
Found: [actual situation]
Why this matters: [explanation]

How should I proceed?
```

### Codebase-Specific Guidelines

When implementing, keep these coder_eval conventions in mind:

- **Models**: All Pydantic models must be importable from `coder_eval.models` — update `models/__init__.py` if adding new models
- **Criteria**: New criteria use `@register_criterion` decorator in `criteria/` and must be added to the `SuccessCriterion` discriminated union in `models/criteria.py`
- **Agents**: New agents implement the `Agent` ABC (start, communicate, stop, get_state) and register in `AgentKind` enum + `Orchestrator._create_agent()`
- **Config merge**: Changes to defaults must consider the 5-layer merge order (default.yaml -> experiment defaults -> task YAML -> variant -> CLI flags)
- **Ripple effects**: When adding/removing/renaming a model field, config key, or CLI flag, trace every reference — task YAMLs in `tasks/`, experiment YAMLs in `experiments/`, slash command templates in `.claude/commands/`, and docs

## Phase Execution Loop

For each phase in the plan, execute this cycle:

### Step 1: Implement

- Make the changes described in the phase
- Follow the plan's details for file paths, function signatures, model fields, etc.
- Handle the edge cases listed in the phase

### Step 2: Commit the Phase

Commit after completing each phase (or logical group of changes):

- Use a conventional commit message referencing the phase (e.g., `feat(criteria): phase 2 — add import_check criterion`)
- This gives clean rollback points if later phases need rethinking
- Do NOT commit broken or partially-verified work

### Step 3: Automated Verification

Run the project's verification suite:

```bash
make format    # ruff format
make check     # ruff check (lint)
make typecheck # pyright
make test      # pytest with coverage
```

If any check fails, fix the issue before proceeding. Do NOT skip checks or suppress warnings. After all pass, run:

```bash
make verify    # all of the above + 80% coverage check
```

If the phase specifies additional test commands (e.g., a specific pytest file), run those too.

### Step 4: Code Review Gate

After automated verification passes, launch an **Opus sub-agent** (`Agent` tool with `model: "opus"`) to review only the files changed in this phase (use `git diff` to identify them). The sub-agent should evaluate against the **Review Criteria** defined below and return findings as severity + file:line + description.

### Step 5: Fix Review Findings

Fix all medium-severity and above issues found by the sub-agent:

- **Logic/correctness bugs**: Write a unit test that fails, fix the code, re-run the test. If not reproducible, mark as false positive.
- **Structural issues** (naming, KISS/DRY): Fix directly — no test required.
- **Regression lint**: For each logic/correctness bug fixed, ask: _is this pattern mechanically detectable by an AST rule?_ If yes, add a custom lint rule to `tests/lint/rules/` following the CE001–CE005 pattern and wire it up in `tests/lint/runner.py`. Run `make lint` to confirm. This converts a one-time fix into permanent enforcement — future code cannot reintroduce the same class of bug.

After all fixes, re-run `make verify`. Commit review fixes as a separate commit (e.g., `fix(criteria): code review fixes for phase 2`) — do not amend the implementation commit. Low-severity issues are noted but not fixed.

### Step 6: Checkpoint

After all fixes pass verification:

1. Check off completed items in the plan file using Edit
2. Update your todo list
3. Pause and inform the user:

```
Phase [N] Complete - Ready for Manual Verification

Automated verification passed:
- [List: make format/check/typecheck/test/verify results]

Code review findings:
- [N] high (all fixed), [N] medium (all fixed), [N] low (noted)

Please perform any manual verification steps listed in the plan:
- [List manual verification items from the plan, if any]

Let me know when ready to proceed to Phase [N+1].
```

If instructed to execute multiple phases consecutively, skip the pause until the last phase.

Do not check off manual testing steps until confirmed by the user.

## If You Get Stuck

When something isn't working as expected:

- Read and understand all the relevant code — check if the codebase evolved since the plan was written
- Check for circular imports if adding new model exports
- Check if `criteria/__init__.py` static fallback set needs updating for new criteria
- Present the mismatch clearly and ask for guidance

Use sub-agents sparingly — mainly for targeted debugging or exploring unfamiliar territory.

## Implementation Completion

When all phases are done:

1. Review all changes against the starting commit SHA (`git diff <start-sha>..HEAD`) to ensure nothing was missed. If something was missed, fix it.
2. If the plan contains a Master Acceptance Checklist, check off each item. For any item you cannot verify, flag it to the user.
3. Run the full verification suite: `make verify`
4. Proceed to the Full Code Review below.
5. After the review, summarize what was implemented, any deviations, and anything left for follow-up.
6. Mark the plan fully complete.

## Full Code Review

After all phases are implemented and verified, run a comprehensive code review of the entire implementation using multiple AI models in parallel. All reviewers evaluate against the same **Review Criteria** below.

1. **Identify scope**: `git diff <start-sha>..HEAD`. Collect all changed files. Read them in full.

2. **Launch two reviews in parallel**:

   **Review A — Multi-model (Gemini + Codex)**: Use `mcp__multi__codereview` with `models: ["gemini-3", "codex"]`, `relevant_files`: absolute paths of all changed files, `content`: review request referencing the Review Criteria below, `step_number`: 1, `next_action`: "stop", `base_path`: project root. If `mcp__multi__codereview` is not available, launch two additional Opus sub-agents instead.

   **Review B — Opus sub-agent**: Use `Agent` tool with `model: "opus"`. List all changed files, ask it to read them and evaluate against the Review Criteria. Return structured findings with severity + location + description, positive observations, and overall assessment.

3. **Synthesize**: Combine findings, deduplicate, note multi-reviewer agreement (higher confidence). Discard false positives or suggestions that contradict the review principles.

4. **Fix medium+ issues**: Same approach as Step 5 — logic bugs get test-first treatment, structural issues fixed directly. Re-run `make verify`. Commit as a separate commit (e.g., `fix: full code review fixes for <plan-name>`). Low-severity left as informational.

## Final Summary

At the end of all phases (or when stopping), present a concise end-to-end summary:

```
## Implementation of <file name / few word summary> Complete

### What Was Built
- (bullet list of each phase and what it delivered)

### Deviations from Plan
- (any places where reality differed from the plan and how you adapted — or "None")

### Code Review Findings
- Critical/High fixed: (count + brief descriptions)
- Medium fixed: (count + brief descriptions)
- Low/informational: (count, left as findings)

### Test Results
- Unit: X/X passing
- Integration: X/X passing (if run)
- Typecheck: clean

### Suggested Next Steps
- (follow-up work, known gaps, or items explicitly deferred from scope)
- (any low-severity review findings worth revisiting)
- (any plan items marked as future work or out of scope)
```

## Review Criteria

Both the per-phase Opus review (Step 4) and the Full Code Review use these same criteria.

**Principles** — evaluate against all of these:

- **Bug-free code**: Logic errors, edge cases, off-by-one errors, unhandled states
- **KISS**: Is the code as simple as it can be? No unnecessary abstractions or indirection
- **DRY**: No duplicated logic that should be consolidated; no premature abstraction either
- **Not over-engineered**: No unnecessary generalization, no speculative features, no "just in case" code
- **Simplicity**: Could a junior developer understand this? Is the intent clear?
- **No unnecessary comments**: Don't describe what the code obviously does
- **CLAUDE.md adherence**: Follows patterns defined in CLAUDE.md and the codebase — no ad-hoc solutions that bypass established abstractions

**Checklist** — check every item:

1. **Correctness**: Does the implementation match the plan's intent? Are all edge cases handled?
2. **Type safety**: Proper annotations, no `Any` escape hatches, Pydantic fields have correct types/defaults/descriptions
3. **Ripple completeness**: All references updated when a model field/config key/CLI flag is added/removed/renamed (task YAMLs, experiment YAMLs, `.claude/commands/`, docs, `experiments/default.yaml`, `models/__init__.py`)
4. **Test quality**: No hardcoded magic values from config; at least one test for the exact edge case; sandbox cleanup via `try/finally` or fixtures; coverage of happy path, invalid input, error paths, boundary conditions
5. **Shell safety**: Commands built via f-string use `shlex.quote()` or argument lists
6. **Allowlist over denylist**: Status classification uses explicit allowlists, not denylists
7. **Resource cleanup**: No file handle, subprocess, or temp directory leaks — especially in error paths
8. **Public API surface**: New exports from `coder_eval.models` are intentional; discriminated unions updated if needed
9. **Regression lint**: For each correctness bug found, consider whether it is mechanically detectable (wrong import path, missing decorator, blocking call in async, silent exception). If so, flag it as a candidate for a new rule in `tests/lint/rules/` — the reviewer should call this out explicitly so it gets actioned in Step 5.
10. **Layer-merge coverage**: New fields on `ResolvedTask` / `AgentConfig` / `BatchRunConfig` have explicit coverage in `test_experiment_resolver.py` exercising all 5 merge layers (default → exp defaults → task → variant → CLI), and a matching CLI override in `_apply_cli_overrides`. Fix-commit family includes `8ed1d6c`, `a5466b5`, `d2fa30d`, `56a5e38`, `2b2086b`.
11. **Pydantic round-trip integrity**: Changes to layered configs or polymorphic `CriterionResult` subclasses preserve `model_fields_set` and the discriminator across `model_dump(exclude_unset=True)` → `model_validate()`. Round-trip tests exist for new variants.
12. **Discriminated unions**: New or modified Pydantic unions use `Annotated[..., Field(discriminator="type")]`. Bare `A | B | C` unions silently coerce to the first variant on a missing or typo'd `type`.
13. **Cross-retry state hygiene**: After `AgentCrashError` / `TurnTimeoutError` / `is_error=True` SDK message, the agent resets `_session_id`, `pending_turn`, watchdog references, streaming-event `ContextVar`s, and iteration counters before the next attempt. Test covers a crashing turn followed by a successful turn in the same `Orchestrator` instance.
14. **Untrusted text in evaluator prompts**: Strings derived from agent output (tool-call args, stdout, file contents, dialog history) injected into a judge / simulator / reviewer prompt are wrapped in a fenced block with explicit untrusted-data framing; the system prompt instructs the model to treat that block as adversarial.
15. **NaN / non-finite guards**: Score and threshold clamps via `max(lo, min(hi, x))` are preceded by `math.isfinite(x)`. Bad parses fail explicitly instead of silently returning the upper bound (`max(0.0, min(1.0, nan)) == 1.0`).
16. **Registry over hardcoded dispatch**: New agent / criterion / template / route variants go through the existing registry (`@register_criterion`, etc.). `if x.type == ...` / `isinstance(...)` ladders in `orchestrator.py`, `simulation/`, or `evaluation/` are rejected.
17. **Test mocks match real SDK shape**: Mocks of `claude_agent_sdk` types only set attributes present on the installed class. Use `Mock(spec=RealType)` or assert `hasattr(RealType, attr)` in test setup so production reads of fabricated fields surface as test failures.
18. **`extra="forbid"` on configs**: Pydantic models that consume YAML or CLI input declare `model_config = ConfigDict(extra="forbid")`. Unknown keys raise instead of being silently dropped.
