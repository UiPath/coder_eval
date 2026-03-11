# Bug Fixes — 2026-03-02

**Related PR:** #26

Comprehensive bug review across the coder_eval codebase. 21 bugs identified (2 HIGH, 9 MEDIUM, 10 LOW).
8 bugs confirmed and fixed, 2 marked as intentional design decisions, 1 acknowledged as a design concern (deferred),
10 LOW severity left as informational. Additionally, a multi-model code review surfaced 2 more issues that were fixed.

## Fixed Bugs

### BUG-01 (HIGH): Log cross-contamination in parallel batch runs

**Files:** `logging_config.py`, `orchestrator.py`

The `task_log_handler` context manager added a `FileHandler` to the shared `coder_eval` logger singleton.
In parallel batch runs, multiple concurrent tasks each added their own handler, causing each task's log file
to receive messages from all running tasks.

**Fix:** Added `_TaskIdFilter` (a `logging.Filter` subclass) that filters log records by `task_id`. Each handler
only receives records tagged with its own task ID. Added `threading.Lock` for thread-safe level management
with reference counting (`_task_handler_count`, `_task_handler_original_level`). `orchestrator.py` now passes
`task_id` to `task_log_handler`.

**Tests:** `tests/test_logging_isolation.py`

---

### BUG-02 (HIGH): Empty baseline dict makes complexity scoring meaningless

**File:** `criteria/reference_comparison.py`

When `comparison_method == "complexity"`, the checker passed `{}` as `reference_baseline` to `ComplexityScorer`.
All comparisons used arbitrary hardcoded defaults (10 cyclomatic, 50 LOC, 3 functions).

**Fix:** Compute baseline from reference code via `complexity_scorer.calculate_metrics(reference_code)` and
pass the result as the baseline dict.

**Tests:** `tests/test_reference_comparison_scoring.py::TestComplexityBaseline`

---

### BUG-03 (MEDIUM): Average command time divided by wrong count

**File:** `analysis.py`

Average was computed by dividing total time (sum of commands *with* timing data) by `len(all_commands)`
(including commands *without* timing), artificially deflating the average.

**Fix:** Divide by count of commands that actually have `duration_ms is not None`.

**Tests:** `tests/test_command_statistics.py`

---

### BUG-05 (MEDIUM): file_contains score averaging inflates results

**File:** `criteria/file_contains.py`

Score always averaged `includes_score` and `excludes_score` equally, even when only one category was specified.
When only `includes` was defined, `excludes_score` defaulted to `1.0`, inflating the result.

**Fix:** Conditional scoring — only average when both `includes` and `excludes` are actively specified.
Use just `includes_score` or `excludes_score` alone when only one is present.

**Tests:** `tests/test_file_contains_scoring.py`

---

### BUG-07 (MEDIUM): Division by zero in ComplexityScorer

**File:** `scoring/complexity.py`

When `reference_baseline` had `"cyclomatic": 0` or `"lines_of_code": 0` (valid for empty modules),
`agent_cc / (ref_cc * 1.5)` raised `ZeroDivisionError`.

**Fix:** `max(ref_cc, 1)` and `max(ref_loc, 1)` guards to prevent division by zero.

**Tests:** `tests/test_reference_comparison_scoring.py::TestComplexityDivisionByZero`

---

### BUG-08 (MEDIUM): Scoring ignores async def functions

**Files:** `scoring/signature_similarity.py`, `scoring/quality.py`

All scoring modules only checked `ast.FunctionDef`, missing `ast.AsyncFunctionDef`. For async-heavy code,
signature similarity, type hint scoring, docstring scoring, and error handling scoring all produced wrong results.

**Fix:** Changed all `isinstance(n, ast.FunctionDef)` checks to `isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))`.

**Tests:** `tests/test_scoring_quality.py::TestAsyncFunctionDefScoring`

---

### BUG-09 (MEDIUM): Quality scorer penalizes self/cls for lacking annotations

**File:** `scoring/quality.py`

`score_type_hints` counted `self` and `cls` parameters, which by PEP 484 should not have annotations.
A perfectly annotated method scored 0.75 instead of 1.0.

