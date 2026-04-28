---
allowed-tools: Bash(uv run ruff check:*), Bash(uv run ruff format:*), Bash(uv run pyright:*), Bash(uv run pytest:*), Bash(uv run pip-audit:*), Bash(uv run bandit:*), Bash(wc:*), Bash(git log:*), Bash(git diff:*), Bash(git status:*), Read(*), Grep(*), Glob(*), Write(tmp/code-review/*), Agent
description: Review the codebase across critical quality axes
---

## Context

You are performing a comprehensive codebase review of the `coder_eval` project. Optional focus argument: $ARGUMENTS (if empty, review all axes).

## Review Principles

All analysis must evaluate against these core principles:
- **Bug-free code**: Logic errors, edge cases, off-by-one errors, unhandled states
- **KISS**: Is the code as simple as it can be? Are there unnecessary abstractions or indirection?
- **DRY**: Is there duplicated logic that should be consolidated? But also: is DRY being over-applied (premature abstraction)?
- **Not over-engineered**: No unnecessary generalization, no speculative features, no abstractions for one-time operations, no "just in case" code
- **Simplicity**: Could a junior developer understand this? Is the intent clear?

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

## Procedure

1. **Automated checks first**: Run the automated tools (ruff, pyright, pytest, bandit, pip-audit) in parallel to gather objective data.

2. **Manual inspection**: Use Grep and Read to investigate key modules for each axis. Focus on the most critical/complex files: `orchestrator.py`, `sandbox.py`, `agent.py`, `evaluation/checker.py`, `errors/`.

3. **Parallel Opus sub-agent reviews**: Launch **8 `Agent` sub-agents in parallel**, one per axis. Each agent receives the Review Principles, its axis criteria, and relevant automated tool output. Each must return a structured review with: issues found (severity + location + description), positive observations, and a score out of 10.

   - **Agent 1 — Code Quality & Style**: Read key modules (`orchestrator.py`, `sandbox.py`, `agent.py`, `evaluation/checker.py`). Cross-reference with ruff output. Look for dead code, complexity, naming issues.
   - **Agent 2 — Type Safety**: Read key modules and `models/` package. Cross-reference with pyright output. Look for missing annotations, `Any` escapes, Pydantic field types.
   - **Agent 3 — Test Health**: Read `tests/` directory. Cross-reference with pytest output. Evaluate coverage gaps, test isolation, missing edge cases, untested critical paths.
   - **Agent 4 — Security**: Read `sandbox.py`, `agents/`, `evaluation/`, any subprocess/command execution code. Cross-reference with bandit and pip-audit output. Look for injection, path traversal, secrets.
   - **Agent 5 — Architecture & Design**: Read `models/`, `criteria/`, `orchestrator.py`, `agent.py`, `agents/`. Evaluate coupling, cohesion, circular imports, plugin extensibility, DRY violations.
   - **Agent 6 — Error Handling & Resilience**: Read `errors/`, `orchestrator.py`, `sandbox.py`, `agents/`. Look for bare excepts, swallowed exceptions, resource leaks, retry correctness, cleanup.
   - **Agent 7 — API Surface & Maintainability**: Read `cli/`, `config.py`, public module APIs. Evaluate CLI UX, configuration surface, breaking-change risks, technical debt.
   - **Agent 8 — Evaluation Harness Quality**: Read `orchestrator.py`, `sandbox.py`, `evaluation/`, `criteria/`, `models/tasks.py`, `models/criteria.py`, task YAML files in `tasks/`, and `docs/TASK_DEFINITION_GUIDE.md`. Evaluate ease of use, task applicability, reproducibility, agent extensibility, evaluation fairness, and benchmarking utility.

4. **Synthesize results**: Combine findings from automated tools, manual inspection, and all sub-agent reviews into a single report:
   - Deduplicate overlapping findings across agents
   - Note when multiple agents independently flagged the same issue (higher confidence)
   - Discard false positives or suggestions that contradict the Review Principles (e.g., suggestions to add unnecessary abstractions)

5. **Fix confirmed issues**: For each issue rated medium severity or above:

   a. **Write a test first**: Create a unit test that reproduces the bug. Run it to confirm it fails — this proves the bug is real.

   b. **If the test passes** (bug is not reproducible): Mark the issue as a false positive in the summary and move on. Do not change production code.

   c. **If the test fails** (bug is confirmed): Fix the bug in the production code, then re-run the test to confirm it passes.

   d. After all fixes, run the full test suite to ensure nothing is broken.

   Low-severity issues should be left as informational findings in the report — do not fix them automatically.

## Output Format

For each axis, produce:

```
### Axis N: <Name>
**Health: 🟢 Good | 🟡 Needs Attention | 🔴 Critical**

**Automated Results**: <tool output summary>

**Findings**:
1. [severity: high/medium/low] Finding description
   - File(s): path:line
   - Recommendation: what to do

**Score**: X/10
```

End with:

```
## Summary
| Axis | Health | Score | Top Issue |
|------|--------|-------|-----------|
| ...  | ...    | .../10| ...       |

**Overall Score**: X/80
**Top 3 Priority Actions**:
1. ...
2. ...
3. ...
```

6. **Write report files**: After producing the report, create `tmp/code-review/` directory and write:

   - `tmp/code-review/00-summary.md` — Summary table, Critical & High Issues section (with axis, severity, description, file(s), recommendation), Top 3 Priority Actions, timestamp
   - `tmp/code-review/01-code-quality.md` — Axis 1 full report
   - `tmp/code-review/02-type-safety.md` — Axis 2 full report
   - `tmp/code-review/03-test-health.md` — Axis 3 full report
   - `tmp/code-review/04-security.md` — Axis 4 full report
   - `tmp/code-review/05-architecture.md` — Axis 5 full report
   - `tmp/code-review/06-error-handling.md` — Axis 6 full report
   - `tmp/code-review/07-api-surface.md` — Axis 7 full report
   - `tmp/code-review/08-harness-quality.md` — Axis 8 full report
