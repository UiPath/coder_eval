# Harness & lint improvements

**Status:** implemented · **Date:** 2026-06-22

## What it does

A batch of harness-hardening and lint changes from the 2026-06-22 code review.
The theme is *mechanical enforcement*: turn recurring defect classes into
errors the toolchain catches, and backfill the runtime-only gaps that had no
test.

### Pyright: three diagnostics promoted to `error`

`reportImplicitStringConcatenation`, `reportMissingTypeArgument`, and
`reportImportCycles` are now `error` in `[tool.pyright]`. Existing
adjacent-string-literal sites were joined with explicit `+` (no message text
changed); the three by-design model-hub ↔ registry/config type-level cycles
carry explicit, commented file-level `# pyright: reportImportCycles=false`
pragmas at the files pyright anchors the diagnostic on (`agent.py`, `config.py`,
`agents/registry.py`). A *new* accidental cycle outside those seams now fails
CI. The unused `Orchestrator.run_batch` wrapper (the gratuitous orchestrator→batch
edge) was deleted; production already calls `orchestration.batch.run_batch`.

### CE018 — `no-final-status-name-denylist`

A new custom AST lint rule (`tests/lint/rules/ce018_no_final_status_name_denylist.py`)
flags string comparisons against `FinalStatus` *member names*
(`s == "SUCCESS"`, `s in ("FAILURE", "TIMEOUT")`) that re-implement
`FinalStatus.category` and silently miss new members. `reports_html._status_badge`
was the offender; it now dispatches on `FinalStatus(status_str).category` (single
source of truth), falling back to a `neutral` badge only for genuinely unknown
strings. The rule's hardcoded name set is pinned to the enum by a runtime test.

### `BaseSuccessCriterion.type` declared

The discriminator `type: str` is now declared on the criterion base (each
concrete criterion narrows it to its `Literal` tag), so `criterion.type` reads
without `getattr(..., "type", ...)` holes in `criteria/base.py` and
`models/tasks.py`. The Literal-narrows-`str` override is the intended pydantic
discriminator pattern; pyright's `reportIncompatibleVariableOverride` is
suppressed with one documented file-level pragma in `models/criteria.py`.

### ruff `PLR0915` / `PLR0912` function-size ceiling

`max-statements = 80` / `max-branches = 25`. The eight existing offenders carry
tracked `# noqa` debt markers pointing at this review; decomposing them is
separate out-of-scope work. The gate stops *new* god-functions from landing
without a visible marker.

### Fail-loud weighted score + single-sourced success gate

`EvaluationResult.calculate_weighted_score` raises `ValueError` on a
results/criteria length mismatch (an upstream bug) instead of silently
fabricating an unweighted average; the empty-input → `0.0` path is kept. The new
`EvaluationResult.all_criteria_passed(criteria)` is the single source of truth
for the pass/fail gate, and all four orchestrator gate sites now delegate to it.

### `ErrorCategory` liveness

`TESTS_FAILED` and `SANDBOX_COMMAND_ERROR` (no producing `categorize_error`
path, no downstream consumer) were deleted from the enum, `RETRY_CONFIG`, and
the pinned-contract test. A liveness test now drives *every* remaining member
from a real non-hint `categorize_error` input, so a future dead member fails CI.

### Backfilled tests

- `heartbeat_is_fresh` extracted as a pure, unit-tested watchdog predicate.
- `TokenManager._acquire_token` exercised end-to-end via `httpx.MockTransport`
  (200 happy path + non-2xx raise) with no network.
- The `coder-eval report` command gained `CliRunner` coverage for the markdown
  path and every `_regenerate_html_reports` exit branch (now 100% covered).
- The proxy `--vendor` option is a closed `click.Choice` (unknown values fail at
  parse).

### Sampling reproducibility

> **Note (reverted in review):** an earlier revision of this PR rejected
> `--type codex --backend {bedrock,proxy}` on the premise that Codex ignores
> `API_BACKEND`. That guard was **removed** — `--backend` doesn't only route the
> agent; the `llm_judge` / `agent_judge` calls share `config.api_backend`, so
> `--type codex --backend bedrock` is the standard way to run a Codex agent with
> Bedrock judges (org policy mandates Bedrock). Codex self-routes for its own
> calls and the backend stays available for the judges. `validate_api_keys`
> therefore validates Bedrock/proxy settings for codex as for any agent.

- **Sampling divergence (documented, not changed):** CLI `--sample N` is
  fixed-seed and reproducible by default. The stratified `Dataset.sample_per_stratum`
  draw is **nondeterministic by default** (`sample_seed=None` re-draws each run —
  a deliberate design for the nightly activation suites, which broadens coverage
  over nights). Reproducibility requires setting `sample_seed` to an integer. The
  field description now spells this out; `test_seeded_is_reproducible` pins the
  seeded contract and `test_unseeded_redraws_each_run` keeps the default guarded.

## Why

Each item closes a gap the review found: a defect that the type checker or linter
*could* have caught but didn't, a runtime path with no test, or a silent-degrade
that should be a loud failure. Promoting the diagnostics and adding CE018 convert
one-time fixes into permanent enforcement.
