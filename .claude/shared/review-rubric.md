# coder_eval Review Rubric (shared)

Single source of truth for the review/plan slash commands and workflows in `.claude/`.
Consumers reference this file by section heading rather than inlining a copy, so a
rule changes in **one** place. Update this file periodically as the codebase's
conventions and lint rules evolve.

**Consumers** (keep this list current when you add a new one):
- `commands/coder-eval-create-plan.md` — Review Principles, Risk triggers
- `commands/coder-eval-implement-plan.md` — Review Principles, Review Criteria, Severity rubric, Risk triggers, Fix Policy, Harness loop
- `commands/coder-eval-code-review.md` — Review Principles, Review Criteria
- `commands/coder-eval-code-review-full.md` — Review Principles (its Severity Standard is a bespoke per-axis table, defined locally)
- `workflows/cr-axis.js` — reads **Review Principles** directly from this file; reads its Severity Standard / Output Format / Techniques from `coder-eval-code-review-full.md`
- `commands/coder-eval-code-review-wf.md` — the `code-review-wf` workflow's doc; states that its axis agents read **Review Principles** from this file (and the rest from `coder-eval-code-review-full.md`)
- `workflows/cr-parent.js` — transitive only: reads `coder-eval-code-review-full.md`'s synthesis-pass sections; no direct dependency on this file

**Other shared resources** in `.claude/shared/`: `axes.md` (the canonical code-review **axis catalog** — the num ↔ name ↔ output-slug spine, consumed by `coder-eval-code-review-full.md` and `coder-eval-code-review-wf.md`, and mirrored by `workflows/cr-parent.js`'s `AXIS_FILE`), `multi-model-review.md` (the `mcp__multi__codereview` + Opus review procedure), and `run-layout.md` (the on-disk run directory contract).

> Heading stability matters: `coder-eval-code-review-full.md` is read by the
> `code-review-wf` workflow by section heading. Do not rename the `## Review Principles`
> heading there without updating `workflows/cr-axis.js`.

---

## Review Principles

Evaluate against all of these:

- **Bug-free code**: logic errors, edge cases, off-by-one, unhandled states, concurrent modification.
- **KISS**: as simple as it can be; no unnecessary abstractions or indirection.
- **DRY**: no duplicated logic that should be consolidated; no premature abstraction either.
- **Not over-engineered**: no unnecessary generalization, no speculative features, no "just in case" code.
- **Simplicity**: a junior could follow it; intent is clear.
- **No unnecessary comments**: don't describe what the code obviously does.
- **CLAUDE.md adherence**: follows patterns in CLAUDE.md and the codebase — no ad-hoc solutions that bypass established abstractions.

---

## Review Criteria

The coder_eval-specific quality checklist. Check every item:

1. **Correctness**: matches the plan's/PR's intent; all edge cases handled.
2. **Type safety**: proper annotations, no `Any` escape hatches, Pydantic fields have correct types/defaults/descriptions.
3. **Ripple completeness**: all references updated when a model field/config key/CLI flag is added/removed/renamed (task YAMLs, experiment YAMLs, `.claude/commands/`, docs, `experiments/default.yaml`, `models/__init__.py`).
4. **Test quality**: no hardcoded magic values from config; at least one test for the exact edge case; sandbox cleanup via `try/finally` or fixtures; coverage of happy path, invalid input, error paths, boundary conditions; tests use Haiku/Sonnet, never Opus.
5. **Shell safety**: commands built via f-string use `shlex.quote()` or argument lists.
6. **Allowlist over denylist**: status classification uses explicit allowlists, not denylists (CE018 guards `FinalStatus`).
7. **Resource cleanup**: no file handle, subprocess, or temp directory leaks — especially in error paths.
8. **Public API surface**: new exports from `coder_eval.models` are intentional; discriminated unions updated if needed.
9. **Regression lint**: for each correctness bug found, consider whether it is mechanically detectable (wrong import path, missing decorator, blocking call in async, silent exception). If so, flag it as a candidate for a new rule in `tests/lint/rules/` (CE001–CExxx).
10. **Layer-merge coverage**: new fields on `ResolvedTask` / `AgentConfig` / `BatchRunConfig` have explicit coverage in `test_experiment_resolver.py` exercising all 5 merge layers (default → exp defaults → task → variant → CLI), and a matching `-D` override path. New list/dict fields declare a `MergeField` strategy (CE014).
11. **Pydantic round-trip integrity**: changes to layered configs or polymorphic `CriterionResult` subclasses preserve `model_fields_set` and the discriminator across `model_dump(exclude_unset=True)` → `model_validate()`. Round-trip tests exist for new variants.
12. **Discriminated unions**: new or modified Pydantic unions use `Annotated[..., Field(discriminator="type")]`. Bare `A | B | C` unions silently coerce to the first variant on a missing or typo'd `type`.
13. **Cross-retry state hygiene**: after `AgentCrashError` / `TurnTimeoutError` / `is_error=True` SDK message, the agent resets `_session_id`, `pending_turn`, watchdog references, streaming-event `ContextVar`s, and iteration counters before the next attempt. Test covers a crashing turn followed by a successful turn in the same `Orchestrator` instance.
14. **Untrusted text in evaluator prompts**: strings derived from agent output (tool-call args, stdout, file contents, dialog history) injected into a judge / simulator / reviewer prompt are wrapped in a fenced block with explicit untrusted-data framing; the system prompt instructs the model to treat that block as adversarial.
15. **NaN / non-finite guards**: score and threshold clamps via `max(lo, min(hi, x))` are preceded by `math.isfinite(x)`. Bad parses fail explicitly instead of silently returning the upper bound (`max(0.0, min(1.0, nan)) == 1.0`).
16. **Registry over hardcoded dispatch**: new agent / criterion / template / route variants go through the existing registry (`@register_criterion`, the `coder_eval.plugins` SPI, etc.). `if x.type == ...` / `isinstance(...)` ladders in `orchestrator.py`, `simulation/`, or `evaluation/` are rejected.
17. **Test mocks match real SDK shape**: mocks of `claude_agent_sdk` types only set attributes present on the installed class. Use `Mock(spec=RealType)` or assert `hasattr(RealType, attr)` in test setup so production reads of fabricated fields surface as test failures.
18. **`extra="forbid"` on configs**: Pydantic models that consume YAML or CLI input declare `model_config = ConfigDict(extra="forbid")`. Unknown keys raise instead of being silently dropped.

---

## Severity rubric

Used by the plan commands' per-phase and final reviews. (The `coder-eval-code-review*`
commands define their own emoji/CVSS/per-axis severity standards locally — those are
intentionally distinct and are NOT governed by this block.)

