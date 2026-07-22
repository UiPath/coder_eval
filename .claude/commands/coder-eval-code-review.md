---
description: Run a multi-model code review on uncommitted changes or a described set of files
---

## Context

- Current git status: !`git status --short`
- Current branch: !`git branch --show-current`
- Uncommitted changes: !`git diff --name-only && git diff --name-only --cached`

## Your Task

Run a thorough code review using multiple AI models in parallel, then fix all high-confidence medium-severity and above findings.

**Cost note**: This command runs `make verify` + a multi-model review (`gemini-3` + `gpt-5` via MCP, plus an Opus sub-agent) + automatic fixes. Expensive in time and tokens. Use this for changes you're about to ship; for broad codebase audits use `/coder-eval-code-review-full`; for quick local checks, run targeted tools (`ruff`, `pyright`, `pytest`) directly.

Input: $ARGUMENTS

## Determining Scope

**If input was provided**: Interpret it as a description of what was changed. Use it to identify the relevant files — search the codebase, read recent commits, check git status — then read those files in full.

**If no input was provided**: Review all locally modified files (unstaged + staged, not yet committed). Identify them from `git diff --name-only` and `git diff --name-only --cached`. If there are no uncommitted changes, review the files changed in the most recent commit (`git diff HEAD~1 --name-only`).

Read all identified files in full before launching any review.

## Severity Standard

Tag every finding 🔴 Critical / 🟠 High / 🟡 Medium / 🔵 Low. Calibrate by
**impact if shipped**, not by fix difficulty. When torn between two levels,
pick the lower one. Use the anchors below to keep ratings reproducible across
runs and across reviewers.

| 🔴 Critical | 🟠 High | 🟡 Medium | 🔵 Low |
|------------|--------|----------|-------|
| Data loss; secrets/credentials leak; security hole reachable in production; crash on the golden path; silently-wrong `EvaluationResult` | Wrong behavior on a common path; race condition; resource leak (subprocess, tempdir, file handle); bare `except` swallowing a specific error class; logic fix landing without a regression test | Design issue (KISS/DRY/CLAUDE.md violation); Checklist hit outside a hot path; weak test that wouldn't catch the regression; missing `extra="forbid"` on a config model | Style or naming nit; missing internal docstring; redundant comment; example in `--help` is stale |

**Security findings additional requirement**: any finding whose `Trigger` is
security-class (Checklist items 5, 7, 14, anything about secrets/auth/subprocess
injection, or a `bandit` / `pip-audit` hit) MUST include a CVSS v3.1 vector
string (e.g. `CVSS:3.1/AV:L/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H`). Map base score to
severity: 9.0–10.0 → 🔴, 7.0–8.9 → 🟠, 4.0–6.9 → 🟡, 0.1–3.9 → 🔵.

**Severity is NOT**:
- a measure of fix difficulty — a one-line fix can still be 🔴.
- a vote of confidence — use the `confirmed by N/3 reviewers` note instead.
- inflated to make findings look important — when uncertain, go lower.

**Fix threshold**: Step 4 fixes everything 🟡 and above. 🔵 is reported in the
summary but not auto-fixed.

## Step 1: Automated Verification

Before reviewing, confirm the code is in a passing state:

```bash
make verify
```

If `pyproject.toml` or `uv.lock` is in the diff, also run `uv run pip-audit` to surface new dependency CVEs.

If it fails, report what's failing and stop — code review on broken code is premature. Ask the user whether to fix the failures first or proceed anyway.

## Step 2: Multi-Model Code Review

Launch two reviews **in parallel**:

**Review A — Multi-model (via `mcp__multi__codereview`)**: run it per the shared procedure in `.claude/shared/multi-model-review.md` — `models: ["gemini-3", "gpt-5"]`, `relevant_files` = absolute paths of all changed files, `content` = a review request citing the Severity Standard, Review Principles, and Checklist. **Heed the multi-step protocol described there** (the step-1 response is usually an `in_progress` checklist, not findings — you must make the `step_number: 2` follow-up call with the same `thread_id`). Falls back to two Opus sub-agents if the tool is unavailable.

**Review B — Opus sub-agent**: Use the `Agent` tool with `model: "opus"`. Give it the list of changed files and the following specialized tasks to run in parallel internally:

- **Bugs**: Read the changes and do a shallow scan for obvious bugs. Focus on large bugs; avoid nitpicks and likely false positives.
- **CLAUDE.md compliance**: Check adherence to patterns in CLAUDE.md and codebase conventions.
- **Historical context**: Read git blame (`git log -p --follow`) for the changed files to identify bugs in light of past decisions.
- **Review Criteria**: Evaluate against the full checklist below.

**Required return shape** (each reviewer, no prose preamble or trailing summary):

```
- [severity: 🔴 Critical | 🟠 High | 🟡 Medium | 🔵 Low] One-line description
  - File: `path:line`
  - Trigger: which Principle / Checklist item / Severity anchor matched
  - Recommendation: what to do (or `open question`)
  - (Security findings only) CVSS vector: `CVSS:3.1/...`
```

Positives go in a separate list at the bottom of the response, not interleaved
with findings. This shape lets Step 3 concatenate reviewer outputs with
minimal reformatting.

## Step 3: Synthesize

Combine surviving findings across all reviewers:

- Deduplicate — consolidate overlapping findings into one entry.
- Note multi-reviewer agreement as `confirmed by N/3 reviewers` (genuine confirmation here: three reviewers independently looking at the same diff from the same lens).
- On severity disagreement for the same finding, take the **highest** rating and note the disagreement.
- List positives in a separate section below findings — never mixed in.

## Step 4: Fix Medium+ Issues

Fix all critical, high, and medium severity findings that survived the confidence filter:

- **Logic/correctness bugs**: Write a unit test that fails first, fix the code, re-run the test. If not reproducible, mark as false positive.
- **Structural issues** (naming, KISS/DRY violations): Fix directly — no test required.
- **Regression lint**: For each logic/correctness bug fixed, ask: _is this pattern mechanically detectable by an AST rule?_ If yes, add a custom lint rule to `tests/lint/rules/` following the existing CE001–CExxx pattern and wire it in `tests/lint/runner.py`. Run `make lint` to confirm.

After all fixes, re-run `make verify`. Commit fixes if there are any (e.g., `fix: code review fixes`). Low-severity issues are noted but not fixed.

## Step 5: Summary

Present a structured summary. The `Review Metadata` block makes runs comparable
across sessions and provides forensic context if a fix later proves wrong:

```
## Code Review Complete

### Review Metadata
- Timestamp: <ISO timestamp from `date -u +%Y-%m-%dT%H:%M:%SZ`>
- Git SHA: <`git rev-parse HEAD`>
- Branch: <`git rev-parse --abbrev-ref HEAD`>
- Scope: <uncommitted | described "<input>" | last commit>
- Reviewers: <e.g. gemini-3, gpt-5, opus-fallback-1, opus-fallback-2 — list actual reviewers used>

### Scope
- Files reviewed: (list)

### Automated Verification
- make verify: passed / failed (details)

### Findings
- 🔴 Critical / 🟠 High fixed: (count + brief descriptions)
- 🟡 Medium fixed: (count + brief descriptions)
- 🔵 Low / informational: (count, left as findings)
- Filtered as false positives: (count)

### Suggested Next Steps
- (any 🔵 findings worth revisiting)
- (patterns flagged as candidates for new lint rules)
```

## False Positives — Do Not Flag These

- Something that looks like a bug but is not actually a bug
- Pedantic nitpicks a senior engineer wouldn't call out
- Issues that ruff, pyright, or pytest would catch — these run in CI separately
- General code quality concerns (test coverage, documentation) unless explicitly required in CLAUDE.md
- Issues called out in CLAUDE.md but explicitly suppressed with a `# noqa` or `# type: ignore` comment
- Changes in behavior that are clearly intentional given the broader context
- Real issues on lines the change did not touch

## Review Criteria

**Defined in `.claude/shared/review-rubric.md`** — read both the **Review Principles** (KISS/DRY/simplicity/CLAUDE.md-adherence) and the 18-item **Review Criteria** checklist (correctness, type safety, ripple completeness, shell safety, allowlist-over-denylist, resource cleanup, public-API surface, regression-lint candidates, layer-merge coverage, Pydantic round-trip integrity, discriminated unions, cross-retry state hygiene, untrusted-prompt framing, NaN guards, registry-over-dispatch, SDK-mock shape, `extra="forbid"`). The Severity Standard, False-Positives list, and procedure above are specific to this command and stay here. Reviewers must read the shared sections rather than work from memory; the `Trigger` field of each finding cites the Principle or Checklist item it matched.
