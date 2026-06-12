---
allowed-tools: Bash(*), Read(*), Grep(*), Glob(*), Write(tmp/code-review-*/*), Agent
description: Review the codebase across critical quality axes
---

## Context

You are performing a structured codebase review of the `coder_eval` project.

Optional argument: $ARGUMENTS — space-separated tokens that control review
scope and axis selection. See **Scope Selection** below. If empty, review the
full codebase across all 8 axes.

## Scope Selection

`$ARGUMENTS` accepts a space-separated set of tokens. Default scope is `all`.
Default axes are all 8.

**Scope tokens** (pick one):

- `all` — full codebase (walk `coder_eval/`). Default.
- `local` — uncommitted changes + untracked files. Resolve via `git status --porcelain` plus `git diff` for tracked changes; read untracked files directly.
- `staged` — only files in `git diff --staged --name-only`.
- `branch` — files changed on the current branch vs `main` (`git diff main...HEAD --name-only`).
- `unpushed` — commits on the current branch not on `origin` (`git log @{u}..HEAD --name-only --pretty=format: | sort -u`).
- `pr:<N>` — files in GitHub PR #N **at the PR's actual HEAD, not the local working tree**. The local checkout may differ from (or be unaware of) the PR's branch. Resolve as follows:
  1. `git fetch origin pull/<N>/head:pr-<N>` — creates a local ref `pr-<N>` at the PR's HEAD (works for fork PRs too via the GitHub `pull/<N>/head` refspec).
  2. `gh pr view <N> --json baseRefName -q .baseRefName` → store as `<base>`.
  3. `git fetch origin <base>:refs/remotes/origin/<base>` — ensure base is current.
  4. File list: `git diff origin/<base>...pr-<N> --name-only`.
  5. **Sub-agents read file contents via `git show pr-<N>:<path>`, NOT via Read on the working tree.** Include this instruction in every sub-agent prompt for `pr:<N>` scope.

**Axis filter** (optional, combinable with any scope):

- `axis:<comma-list>` — restrict to the listed axis numbers (e.g. `axis:4,6`).

**Action flags** (optional, combinable with any scope):

- `--post-comment` — after writing the report, post `99-pr-comment.md` to the relevant PR via `gh pr review <N> --comment --body-file <path>`. Treat this token as explicit authorization to perform the shared-state action; do not re-confirm with the user. The PR number is resolved as follows:
  1. If scope is `pr:<N>`, use that N directly.
  2. Otherwise (`branch` / `local` / `staged` / `unpushed` / `all`), interpret the flag as "post to *this* PR" — the PR associated with the current branch. Resolve via `gh pr view --json number,title,author,headRefName,baseRefName,state` (no arg = current branch). If that succeeds and the PR is `OPEN`, use its number. If `gh pr view` fails (no PR for the branch) or the PR is closed/merged, abort before running any tools with a clear error: "--post-comment requires either `pr:<N>` scope or an open PR for the current branch; got `<branch>` with no open PR" — and suggest pushing the branch + opening a PR first, or invoking with `pr:<N>` explicitly.

  When this resolves successfully for a non-`pr:<N>` scope, also stash the PR metadata (title/author/headRef/baseRef) for the PR-comment file header — same as the `pr:<N>` path — so the comment is framed as a PR review regardless of which scope drove it.

**Examples**:

- `pr:253` — review PR #253 across all 8 axes (writes the PR comment as a file; does not post).
- `pr:253 --post-comment` — review PR #253 and post the comment to GitHub.
- `branch --post-comment` — review the current branch's diff vs `main` and post to *this* branch's PR.
- `local --post-comment` — review uncommitted + untracked changes and post to this branch's PR.
- `branch axis:4` — security-only review of current branch's changes vs `main`.
- `local` — review everything uncommitted and untracked.
- (empty) — full review of the whole codebase, all axes.

**Two rules for non-`all` scopes**:

1. **Read freely beyond the diff; file findings only inside it.** Reviewing a
   change in `orchestrator.py` requires reading its callers, the types it
   touches, and the tests for it. Sub-agents treat the scoped file list as
   the *target for findings* — they may read any file in the repo for
   context, but a finding MUST be against a file in scope. This catches bugs
   that only become visible when you read the surrounding code.

2. **Some axes don't scope cleanly — adjust the question they answer.**
   Architecture (Axis 5) and Harness Quality (Axis 8) are whole-system
   properties. For non-`all` scopes, those axes answer "does this change
   make the architecture / harness *worse*?" — not "is the architecture good
   in absolute terms." File a finding only if an in-scope change introduces
   or worsens an axis-5/8 issue.

## Review Principles

All analysis must evaluate against these core principles:
- **Bug-free code**: Logic errors, edge cases, off-by-one errors, unhandled states
- **KISS**: Is the code as simple as it can be? Are there unnecessary abstractions or indirection?
- **DRY**: Is there duplicated logic that should be consolidated? But also: is DRY being over-applied (premature abstraction)?
- **Not over-engineered**: No unnecessary generalization, no speculative features, no abstractions for one-time operations, no "just in case" code
- **Simplicity**: Could a junior developer understand this? Is the intent clear?

## Severity Standard

Every finding MUST be tagged 🔴 Critical / 🟠 High / 🟡 Medium / 🔵 Low. Calibrate
by **impact if shipped**, not by fix difficulty. When torn between two levels,
pick the lower one. The anchors below define each axis's bar — match a finding
to the closest example before assigning.

### Per-axis severity anchors

| Axis | 🔴 Critical | 🟠 High | 🟡 Medium | 🔵 Low |
|------|------------|--------|----------|-------|
| **1. Code Quality** | Dead code on a live path that misleads readers into wrong assumptions (e.g. an unused branch in the orchestrator that looks load-bearing) | Cyclomatic complexity > 20 in a hot module (`orchestrator`, `checker`, `sandbox`); duplicated logic across 4+ sites | CC 10–20 outside hot modules; duplication across 2–3 sites | Naming inconsistency; long function that's still readable; missing internal docstring |
| **2. Type Safety** | Type hole that lets malformed task YAML pass `TaskDefinition` validation silently | `Any` in a public API consumed by agents/criteria; missing types on a user-facing Pydantic field | Missing return types on internal helpers in hot modules; `# type: ignore` without justification | Missing types on private helpers in cold paths |
| **3. Test Health** | No coverage for orchestrator main loop, sandbox cleanup, or a `FinalStatus` failure path | Untested public API (`run_batch`, a `BaseCriterion` subclass); flaky test masking a real race | Coverage gap on a non-critical module; missing edge cases on one criterion | Test that duplicates another; minor readability issue in a test |
| **4. Security** (CVSS v3.1) | CVSS 9.0–10.0 — shell-quoted user input passed to `subprocess` with `shell=True` in the sandbox path; secrets written to a world-readable log; auth token forwarded to an attacker-controlled URL | CVSS 7.0–8.9 — path traversal in template loading reachable only with a crafted task YAML; `pip-audit` finding with a known exploit in a runtime dependency | CVSS 4.0–6.9 — bandit B603 on a subprocess call whose arguments come from internal config but aren't shell-escaped; outdated dep with a vulnerability not reachable from our call sites | CVSS 0.1–3.9 — missing `# nosec` justification on an audited call; hardcoded localhost URL that should be configurable |
| **5. Architecture** | Coupling that breaks an extension point claimed in CLAUDE.md (e.g. orchestrator hardcodes Claude Code assumptions, blocking new `Agent` subclasses) | God class > 500 lines mixing 3+ concerns in a hot module; circular import worked around with local `import` | DRY violation across `criteria/` or `models/`; pattern drift between similar checkers | Module that could be split for cohesion but isn't painful today |
| **6. Error Handling** | Resource leak that compounds across runs (subprocess, tempdir, file handle); failure path that returns a successful `EvaluationResult` with wrong data | Bare `except:` or `except Exception:` swallowing a specific error class; missing cleanup on a rare-but-reachable path; retry that double-counts tokens/cost | Inconsistent error context (some paths wrap, some don't); error categorization gap in `errors/categories.py` | Error message could name the offending field |
| **7. API Surface** | Silently-wrong CLI default (e.g. `--max-turns` ignored when also set in YAML) producing incorrect evaluation results | Breaking change to a documented CLI flag without alias; required flag with no sensible default and no clear error when omitted | Too many overlapping knobs for one feature; flag name doesn't match its YAML key | Help text could be clearer; example in `--help` is stale |
| **8. Harness Quality** | Non-determinism in scoring: same task + same agent output produces different `score` across runs | Criterion with ambiguous pass/fail (judge prompt drift; threshold semantics unclear); `Agent` ABC assumption that ties orchestrator to Claude Code | Awkward YAML schema for a common case; unhelpful error when task definition is malformed; reports lack a field needed for cross-run comparison | Convenience missing (e.g. no `--dry-run`); minor UX paper cut |

**Axis 4 (Security) additional requirement**: every security finding MUST
include the CVSS v3.1 vector string (e.g.
`CVSS:3.1/AV:L/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H`) so the severity rating is
reproducible. The base score determined by that vector must match the column
the finding is filed under.

**Severity is NOT**:
- a measure of fix difficulty (a one-line fix can still be 🔴)
- a vote of confidence — use the `Cross-axis: flagged by axes <list>` note for that
- comparable across axes (a 🟠 in Test Health and a 🟠 in Architecture are both High, not "equally bad")

When sub-agents disagree on severity for the same finding, synthesis takes the
**highest** rating and records the disagreement in the finding's notes.

## Scoring

Each axis gets a numeric score 0–10, derived **deterministically** from its
finding counts. Same findings → same score, every run. There is no
"give it a feel" component.

**Per-axis formula**:

```
score = max(0, 10 − 2.0·🔴 − 0.7·🟠 − 0.2·🟡 − 0.05·🔵)
```

Round to one decimal place.

**Weight rationale** (so future edits don't unwittingly recalibrate):
- One 🔴 alone → 8.0. A single critical demands action but doesn't tank the axis.
- Two 🔴 → 6.0; five 🔴 → 0.
- One 🟠 → 9.3; ten 🟠 → 3.0. Sustained High noise matters.
- 10 🔵 → 9.5. Pure cosmetic noise stays healthy.

**Cross-axis aggregates** (in the Summary section):
- **Overall Score** = mean of per-axis scores across reviewed axes, rounded to one decimal.
- **Weakest Axis** = the axis with the lowest score, named explicitly. Prevents a single bad axis from hiding behind averaging.

The score is a pure function of the finding counts produced under the Severity
Standard. Do not adjust by feel. If a score looks wrong, the fix is to
re-examine severity tags against the anchor table — not to nudge the number.

## Critical Axes

Review the codebase across these **8 critical axes**, producing a structured report with findings, severity, and actionable recommendations for each.

### Axis 1: Code Quality & Style
- Run `uv run ruff check coder_eval/` and `uv run ruff format --check coder_eval/`
- Look for: dead code, unused imports, overly complex functions, naming inconsistencies
- Check cyclomatic complexity of key modules (orchestrator, checker, agent)

### Axis 2: Type Safety
- Run `uv run pyright`
- Look for: missing type annotations on public APIs, `Any` escape hatches, inconsistent return types
- Check that Pydantic models have proper field types and validators

### Axis 3: Test Health
- Run `uv run pytest --co -q` to list all tests, then `uv run pytest --tb=short -q` for results
- Evaluate: coverage gaps, test isolation, missing edge cases, flaky test patterns
- Identify untested public APIs or critical paths (orchestrator loop, error handling, criteria checkers)

### Axis 4: Security
- Run `uv run bandit -r coder_eval/ -ll` for security scanning
- Run `uv run pip-audit` for dependency vulnerabilities
- Look for: command injection in sandbox/subprocess calls, path traversal, secrets in code, unsafe deserialization

### Axis 5: Architecture & Design
- Evaluate separation of concerns, coupling between modules, cohesion within modules
- Check for: circular imports, god classes, leaky abstractions, violated design patterns from CLAUDE.md
- Assess plugin system extensibility (criteria registry, agent ABC)
- Review the models/ package for DRY violations and schema consistency

### Axis 6: Error Handling & Resilience
- Review the errors/ package for completeness and consistency
- Check for: bare excepts, swallowed exceptions, missing error context, retry logic correctness
- Evaluate graceful degradation in orchestrator, sandbox cleanup, agent lifecycle
- Look for resource leaks (file handles, subprocesses, temp directories)

### Axis 7: API Surface & Maintainability
- Review public APIs for clarity, consistency, and documentation
- Check CLI commands for user-facing correctness (help text, error messages, defaults)
- Evaluate configuration surface (too many knobs? unclear defaults?)
- Look for breaking-change risks or technical debt

### Axis 8: Evaluation Harness Quality
- **Ease of use**: How easy is it to define a new task, run an evaluation, and interpret results? Is the YAML schema intuitive? Are error messages helpful when a task definition is malformed?
- **Task applicability**: Do the 10 success criteria cover real-world coding agent scenarios? Are there gaps (e.g., multi-file changes, refactoring quality, performance benchmarks)? Is the weighting/threshold system flexible enough?
- **Reproducibility**: Can the same task produce consistent results across runs? Evaluate sandbox isolation, snapshot reliability, deterministic scoring, and seed/config pinning
- **Agent extensibility**: How easy is it to add a new agent beyond Claude Code? Is the Agent ABC practical or over-constrained? Does the orchestrator make assumptions tied to a specific agent?
- **Evaluation fairness**: Are criteria well-defined enough to avoid ambiguous pass/fail? Is the llm_judge prompt robust or susceptible to drift?
- **Benchmarking utility**: Can results be meaningfully compared across agents, models, or runs? Are reports structured for aggregation and trend analysis?

## Output Format

This is the shape every sub-agent returns and the shape the final report
takes. Defining it before the Procedure so step 5 has something concrete to
point at.

**Per-axis section** (one per reviewed axis):

```
### Axis N: <Name>
**Score**: N.N / 10
**Counts**: 🔴 N · 🟠 N · 🟡 N · 🔵 N

**Automated Results**: <tool output summary>

**Findings**:
1. [severity: 🔴 Critical | 🟠 High | 🟡 Medium | 🔵 Low] Finding description
   - File(s): path:line
   - Recommendation: what to do (or `open question`)
   - (When applicable) Cross-axis: flagged by axes <comma list>
   - (Axis 4 only) CVSS vector: e.g. `CVSS:3.1/AV:L/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H`
```

The Score line is computed from Counts via the Scoring formula — do not write
in a value that doesn't match the formula.

**Summary block** (appended once, at the end of the assembled report):

```
## Summary
| Axis | Score | 🔴 | 🟠 | 🟡 | 🔵 | Top Issue |
|------|-------|----|----|----|----|-----------|
| ...  | ../10 | .. | .. | .. | .. | ...       |

**Overall Score**: N.N / 10 (mean of reviewed axes)
**Weakest Axis**: <Name> at N.N / 10
**Totals**: 🔴 N · 🟠 N · 🟡 N · 🔵 N across all reviewed axes.

**Top 5 Priority Actions**:
1. ...
2. ...
3. ...
4. ...
5. ...
```

## Procedure

1. **Resolve scope and capture review metadata**: Parse `$ARGUMENTS` per the
   Scope Selection rules. Produce:
   - **In-scope file list**: paths the findings will be filed against.
   - **Selected axes**: from `axis:<list>` if present, else all 8.
   - **Action flags**: detect `--post-comment`. When present, resolve the target PR number now (before any expensive work):
     - If scope is `pr:<N>`, target is N.
     - Otherwise, run `gh pr view --json number,title,author,headRefName,baseRefName,state` against the current branch. If it returns an open PR, target is that PR's `number`. If `gh pr view` errors (no PR for the branch) or the PR is `CLOSED`/`MERGED`, abort with a clear error: `--post-comment requires either pr:<N> scope or an open PR for the current branch; got <branch> with no open PR — push the branch and open a PR, or invoke with pr:<N> explicitly.` Do **not** run any tools or sub-agents before this check passes.
     - Stash the resolved PR number and the `gh pr view` metadata blob alongside the rest of step-1 metadata.
   - **Scope summary string**: e.g. `pr:253 (12 files) axis:1,2,3,4,5,6,7,8`.
   - **Reproducibility metadata**: `git rev-parse HEAD` (SHA), `git rev-parse --abbrev-ref HEAD` (branch), current ISO timestamp, model identifier (the model running this command). For `pr:<N>` scope additionally capture `gh pr view <N> --json title,author,headRefName,baseRefName,state` so the PR comment header can name the PR. If `--post-comment` resolved a current-branch PR in the previous bullet, reuse that metadata (don't re-fetch). Stash all of this for the summary and PR-comment files.

2. **Prepare the checkout for automated checks**: The automated tools must run against the *exact* state being reviewed, otherwise their output (especially pyright/bandit) is misleading.

   - **`pr:<N>` scope** (or non-PR scope with `--post-comment` that resolved to a current-branch PR whose HEAD differs from the working tree): create a worktree at PR HEAD and install dev deps. Concretely:
     ```
     git worktree add /tmp/pr-<N>-worktree pr-<N>
     (cd /tmp/pr-<N>-worktree && uv sync --extra dev)
     ```
     The `uv sync --extra dev` step is required — without it `pyright`/`bandit`/`pip-audit` are not on PATH inside `uv run`. Stash the worktree path; all automated tools run inside it.
   - **Non-PR scopes** (`all`, `branch`, `local`, `staged`, `unpushed` without `--post-comment`-resolved PR): run automated tools against the working tree at `/Users/religa/src/coder_eval`. No worktree needed; assume dev deps are already installed (the user runs `make verify` locally).

   Stash the resolved "run-tools-here" path for step 2b.

2b. **Automated checks**: Run the automated tools (ruff, pyright, pytest, bandit, pip-audit) in parallel inside the path resolved in step 2. Tools always scan the full tree; sub-agents are responsible for restricting findings to in-scope files. Notes:
   - `pytest` configuration includes `-n auto` (xdist). If `pytest-xdist` is missing, fall back to `uv run pytest -p no:xdist --tb=line -q`.
   - For `pr:<N>` scope, sub-agents in step 4 are told to read file contents via `git show pr-<N>:<path>` from the *main* repo (not the worktree) — the worktree exists only so that the automated tools see the PR state. Sub-agent Read calls into the worktree path are also acceptable (same contents).

3. **Discover packages once**: Run `ls coder_eval/` to enumerate top-level packages (and `ls tests/` if Axis 3 is in scope). Stash this list — it goes into every sub-agent prompt in step 4. Doing this here, in the main agent, avoids 8× duplicated `ls` work and ensures every sub-agent sees the same authoritative package list (so a new package like `simulation/` or `streaming/` can't be invisible to one axis but not another).

4. **Parallel sub-agent reviews**: Launch one `Agent` sub-agent per *selected* axis (up to 8) in parallel.

   **Each sub-agent prompt MUST include verbatim**:
   - The full **Review Principles** section.
   - The full **Severity Standard** section, including the per-axis anchor table. Pass the whole table — not just the agent's row — so it can calibrate against neighboring axes.
   - The full **Output Format** axis template (above), as the required return shape.
   - The **Scope Selection** scope spec, the in-scope file list, and the two non-`all` rules (read freely, file findings only inside scope). For `pr:<N>` scope, include the rule that file contents come from `git show pr-<N>:<path>`, not the working tree.
   - The package list discovered in step 3.
   - The relevant automated tool output for this axis (e.g. pyright to Agent 2; bandit + pip-audit to Agent 4).
   - The axis-specific starting point below.
   - The full **Techniques to apply** block below — paste it verbatim into each sub-agent prompt. These are concrete bug-shape checks that catch defects a "read the diff and look for issues" pass misses; the block names the three patterns most relevant to a coder_eval change.

   **Techniques to apply** (paste verbatim into every sub-agent prompt):

   ```
   ## Techniques to apply

   In addition to reading the in-scope files end-to-end, run these three cross-file consistency checks. They catch defect classes that "read the diff and look for problems" misses, and they're cheap because they're grep-shaped.

   1. **Grep for the old pattern on any rename or removal.**
      If the change renames a symbol, file, field, or YAML key — OR removes one — search `src/` and `tests/` for the old name and flag any usage that wasn't updated. Examples: a Pydantic field renamed in `models/sandbox.py` but still referenced by string key in `reports*.py`; a function removed from `orchestrator.py` but still imported in `agents/`; a CLI flag renamed but the old name still appears in `docs/` or task YAMLs. Use `grep -rn` from the repo root, scoped to `src/coder_eval/ tests/ docs/ tasks/` as appropriate. Even a "trivial" rename routinely leaves 1–2 stragglers, so this is the highest-yield single check.

   2. **Compare parallel models / parallel code paths.**
      coder_eval has several pairs of structures that *must* stay in sync. When one is changed, check the other:
        - Models with the same field across types (e.g. `RunSummary` and `VariantAggregate`, `TaskDefinition` and `ResolvedTask`, `EvaluationResult` and the per-row `CriterionResult`): verify type, default, validator, and field description match.
        - Parallel orchestration code paths: `orchestration/batch.py` ↔ `orchestration/experiment.py`. A bug fixed in one routinely needs to be fixed in the other (precedent in this codebase: dataset fan-out, run_limits merging, lineage tracking).
        - Parallel agent paths: `Orchestrator` ↔ any new driver (e.g. `isolation/docker_runner.py`) — does the driver preserve the `pending_turn` / `crashed=True TurnRecord` contract documented in CLAUDE.md?
        - Parallel renderers: `reports.py` ↔ `reports_experiment.py` ↔ `reports_html.py` ↔ `reports_stats.py` — if a new field is added to `EvaluationResult`, do all four render it (and if not, is that deliberate)?
      Flag any divergence as a finding even if the unchanged side is technically still correct in isolation — the divergence itself is the bug, and silent drift between parallel paths is one of the most expensive defects to debug later.

   3. **Check exhaustiveness when an enum / Literal / status set changes.**
      When the change adds, renames, or removes a value of a discriminated union, `Literal`, `StrEnum`, or any closed set (`AgentKind`, `AgentState`, `FinalStatus`, `SnapshotMode`, `ApiBackend`, `SuccessCriterion` discriminator, criterion `type` keys, `ErrorCategory`, etc.), verify that **every** consumer handles the new universe:
        - `dict` lookups keyed on the enum value — does the dict have an entry for the new value, or does it fall through to a generic default like `"?"` or `"unknown"`?
        - `if`/`elif` and `match` chains — is there a final `else` that silently swallows the new value, or does it raise / log?
        - Display / icon / classification mappings in `reports*.py` — does the new value have an icon, a label, a colour, a column?
        - Counting / classification formulas — if a new `FinalStatus.X` is added, do all places computing `pass_rate`/`failure_rate`/`category == "failed"` correctly classify it?
        - JSON Schema / JSON discriminator: if a new criterion type is added, is its model included in the `SuccessCriterion` union *and* re-exported from `coder_eval.models`?
      Fallthrough to a generic default isn't always wrong, but it should be a deliberate choice — flag it so the reviewer can confirm.

   Apply these techniques while reading. Findings produced this way go into the same output as ordinary findings, tagged with the appropriate axis and severity.
   ```

   **Each sub-agent returns** its axis section in the exact **per-axis Output Format defined above** — `### Axis N: <Name>` heading, Score line, Counts line, Automated Results line, Findings list — as raw markdown. The main agent concatenates these sections into the report with minimal reshuffling; sub-agents that return a different shape force the main agent to reformat, wasting tokens and risking drift.

   **Axis starting points** — these are *entry points, not exhaustive lists*. Sub-agents follow imports and reconcile against the package list from step 3 so newly added top-level packages aren't invisible.

   - **Agent 1 — Code Quality & Style**: Start at `orchestrator.py`, `sandbox.py`, `agent.py`, `evaluation/checker.py`. Cross-reference with ruff output.
   - **Agent 2 — Type Safety**: Start at the same hot modules and `models/`. Cross-reference with pyright output.
   - **Agent 3 — Test Health**: Start at `tests/`. Cross-reference with pytest output.
   - **Agent 4 — Security**: Start at `sandbox.py`, `agents/`, `evaluation/`, and any subprocess/command execution code. Cross-reference with bandit and pip-audit output.
   - **Agent 5 — Architecture & Design**: Start at `models/`, `criteria/`, `orchestrator.py`, `agent.py`, `agents/`. Then walk the rest of `coder_eval/`.
   - **Agent 6 — Error Handling & Resilience**: Start at `errors/`, `orchestrator.py`, `sandbox.py`, `agents/`.
   - **Agent 7 — API Surface & Maintainability**: Start at `cli/`, `config.py`, and public module APIs.
   - **Agent 8 — Evaluation Harness Quality**: Start at `orchestrator.py`, `sandbox.py`, `evaluation/`, `criteria/`, `models/tasks.py`, `models/criteria.py`, task YAMLs in `tasks/`, and `docs/TASK_DEFINITION_GUIDE.md`.

5. **Synthesize results**: Assemble the report in the **Output Format defined above** by concatenating each sub-agent's axis section, then writing the Summary block:
   - **Recompute every axis score from its `Counts` line using the published formula** (`max(0, 10 − 2.0·🔴 − 0.7·🟠 − 0.2·🟡 − 0.05·🔵)`, rounded to one decimal). If a sub-agent's reported score doesn't match, replace it with the formula result — sub-agent arithmetic is unreliable (observed in practice: agents off by 0.1–0.5 on multiple axes in a single run). The Counts are the source of truth; the Score is a pure function of them. Do the same for the Overall Score (mean across axes) and Weakest Axis after the per-axis fixups.
   - Deduplicate findings: if the same `file:line` appears in two sub-agents' outputs with the same root cause, merge them into one entry. **Re-decrement the Counts and re-run the score formula** on the affected axis after a merge — a finding folded into another shouldn't keep contributing to the count.
   - When two or more axes flag the same `file:line` (different lenses converging on one location), add a `Cross-axis: flagged by axes <list>` note to the merged finding. This is a stronger signal than any single axis — a systemic issue visible through multiple lenses. Do not use a "confirmed by N/8" framing: sub-agents are siloed per axis, so "confirmation" isn't what's happening; cross-axis convergence is.
   - Only discard a finding if it recommends adding speculative complexity *with no concrete bug behind it*. A real bug stays even if the recommended fix is debatable — record the bug, mark the recommendation as `open question`.
   - On severity disagreement for the same finding, apply the Severity Standard rule: take the highest rating, note the disagreement.
   - **Derive the "What's Missing" list**: after assembling the per-axis findings, do a dedicated synthesis pass that asks *what should have been changed but wasn't?* This is orthogonal to the 8 axes — diff-only reviews routinely miss it. Walk the merged findings and the in-scope file list and call out items in these four buckets:
     - **Parallel code paths not updated**: e.g. `orchestration/batch.py` was changed but `orchestration/experiment.py` wasn't, despite handling the same concept; a fix landed in `Orchestrator` but the parallel `DockerRunner` path was missed.
     - **Missing tests for new code paths**: every new branch, public function, validator, CLI flag, and report row needs a test. If a report row was added, is there a test asserting non-zero values for it?
     - **Downstream consumers of changed counting / classification / formula logic**: if a counting formula changed in one place, did all places that compute rates / averages / percentages / pass/fail status from those counts also update? Same for any threshold or scoring change.
     - **Display / icon / mapping dicts not extended for new enum values**: if a new `FinalStatus` / `AgentState` / `SnapshotMode` / criterion type / category was added, do all rendering dicts in `reports*.py` cover it, or do they fall through to `"?"` / `"unknown"`?

     This pass is allowed to surface items that are not tied to a single file:line (since the whole point is that the *absence* of a change isn't anchored anywhere). Express each as a short bullet, prefixed with the bucket it falls into, and reference the *changed* file that triggered the expectation. Each bullet should also carry a severity tag (🔴 / 🟠 / 🟡 / 🔵) using the same anchor table — a missing test for a new public function is 🟠 Test Health; a missing entry in a display dict is typically 🟡; a missing parallel-path update that introduces a real divergence is 🟠 Architecture. **Add these severity tags to the per-axis totals** so they show up in Counts and on-screen output. If there's nothing missing, write a single line: "Nothing identified."

6. **Write report files** (to disk — the report itself was assembled in step 5): Generate a timestamped output directory so successive reviews don't overwrite each other:

   - Run `date +%y%m%d-%H%M` to get the current timestamp (e.g. `260513-1742`).
   - Create `tmp/code-review-<timestamp>/` (e.g. `tmp/code-review-260513-1742/`).
   - Write the following files into that directory (referred to below as `<dir>`):

   - `<dir>/00-summary.md` — must start with a **Review Metadata** block (from step 1's stash) so reviews are comparable across runs:
     ```
     ## Review Metadata
     - Timestamp: <ISO timestamp>
     - Git SHA: <full SHA>
     - Branch: <branch>
     - Scope: <scope spec, e.g. `pr:253` or `branch` or `all`>
     - In-scope files: <count> (full list in body if non-`all`)
     - Axes reviewed: <comma list>
     - Model: <model identifier running this command>
     ```
     Then: Summary table, Critical & High Issues section (🔴/🟠 only, sorted by severity then axis; security findings include CVSS vector), **What's Missing** section (the synthesis-pass output from step 5, grouped by bucket; or "Nothing identified."), Top 5 Priority Actions.
   - `<dir>/01-code-quality.md` — Axis 1 full report (omit if Axis 1 not in scope)
   - `<dir>/02-type-safety.md` — Axis 2 full report (omit if Axis 2 not in scope)
   - `<dir>/03-test-health.md` — Axis 3 full report (omit if Axis 3 not in scope)
   - `<dir>/04-security.md` — Axis 4 full report (omit if Axis 4 not in scope)
   - `<dir>/05-architecture.md` — Axis 5 full report (omit if Axis 5 not in scope)
   - `<dir>/06-error-handling.md` — Axis 6 full report (omit if Axis 6 not in scope)
   - `<dir>/07-api-surface.md` — Axis 7 full report (omit if Axis 7 not in scope)
   - `<dir>/08-harness-quality.md` — Axis 8 full report (omit if Axis 8 not in scope)

   After writing, print the resolved `<dir>` path so the user can find the report.

7. **Print the executive summary to the screen**: Files are easy to miss in a long terminal. Emit the executive summary inline as your final user-facing message so the user can read the verdict without opening anything. This is **in addition to** the files, not a replacement — keep the files complete.

   The on-screen summary MUST include, in this order:
   - One-sentence verdict (e.g. "Solid feature work, but blocked on test coverage and one Linux-only privilege escalation.").
   - The Summary table (axes × scores × counts × top issue) — same shape as `00-summary.md`.
   - `Overall Score`, `Weakest Axis`, and the `Totals` line.
   - The full **Critical & High Issues** list (🔴 / 🟠 only) — one line per finding in the form `[Axis N] <description> (file:line)`. Security findings include the CVSS vector.
   - The **What's Missing** list (grouped by bucket: Parallel paths / Tests / Downstream consumers / Display & mapping dicts). Tight one-line bullets. If "Nothing identified.", say that and skip the heading.
   - The **Top 5 Priority Actions** in full prose.
   - A trailing `Full report: <dir>` line pointing at the path printed in step 6.

   Do NOT print the 🟡 / 🔵 findings inline — those live in the per-axis files. The on-screen view is the executive summary; the files are the source of truth.

   Cap the on-screen summary at roughly 80 lines. If Critical & High issues exceed ~12, switch to grouped bullets (`[Axes 4, 6] Container/subprocess leak + no wall-clock cap — see 04, 06`) and direct the reader to the files.

8. **Always draft a PR-review-style comment**: Write `<dir>/99-pr-comment.md` regardless of scope. The file structure is identical in every case; only the header framing changes:
   - **`pr:<N>` scope**, OR **`--post-comment` resolved an open PR on the current branch**: header names the PR (title, author, headRef → baseRef, state). This is the author-facing comment.
   - **Other scopes with no PR**: header lists scope/branch/SHA/timestamp. The file is then a review-prep document the user can repurpose into a self-review, a hand-off note, or a draft PR description.

   `99-pr-comment.md` must be detailed enough to stand on its own (the reader should not need to open per-axis files for the headline view), but tighter than 00-summary.md (no per-axis Findings dumps — those stay in `01-*.md` … `08-*.md`). Required sections, in order:

   ```
   # Review: <PR title or scope-appropriate header>

   <Header sub-line: PR #N by @author · <headRef> → <baseRef> · state · reviewed against <short-SHA>
   — for pr:<N> scopes (and for non-PR scopes where --post-comment resolved to the current branch's PR).
   For other scopes: Scope · branch · <short-SHA> · timestamp.
   Keep to one line. The <short-SHA> is the first 7 chars of the SHA reviewed (PR HEAD for PR scopes;
   `git rev-parse HEAD` for non-PR scopes) — pinning the SHA prevents confusion after fixup pushes
   force-update the PR head, and lets a re-review against a later commit cite "previous review
   against <old-SHA>".>

   <2–4 sentence verdict — what's working, what's blocking, what's optional.
   Acknowledge concrete strengths before going into issues; reviewers who
   open with criticism alone read as adversarial. End with the overall
   score and the weakest axis, so the reader knows the bottom line before
   scrolling.>

   ## Summary

   <The same Summary table from 00-summary.md (axis | score | counts | top issue),
   verbatim. Reviewers expect a glanceable table at the top of a long comment.>

   **Overall Score**: N.N / 10 · **Weakest Axis**: <Name> at N.N / 10
   **Totals**: 🔴 N · 🟠 N · 🟡 N · 🔵 N across <axes> reviewed.

   ## Blockers

   <Every 🔴 finding, plus every 🟠 finding except ones that are clearly
   "polish, not pre-merge" (e.g. a missing --help example would be 🟡 not 🟠).
   Each entry: a numbered 2–3 sentence narrative with the file:line, the
   concrete fix, and (for Axis 4) the CVSS vector. Avoid bare bullet lists —
   PR readers skim narratives better than tables. If there are zero blockers,
   write "No blockers found." and move on — don't manufacture one.>

   ## Non-blocking, but please consider before merge

   <All 🟡 findings, plus any 🟠 demoted from Blockers. Group related items
   under short sub-bullets when there are 5+ findings (e.g. all schema/typing
   items together; all docs items together). Each item: one tight sentence
   with file:line and a one-line fix. Be exhaustive — this is the "log of
   things to think about" section.>

   ## Nits

   <Every 🔵 finding worth surfacing. Single-line bullets, file:line, one-line
   fix. Skip findings that recommend speculative complexity without a concrete
   bug — those belong in follow-up issues, not in a PR comment.>

   ## What's Missing

   <The "What's Missing" synthesis-pass output from step 5. Group by bucket:
   **Parallel paths**, **Tests**, **Downstream consumers**, **Display & mapping dicts**.
   Tight one-line bullets, each prefixed with its severity tag and the file
   that triggered the expectation. This section is gold for PR comments —
   it's the kind of "you also need to update X" feedback that authors find
   most actionable. If "Nothing identified.", write that and omit the section
   entirely (don't surface an empty heading).>

   ## Top 5 Priority Actions

   <The same Top 5 Priority Actions from 00-summary.md, verbatim. Reviewers
   appreciate "if you only do three things, do these" framing at the end of
   a long comment.>

   ---

   **Stats:** N 🔴 · N 🟠 · N 🟡 · N 🔵 across <N> axes reviewed.
   Full per-axis breakdown: `<dir>/01-code-quality.md` … `<dir>/08-harness-quality.md`.
   ```

   Tone rules:
   - **Be constructive, not adversarial.** Lead with what works. Frame findings as "consider X" / "this would benefit from Y" rather than "this is wrong".
   - **Drop the severity emoji and `[severity: 🔴 Critical]` tag.** The section heading (`Blockers` / `Non-blocking` / `Nits`) carries the severity signal in language reviewers expect.
   - **Keep file:line references.** They're the load-bearing detail that makes a comment actionable.
   - **Drop the "Cross-axis: flagged by axes …" notes** — that's review-process language, not author-facing language.
   - **Don't trim findings to fit a length target.** Be exhaustive within the Blockers / Non-blocking / Nits sections; the entry filter is *relevance and actionability*, not word count. If the file ends up longer than 00-summary.md, that's fine.

   After writing the file:
   - Print `PR comment draft: <dir>/99-pr-comment.md` on a dedicated line.
   - **If `--post-comment` was specified**: run `gh pr review <N> --comment --body-file <dir>/99-pr-comment.md` against the PR number resolved in step 1 (either the explicit `pr:<N>` or the current branch's open PR). The user authorized this via the flag; do not re-confirm. On success, print the PR URL returned by `gh`. On failure, print the error and the path so the user can post manually.
   - **Otherwise, no `--post-comment`**:
     - If scope is `pr:<N>`, or the current branch has an open PR (cheap check: `gh pr view --json number,url -q '.number,.url' 2>/dev/null`), suggest `gh pr review <N> --comment --body-file <dir>/99-pr-comment.md` as the one-liner to post once the user is happy with it. Don't run it.
     - Otherwise (no PR exists), no posting suggestion — the file is for the user's own use.

9. **Cleanup**: If step 2 created a worktree (i.e. for `pr:<N>` scope or a `--post-comment`-resolved PR), remove it now:
   ```
   git worktree remove --force /tmp/pr-<N>-worktree
   git branch -D pr-<N>
   ```
   Skip for non-PR scopes (nothing was created). Cleanup happens *after* the report is written and (optionally) posted — never bail out earlier or the worktree leaks across sessions and the next review's `git worktree add` fails with "already exists".
