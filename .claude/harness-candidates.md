# Harness Candidates

Deferred lint/test guardrails surfaced during reviews. Promote to a `CExxx` rule
(or a test) when picked up.

## From code review 260701-1954 (fix-review-top5 run) — deferred to a dedicated guardrail plan

> Numbering note: these are *proposed* ids. `CE024` (discriminated-unions) and
> `CE025` (live-verdict consistency) have since been **implemented** for other
> rules, so the candidates below were renumbered to the next free ids. Always
> claim the next unused number in `tests/lint/rules/` — the id-uniqueness assert
> in `tests/lint/runner.py` is the source of truth.

- **CE026** — workflow-YAML rule: forbid any `uses:` step pinned to a floating ref
  (`@v3`, `@main`) rather than a 40-hex commit SHA. Would have caught
  `mxschmitt/action-tmate@v3` (fixed manually in this run). >30 min: needs a
  non-Python file-walk branch in `tests/lint/runner.py`.
- **CE027** — retired-token grep gate: fail when a removed-subsystem token
  (`LLMGW_`, `API_BACKEND=proxy`, `uipath_llmgw_client`) reappears outside an
  allowlist across docs/config/src. Would have caught the LLM-Gateway residue
  swept in this run. >30 min: needs an allowlist + repo-wide text scan.
- **CE028** — assert the Makefile `lint:` help does not hardcode a stale `CE0NN`
  upper bound (use `CE001+`). Would have caught the `CE001–CE005` drift fixed here.
- **docs-vs-harness smoke test** — execute the CI tutorial's `coder-eval run`
  command against a NoOp task and assert the produced tree matches the documented
  globs. Would have caught the `--run-dir runs` layout bug. Not statically
  reachable (needs a live run).
- [ ] CE-rule: `type: Literal[...]` fields on models in `coder_eval/models/` must declare their tag default (`type: Literal["x"] = "x"`) — a member without the default degrades `validate_registry` diagnostics (PydanticUndefined in expected_types) and breaks direct construction. Nothing guards it today; needs a rule-design call (second violation class inside CE024 vs. a new CExxx at the next free id), and the failure is already double-caught by the MINIMAL_PAYLOADS parity test + direct-construction tests — caught in the 2026-07-03 top5-review-fixes run (Phase 1 quality review).

## From 2026-07-03 open-source docs cleanup

- [ ] **Dead-relative-link checker for `docs/**/*.md`** — resolve every relative
  `](target.md)` link against the tree and fail on a missing target. During the
  docs/features purge, the literal `git grep "docs/features"` gate missed 3
  dangling links written in relative form (`](features/...)` in
  TASK_DEFINITION_GUIDE.md ×2 and DOCKER_ISOLATION.md ×1); only a reviewer sweep
  caught them. The cleanup plan explicitly deferred this as YAGNI for the
  one-time purge, but any future doc rename/deletion re-opens the same blind
  spot — caught in the 2026-07-03 open-source-docs-cleanup implementation run.
- [ ] Function-local imports inside `Orchestrator._finalize_result` must sit inside a failure-isolation try (an escaping ImportError there swallows a captured teardown interrupt — the 89ec0d0 invariant). Pre-existing `write_task_html` / `spill_judge_transcripts` imports share the unguarded pattern, so a CExxx rule needs a decision on retrofitting them first — caught in adopt-atif-trajectory-emit final review.
