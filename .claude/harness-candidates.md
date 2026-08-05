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

## From 2026-07-23 stop_when:auto early-stop review

- [ ] CE-rule: the early-stop watcher stop rule must decide polarity via the
  resolved `_armed_polarities`, never a raw `criterion.stop_when` comparison —
  forbid `.stop_when` attribute reads inside `EarlyStopWatcher._evaluate` /
  `_resolve_armed_polarities`'s callers in `orchestration/early_stop.py`. This
  diff *was* the fix for exactly that class of bug (the old rule compared
  `stop_when in ("pass","decided")` and so vetoed every mixed `auto` pass-stop).
  Deferred, not cheap: existing CE rules scope by file/module, not by a specific
  method, so a method-scoped attribute-ban needs a new AST-walk shape (and risks
  false positives on the legitimate `is not None` membership reads elsewhere in
  the file). Claim the next free id in `tests/lint/rules/`. Caught in the
  2026-07-23 stop_when:auto review; the behavior itself is guarded by
  `test_auto_mixed_pass_stops_ignoring_undecided_distractors` +
  `test_mixed_static_arming_pass_stops_ignoring_fail_armed`.

## From 2026-07-24 publish-github-releases review

- [ ] **sdist-contents assertion** — build the sdist and assert it contains only
  intended paths. `pyproject.toml` declares no `[tool.hatch.build.targets.sdist]`
  section, so hatchling's default selection honors only the **root** `.gitignore`
  and sweeps in everything else sitting in the tree at build time. Two distinct
  consequences, worth keeping apart:
  - **What actually reaches PyPI today: nothing unintended.** A local `uv build`
    in a developed worktree produces a 135 MB sdist carrying
    `evalboard/node_modules/**` (8520 files) and `evalboard/.next/**` (190),
    because `evalboard/.gitignore` is nested and therefore not honored. CI is
    spared only incidentally — `release.yml` never runs `npm`/`pnpm install`, so
    those paths do not exist on the runner at `uv build` time. Verified against
    the published artifacts: the 0.8.9 and 0.8.2 sdists on PyPI are ~7.5 MB /
    ~550 files with **zero** `node_modules` entries. (A dirty-tree release would
    not silently ship JS either — 135 MB exceeds PyPI's 100 MB per-file limit, so
    it fails at upload. The real exposure is a broken release, not a stealth one.)
  - **The live hazard is untracked files a workflow leaves in the tree**, which
    hatchling *does* package: a `release-notes.md` written at the repo root by a
    CI step landed in `coder_eval-X.Y.Z/release-notes.md` (verified by building
    it). This is why the "Publish GitHub Release" step writes to
    `${RUNNER_TEMP}` — a convention no check enforces.

  Not cheap: needs a real `uv build` inside the test suite (slow) plus a decision
  on whether to add an explicit sdist include/exclude allowlist, which changes
  published artifacts. Worth pairing with the allowlist so the contract is
  declared rather than inferred from hatchling's defaults — caught in the
  2026-07-24 ci/publish-github-releases review.
- [ ] **CE032 — run the existing AST lint rules over Python embedded in
  `.github/workflows/*.yml`.** CE008/CE009/CE010 already forbid unencoded
  `read_text`/`open`/`subprocess.run`, but `tests/lint/runner.py::check_paths`
  walks only `*.py` under `src/`, so Python inside a `run:` heredoc is invisible
  to ruff, pyright, pytest, coverage *and* the CE runner. Would have caught the
  four unencoded `read_text`/`write_text` calls fixed by hand in this review
  (`release.yml` ×2, `publish-testpypi.yml` ×2). Needs a heredoc extractor
  (`python3 - <<'PY' … PY` → dedent → `ast.parse`) with line-number mapping back
  to the YAML; wire as a `tests/test_custom_lint.py` class like CE027–CE031
  rather than a `BaseRule`. Also consider extending CE008 to `write_text` (it
  matches only `read_text` today, though `src/` happens to be clean).
- [ ] **CE033 — interpreter heredocs in `.github/workflows/**` must use a quoted
  delimiter** (`<<'PY'`, not `<<PY`). With a bare tag the shell expands `$VAR`
  into the *program text* before the interpreter parses it, so a value containing
  a quote or newline breaks out of the string literal it lands in. Fixed by hand
  in `publish-testpypi.yml` in this review (it interpolated `${DEV_VERSION}` into
  Python source); regex-detectable in ~10 lines, and CE032's `ast.parse` is only
  sound on quoted bodies, so the two ship together.
- [ ] **`actionlint` + `zizmor` over `.github/workflows/**`.** No static analysis
  whatsoever runs over workflow YAML today (`make verify` never looks at it), so
  every workflow finding in the 2026-07-24 review was caught by a human reading
  it. `actionlint` runs shellcheck over `run:` bodies; `zizmor`'s
  `excessive-permissions` / `artipacked` / `template-injection` rules cover the
  credential-scoping and `${{ }}`-into-`run:` classes reviewed by hand. Subsumes
  the CE026 SHA-pinning candidate above. Start as a non-blocking annotation job.

## From 2026-07-03 open-source docs cleanup

- [ ] **Dead-relative-link checker for `docs/**/*.md`** — resolve every relative
  `](target.md)` link against the tree and fail on a missing target. During the
  docs/features purge, the literal `git grep "docs/features"` gate missed 3
  dangling links written in relative form (`](features/...)` in
  TASK_DEFINITION_GUIDE.md ×2 and DOCKER_ISOLATION.md ×1); only a reviewer sweep
  caught them. The cleanup plan explicitly deferred this as YAGNI for the
  one-time purge, but any future doc rename/deletion re-opens the same blind
  spot — caught in the 2026-07-03 open-source-docs-cleanup implementation run.
- [ ] CE033 form (c): a raw `.model_dump(` feeding the AGENT-READABLE `task.yaml` write in docker_runner must route through `agent_safe_dump` instead — no lint check today because distinguishing the stripped `task.yaml` write from the legitimate root-only `task_full.json` `model_dump` in the same function needs data-flow analysis, not a single-node AST match. Codebase currently compliant (task.yaml uses agent_safe_dump). — caught in docker-isolation-user-separation Phase 5.
