---
description: Implement an approved coder_eval plan phase by phase with risk-scaled per-phase review, then a final code review
argument-hint: <plan-file-path>
---

## Context

- Current git status: !`git status --short`
- Current branch: !`git branch --show-current`
- Recent commits: !`git log --oneline -5`

# Implement Plan

Implement an approved plan produced by `/coder-eval-create-plan` phase by phase, verifying and reviewing each phase before moving on, then run one cross-cutting review of the whole change. The plan is your contract: it carries, per phase, a **Risk** tag, **Changes**, **Edge Cases**, **Tests to Write**, **Tests to Run** (scoped commands), and **Acceptance Criteria**, plus a global **Master Acceptance Checklist**, **Patterns to Mirror** (real code snippets), and a **Confidence Score**. Drive off those — do not re-derive what the plan and `CLAUDE.md` already specify. The plan path is: $ARGUMENTS

If no plan path was provided, ask for one.

## When to use this

This command is for **multi-phase plans** where phases build on each other and the cost of a wrong turn is high. For a single trivial change (one file, a rename, a typo, a one-line fix), skip the ceremony below — make the change, run the relevant scoped test, and stop. Do not spawn review sub-agents for work a human would eyeball in ten seconds. (If the plan's Confidence Score is below 7, stop — per `/coder-eval-create-plan` it isn't ready; surface that instead of implementing.)

## The loop

Everything below expands this core loop. Hold it in your head:

```
pre-flight (understand plan + Patterns to Mirror, learn conventions, capture <start-sha>, build task list + findings file)
for each phase, in order:
    implement   → write the phase's "Tests to Write" first for new behaviour, then the code
    verify      → Stage 1: the phase's "Tests to Run" + scoped lint must pass
    review      → Stage 2: spec-compliance, then (risk-scaled) quality
    commit      → conventional message with a progress counter
final review of the whole change → fix → tick Master Acceptance Checklist → summary
```

Complete one phase fully before starting the next. Stay within the plan's scope — do not refactor or extend beyond what it specifies.

---

## Pre-flight

Do all of this once, before writing any code:

- **Check readiness.** Read the plan's Confidence Score; if it's below 7 (or the plan carries a "not ready" warning callout), stop and surface it — don't implement.
- **Determine the resume point.** A phase counts as done if **any** of these hold — do not rely on checkboxes alone (plans are often completion-tracked by commit, leaving boxes unticked): its Acceptance Criteria are checked `- [x]`; a commit with a matching `N/M` progress counter exists (`git log --oneline | grep -E '\b<N>/<M>\b'`); or the findings file already has a `## Phase N` block. The first phase satisfying none is where you start.
- **Capture the baseline** (`<start-sha>`, the final-review reference point) — it must sit **before the first phase's work**, not at current HEAD. Fresh run (start = Phase 1): `git rev-parse HEAD`. Resuming (start = Phase K > 1): the commit before the `1/M` commit — `git rev-parse "$(git log --oneline | grep -E '\b1/<M>\b' | tail -1 | cut -d' ' -f1)^"`; if no `1/M` commit exists, ask the user for the pre-work SHA rather than guessing. Store it in a task note. This keeps the completion suite and final review covering **all** phases, not just the resumed ones.
- **Read everything, fully.** The plan (including its **Patterns to Mirror**), the originating ticket/issue, and every file in **Affected Files & Modules** — read those files completely (no limit/offset). The Patterns to Mirror snippets are how this codebase does it; mirror them, don't invent.
- **Learn the conventions.** Read `CLAUDE.md` and `.claude/shared/review-rubric.md` and **follow them exactly** — CLAUDE.md is the source of truth for where models/criteria/agents live, the registry patterns, the 5-layer config merge, the agent lifecycle, sandbox isolation, token accounting, and the custom-lint harness; the shared rubric is the source of truth for Risk triggers, the Severity rubric, the Fix Policy, the Harness loop, and the 18-item Review Criteria. When the plan and these conventions conflict, stop and use the **Mismatch Format**.
- **Read the plan skeptically.** Look for contradictions, gaps, steps that conflict with the current code, or stale assumptions (the codebase may have moved since the plan was written). Resolve minor gaps yourself (prefer the plan's intent over its letter) and note the decision for your summary. For a real conflict — a step that is impossible or would break existing behaviour — stop and use the **Mismatch Format**. A bug caught now is far cheaper than one caught mid-implementation. If anything needs human judgment (not a codebase lookup), batch all such questions into one message and ask before starting.
- **Set up working state.** Build a `TaskCreate` checklist mirroring the plan's phases, noting prerequisites. Set up the **findings file** alongside the plan — `<plan-dir>/<plan-basename>-findings.md`; do not commit it. **Create it only if absent; if it already exists (a resumed run), read it in full and append — never overwrite prior phases' blocks.** Seed it with pre-flight discoveries ("Module X already does Y — don't reimplement"). See **Findings file** for what to append per phase.

Once questions (if any) are answered and the task list and findings file exist, begin Phase 1.

## Project invariants (hot list)

The plan's Master Acceptance Checklist and the **Review Criteria** below are the full set; `CLAUDE.md` has the complete rules. As the last pre-flight act, keep this short hot list of **cheap-to-violate, expensive-to-catch-late** coder_eval invariants live while coding — these pass typecheck and silently break at runtime or in CI:

- **All models import from `coder_eval.models`** — never from submodules (lint-guarded). New models are exported from `models/__init__.py`.
- **New criterion → two edits.** The `@register_criterion` checker in `criteria/` **and** the `SuccessCriterion` discriminated union in `models/criteria.py`. Discriminated unions use `Field(discriminator="type")` — a bare `A | B` union silently coerces.
- **New agent → plugin SPI, not enum dispatch.** Register via a `register(registry)` hook exposed through the `coder_eval.plugins` entry-point group; do **not** edit `Orchestrator._create_agent` (it already delegates to the registry's `create_agent()` factory) or the `AgentKind` enum (known built-in kinds only). Use the shared turn lifecycle (`_begin_turn`/`_end_turn_ok`/`_mark_stopped`) and emit the standardized event protocol through `EventCollector`.
- **Ripple completeness.** Adding/removing/renaming a model field, config key, or CLI flag means tracing every reference — task YAMLs in `tasks/`, experiment YAMLs in `experiments/`, `experiments/default.yaml`, `.claude/commands/`, docs, and `models/__init__.py`.
- **Config merge.** New list/dict fields declare their `MergeField` strategy (CE014). New `ResolvedTask`/`AgentConfig` fields need coverage across all 5 layers and a matching `-D` override path.
- **Crash/retry hygiene.** On `AgentCrashError` / `TurnTimeoutError`, set the partial `crashed=True` TurnRecord on `pending_turn`, then raise bare; reset `_session_id`, `pending_turn`, watchdog refs, streaming `ContextVar`s, and iteration counters before the next attempt.
- **`extra="forbid"`** on config models that consume YAML/CLI; **Haiku/Sonnet, never Opus** in tests (cost).

## Reference blocks

The **Risk triggers**, **Severity rubric**, **Fix Policy**, **Harness loop**, and **Review Criteria** live in `.claude/shared/review-rubric.md` (the single source of truth — read it once at pre-flight and keep it open). The blocks below (**Mismatch Format**, **Findings file**) are specific to this command and defined inline.

### Mismatch Format

When the plan can't be followed as written, stop — do not improvise around it — and present:

```
Issue in Phase [N]:
Expected: [what the plan says]
Found: [actual situation]
Why this matters: [explanation]

How should I proceed?
```

### Findings file

After each phase, before committing, append a `## Phase N — <title>` block to the findings file: key names introduced (models, criteria, functions, constants, registry keys), any decision that deviates from the plan, and anything the next phase must respect that isn't obvious from the code. Keep it to 3–8 bullets. This is the cross-phase memory — later phases rely on it, not your context window. Maintain a running `### Harness candidates` list here for the harness loop. Delete the file only when the whole plan is complete.

### Harness loop (close-in-session)

**Defined in `.claude/shared/review-rubric.md` → "Harness loop"** (read it). It is the promote-or-defer rule for converting a review finding into a permanent CExxx lint rule (or deferring it to `docs/harness-candidates.md`).

Per phase, just note candidates in the findings file's `### Harness candidates` list — don't interrupt phase flow to build guards. The promote-or-defer pass runs once, in the final review. The final summary reports **closed vs deferred** counts.

---

## Per-phase execution

### Implement

Write the code for exactly this phase's **Changes**, mirroring the plan's **Patterns to Mirror**, following the **Fix Policy**'s test-first rule for new behaviour. Update the plan's checkboxes as you complete its steps.

### Verify — Stage 1: computational checks

Run first; nothing inferential happens until this is clean.

- Run the phase's **Tests to Run** and confirm its **Acceptance Criteria** — they're in the plan, with scoped `uv run pytest …` commands. These are scoped on purpose: do **not** run the full `make test` / full `make typecheck` per phase — those are reserved for completion and recovery.
- **Lint the phase's changed files (scoped).** Run `ruff check` (and `make lint` for the custom CExxx rules if the phase touches a guarded pattern) over only this phase's changed files. Stage 1 is the *only* automated gate on a Low-risk phase (no independent reviewer). Fix every violation before Stage 2.
- **Fix every failure before Stage 2.** If a failure is opaque, check the usual coder_eval suspects: a new model not exported from `models/__init__.py`; a new criterion missing from the `SuccessCriterion` union or the `@register_criterion` decorator; a circular import from a new model export; a missing `MergeField` strategy (CE014); a discriminated union missing `Field(discriminator=...)`.
- **Coverage check:** for each user- or behaviour-facing change, confirm a test exercises the *behaviour*, not just compilation — happy path + invalid-input-per-required-field + error path + boundary (None/empty/missing-file). Flag any untested behavioural path in the phase summary.

### Review — Stage 2: inferential, risk-scaled

Spec-compliance always runs first. Quality depth scales with phase risk (see **Risk triggers**).

**Stage 2a — Spec compliance (every phase).** Launch a sub-agent (`Agent`, `subagent_type: "general-purpose"`, strongest available model). Give it the plan path, the phase number/title, and the full list of changed file paths; tell it to read each file in full and answer only: *"Is every item in this phase's Changes / Tests to Write / Acceptance Criteria implemented — nothing missing, nothing extra? Read the actual code, not a summary."* It returns `✅ Spec compliant` or a numbered list of gaps/extras with `file:line`. Fix any gaps (per **Fix Policy**), re-run Stage 1, re-run 2a until compliant.

**Stage 2b — Quality.**
- *Low-risk phase:* you review it yourself against the **Review Criteria** below. No sub-agent.
- *High-risk phase:* launch a second sub-agent (`Agent`, strongest model) to read every changed file in full and review against the **Review Criteria**, returning issues as `severity · file:line · description` per the **Severity rubric**.

Either way: fix Critical/High/Medium per **Fix Policy**; ignore Low. Re-run the phase's **Tests to Run**. **Cycle limit:** if Critical/High issues survive 2 fix-and-review cycles on the same phase, stop and escalate via the **Mismatch Format** — a stubborn issue is usually a design question, not a coding one.

### Commit

Report a one-liner first, e.g. *"Phase 2: spec ✅ — quality 3 issues (1 High, 2 Med) — all fixed."* Append the phase's **Findings file** block, noting any harness candidate in its running `### Harness candidates` list (the final review promotes or defers it). Then commit with a conventional message and a progress counter, e.g. `feat(criteria): 2/5 — add import_check criterion`. Never commit broken or partially-verified work. Update the task list via `TaskUpdate`.

---

## Review Criteria

Used by every quality review (per-phase Stage 2b and the final review). **Defined in `.claude/shared/review-rubric.md`** — the **Review Principles** (KISS/DRY/simplicity/CLAUDE.md-adherence) plus the 18-item **Review Criteria** checklist (ripple completeness, layer-merge coverage, Pydantic round-trip integrity, discriminated unions, cross-retry state hygiene, untrusted-prompt framing, NaN guards, registry-over-dispatch, `extra="forbid"`, …). Read both sections there; do not work from memory.

---

## When you get stuck

Never repeat an approach that already failed.

1. **Diagnose** — read the relevant code, find the root cause, apply a targeted fix. Check if the codebase evolved since the plan was written; check for circular imports if adding model exports; check the `criteria/__init__.py` static fallback set for new criteria.
2. **Different approach** — if (1) failed, try a fundamentally different one; use sub-agents for parallel investigation.
3. **Question the plan** — if (2) failed, challenge the step's assumptions: is the step itself right? does something upstream need to change first? is this solved elsewhere in the codebase (mirror it)?

After 3 failed attempts, stop and escalate:

```
Stuck on Phase [N] after 3 attempts:
Attempt 1: [tried] → [result]
Attempt 2: [tried] → [result]
Attempt 3: [tried] → [result]
Root cause (best understanding): [...]
Options considered: [...]
How should I proceed?
```

**If you break the baseline mid-phase:** `git diff` to see what changed → `git stash` → run the previous phase's **Tests to Run** to confirm the baseline is clean → `git stash pop` and fix carefully. If the pop can't be reconciled, `git restore .` and redo the phase from scratch.

## Resuming

When the Pre-flight resume check (checked boxes, a matching `N/M` commit, or a findings-file phase block — not boxes alone) puts the start past Phase 1: run the most recently completed phase's **Tests to Run** (or `make verify`) to confirm a clean start, spot-check earlier steps, then resume from that phase. Re-verify earlier phases only if something looks inconsistent. Read the existing findings file (don't overwrite it; Pre-flight appends). Reconcile any **uncommitted** leftovers from an interrupted phase (`git status` → inspect): keep a trustworthy partial start, or `git restore`/remove stale half-work so you don't silently build on it. Confirm `<start-sha>` sits before the `1/M` commit so the final review still covers every phase.

## Completion

When all phases are done:

- Re-read the whole diff (`git diff <start-sha>..HEAD`) to catch anything missed; fix it if so.
- **Walk the plan's Master Acceptance Checklist** item by item, ticking each. Flag any you can't verify — don't tick on faith.
- **Run the full suite:**
  ```bash
  make format    # ruff format
  make check     # ruff check (lint)
  make typecheck # pyright
  make test      # pytest with coverage
  make lint      # custom CExxx architectural rules
  make verify    # all of the above + 80% coverage threshold
  ```
  Fix failures by root cause, not by papering over the test.
- If any phase changed a model field/config key/CLI flag, confirm every ripple reference was updated (task/experiment YAMLs, `experiments/default.yaml`, `.claude/commands/`, docs).
- Delete the findings file — after the harness loop reads it (below).
- Mark the plan complete, then run the final review.

---

## Final review

One review of the **entire** change — its job is the cross-phase bugs that per-phase reviews can't see: integration between phases, overall consistency, anything only visible with the whole diff in view.

1. **Scope it.** `git status`, then `git diff <start-sha>..HEAD` (if nothing is uncommitted, `git diff HEAD~1`). Collect the changed files and read them all, fully.

2. **Run the reviews in parallel**, per the shared procedure in `.claude/shared/multi-model-review.md` (Review A — `mcp__multi__codereview` with the strongest models, including its multi-step `step_number: 2` follow-up protocol and the Opus fallback; Review B — an Opus sub-agent). For both, `content`/rubric = the shared **Review Criteria**, and `relevant_files` = the absolute paths of every changed file collected in step 1.

3. **Synthesize.** Merge into one report; dedupe; note issues multiple reviewers flagged independently (higher confidence); discard false positives and anything that contradicts the **Review Criteria**.

   ```
   ## Code Review Summary
   ### Critical / High   (multi-reviewer findings first)
   ### Medium / Low
   ### Positive Observations
   ### Overall Assessment   (1–2 sentences)
   ```

4. **Fix Medium and above** per **Fix Policy**. Leave Low issues as informational. Re-run `make verify`. If you fixed anything, commit it separately (e.g. `fix: code review fixes for <plan-name>`) — do not amend the implementation commits.

5. **Close the harness loop.** Gather every harness gap surfaced this run: the final review's findings plus the findings file's `### Harness candidates` list (read it now — step 6 deletes the file). For each, apply the promote-or-defer decision — write the guard now if it's ≲30 min (a new CExxx rule + its test, or a unit test; commit as its own `test(...)`/`feat(...)` commit), else append it to `docs/harness-candidates.md` (commit the append). Do not let the run end with gaps silently dropped.

6. **Final summary:**

   ```
   ## Implementation of <plan name> Complete

   ### What Was Built
   - (per phase, what it delivered)

   ### Deviations from Plan
   - (where reality differed and how you adapted — or "None")

   ### Code Review Findings
   **Fixed (Critical/High/Medium):**
   - `<file>:<line>` [Severity] — <bug and fix>   (or "None")
   **Left as findings (Low):**
   - `<file>:<line>` [Low] — <description>   (or "None")

   ### Test Results
   - Unit / Typecheck / Lint / Coverage / Integration — pass counts or "clean"

   ### Harness Loop
   **Closed this run (N):**
   - `<test/lint file>` — <rule now guarded by a committed test/lint check>   (or "None")
   **Deferred to docs/harness-candidates.md (M):**
   - <rule> — <why it wasn't ≲30 min to guard>   (or "None")

   ### Suggested Next Steps
   - (follow-ups, known gaps, deferred scope, low-severity items worth revisiting)
   ```
