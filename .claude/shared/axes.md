# coder_eval Review Axes (shared catalog)

Single source of truth for the **axis spine** — the number ↔ name ↔ output-file
slug mapping used by every codebase-review consumer in `.claude/`. Reference this
table instead of restating the axis list, so adding / renaming / re-slugging an
axis is a one-place edit.

**Scope of this file:** the thin catalog only (num, name, slug, one-line scope).
The *rich* per-axis material is intentionally NOT duplicated here and lives once in
`commands/coder-eval-code-review-full.md`:
- **Per-axis severity anchors** — its `## Severity Standard` table.
- **Per-axis review criteria** — its `## Critical Axes` section.
- **Per-axis starting points + emphasis** — its `Axis starting points` bullets
  (step 4 of `## Procedure`). The `code-review-wf` orchestrator copies each
  bullet verbatim into the workflow's `axes[].startingPoint` payload, so those
  bullets stay whole in that file (splitting them would only move the duplication
  and make the orchestrator assemble the prompt from two places).

**Consumers** (keep current when you add one):
- `commands/coder-eval-code-review-full.md` — output-file naming references the Slug column.
- `commands/coder-eval-code-review-wf.md` — the orchestrator builds the `axes` payload (num / name) from this table; `startingPoint` still comes verbatim from the full command's bullets.
- `workflows/cr-parent.js` — its `AXIS_FILE` num→slug map is a **mirror** of the `#`/Slug columns below (the sandboxed workflow script cannot read a file at runtime, so it must hold a literal; keep it in sync with this table).

> Stability matters: the Slug values are persisted report filenames and the
> `AXIS_FILE` literal mirrors them. Don't re-slug an axis without updating
> `workflows/cr-parent.js` in the same change.

---

## Axis catalog

| # | Axis name | Output slug | Scope (one line) |
|---|-----------|-------------|------------------|
| 1 | Code Quality & Style | `01-code-quality` | Dead code, cyclomatic complexity, naming, over-engineering; cross-ref `ruff`. |
| 2 | Type Safety | `02-type-safety` | Public-API annotations, `Any` escape hatches, Pydantic field/return types; cross-ref `pyright`. |
| 3 | Test Health | `03-test-health` | Coverage gaps, **new public behavior shipped untested**, test isolation, flaky patterns; cross-ref `pytest`. |
| 4 | Security | `04-security` | Command injection, path traversal, clear-text secret logging, unsafe deserialization; cross-ref `bandit` / `pip-audit` / CodeQL. |
| 5 | Architecture & Design | `05-architecture` | Coupling/cohesion, circular imports, registry/SPI conformance, `models/` DRY, going-public internal coupling. |
| 6 | Error Handling & Resilience | `06-error-handling` | Bare/​swallowed excepts, fail-loud vs degrade-gracefully, control-flow-in-`finally`, resource leaks, retry correctness. |
| 7 | API Surface & Maintainability | `07-api-surface` | Public-API clarity, CLI user-facing correctness, configuration surface, breaking-change / tech-debt risk. |
| 8 | Evaluation Harness Quality | `08-harness-quality` | Task-def ergonomics, reproducibility, agent extensibility, daily/nightly blast radius, cross-repo (`coder-eval-uipath`) contract. |