- **Critical** — data loss, security/sandbox-escape, corrupted evaluation results, or a crash on a common path.
- **High** — incorrect behaviour on a realistic input, an unhandled error path, a broken invariant from the project hot list.
- **Medium** — a narrower correctness bug, a missing test for a behavioural path, a KISS/DRY violation that will mislead future readers.
- **Low** — style, naming, a comment that restates code, an informational nit. Noted, not fixed.

---

## Risk triggers

Used to tag a plan phase **Low** or **High** (sets the implementer's per-phase review depth).

A phase is **High** if it: introduces new behaviour (a new criterion, agent, CLI command, or evaluation path); changes a Pydantic model/schema that existing task or experiment YAMLs depend on; changes the agent lifecycle or retry/crash-recovery path; changes the 5-layer config merge or a default in `experiments/default.yaml`; touches token accounting or pricing; handles untrusted agent output fed into a judge/simulator prompt; or spans multiple modules/layers. Otherwise it is **Low** (a localized fix, a self-contained helper, a test-only change, a docs/comment edit).

If a diff turns out materially riskier than the plan's tag claimed, trust the diff and treat it as High.

---

## Fix Policy

How to act on a review finding (per-phase or final) and on new work.

- **Logic/correctness bug** → write a failing test first (watch it fail for the right reason), then fix, then confirm green. New behaviour (a new criterion, agent path, CLI flag) is a logic path — the plan's **Tests to Write** lists these; honor any phase the plan deliberately defers ("Tests to Write: none — covered by Phase N").
- **Structural issue** (naming, KISS/DRY, dead code) → fix directly, no test.
- **Can't reproduce** → false positive, change nothing.

Process findings by severity: fix Critical/High/Medium; leave Low as informational.

---

## Harness loop

coder_eval converts one-time fixes into permanent enforcement via custom lint rules
(`tests/lint/rules/`, CE001–CExxx pattern, wired in `tests/lint/runner.py`). A review
finding that enforces a rule **no automated check guards** is a harness gap. For each,
*after* fixing the instance:

- **Cheap to guard (≲30 min) → write it now and commit it** as its own `test(...)`/`feat(...)` commit. Cheap means there's an existing thing to mirror: a new CExxx AST lint rule (+ its test), a Pydantic validator, or a unit test like one already in the affected module. **closed.**
- **Not cheap → defer.** Append one line to `.claude/harness-candidates.md` (create if absent) — `- [ ] <rule> — <why nothing guards it today> — caught in <context>`. Commit the append with the run. **deferred.**
- **Not a harness gap → skip.** A one-off logic bug already covered by the failing test you wrote needs no separate guard.

The promote-or-defer pass runs once, at final review. Report **closed vs deferred** counts in the final summary.