**Fix:** Filter out `self` and `cls`: `[arg for arg in func.args.args if arg.arg not in ("self", "cls")]`.

**Tests:** `tests/test_scoring_quality.py::TestSelfClsExclusion`

---

### BUG-10 (MEDIUM): LLM reviewer feedback silently discarded

**File:** `orchestration/evaluation.py`

When the LLM reviewer returned a valid decision with `issues` but an empty `next_steps` list,
the condition `if decision.next_steps:` was falsy. The function fell through to deterministic
criteria-based feedback, discarding the LLM's analysis.

**Fix:** Added `elif decision.issues:` branch that returns feedback based on `decision.issues`
even when `next_steps` is empty.

**Tests:** `tests/test_evaluation_feedback.py`

---

## Code Review Findings (Fixed)

### M1: Thread-unsafe logger level management

**File:** `logging_config.py`

Concurrent `task_log_handler` calls created a race condition when saving/restoring logger levels.

**Fix:** Added `threading.Lock` (`_task_handler_lock`) and reference-counted level management. Addressed
as part of the BUG-01 fix above.

---

### M3: duration_ms truthiness excludes 0.0ms commands

**File:** `analysis.py`

`if cmd.duration_ms` was used to filter commands with timing data, but this excluded commands with
`duration_ms=0.0` (a valid measurement). Changed to `if cmd.duration_ms is not None` in 3 locations.

**Tests:** `tests/test_command_statistics.py::TestAvgCommandTimeDivisor::test_zero_duration_counted_as_timed`

---

## Intentional Design Decisions (Not Bugs)

### BUG-04 (MEDIUM): Sandbox lacks path traversal protection

**File:** `sandbox.py`

`get_file_content`, `file_exists`, and `list_files` do not validate that requested paths stay within the sandbox.
This is **intentional** — the sandbox is a trusted execution environment where the agent needs filesystem access
beyond the sandbox root (e.g., reading installed packages, system headers). Path traversal protection is handled
at the agent permission level. Documented with an inline comment.

---

### BUG-06 (MEDIUM): Error categorization "insufficient" pattern too broad

**File:** `errors/categorization.py`

The billing error pattern matches the substring `"insufficient"`, which could catch non-billing errors.
This is **intentional** — we prefer false positives (skipping retry on a non-billing error) over false negatives
(wasting retries on a billing error that will never succeed). Documented with an inline comment.

---

## Deferred

### BUG-11 (MEDIUM): Config module-level side effects

**File:** `config.py`

`load_dotenv(override=True)` and environment variable mutations execute at import time.
Acknowledged as a design concern but deferred — requires significant refactoring and is low-risk in practice.

---

## Informational (LOW — Not Fixed)

| ID | File | Description |
|----|------|-------------|
| BUG-12 | `logging_config.py` | Custom formatter drops exception tracebacks |
| BUG-13 | `sandbox.py` | Hardcoded Unix `bin/` path for venv |
| BUG-14 | `reports.py` | Dict key access without `.get()` on loosely-typed data |
| BUG-15 | `errors/retry.py` | `truncate_log` exceeds `max_chars` for small values |
| BUG-16 | `errors/executor.py` | Dead code causes misleading "non-retryable" log messages |
| BUG-17 | `errors/categorization.py` | `"memory"` pattern too broad for OOM classification |
| BUG-18 | `orchestration/task_loader.py` | Empty YAML gives confusing TypeError |
| BUG-19 | `orchestration/evaluation.py` | First HYBRID snapshot is INCREMENTAL without a base |
| BUG-20 | `agents/claude_code_agent.py` | SDK usage accessed as dict, may be object |
| BUG-21 | `scoring/quality.py` | Inconsistent special-case return for no-function code |

## Summary

| Status | Count | IDs |
|--------|-------|-----|
| Fixed | 8 | BUG-01, 02, 03, 05, 07, 08, 09, 10 |
| Code review fixes | 2 | M1 (with BUG-01), M3 |
| Intentional | 2 | BUG-04, 06 |
| Deferred | 1 | BUG-11 |
| Informational (LOW) | 10 | BUG-12 through BUG-21 |
