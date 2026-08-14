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

## From 2026-08-04 openhands-agent integration review

- [ ] **CE034 — forbid internal plan-phase labels ("Phase 0/1/2/3") in `src/`.**
  The MEMORY rule `no-plan-phase-refs-in-code` (keep internal Phase N labels out of
  `coder_eval` source/comments/config) is currently unenforced. This run leaked
  "Phase 0"/"Phase 2" labels into `openhands_agent.py` + `test_openhands_agent.py`
  (the SDK-surface facts were annotated "Phase 0 verified"); they were caught only
  by the spec-compliance reviewer and reworded by hand. Deferred (>30 min, not
  cheap): must scan **comments and string literals** across the whole `src/` tree
  (a whole-tree text rule like CE027–CE031, not a per-file `BaseRule`), AND carry an
  explicit exemption for the legitimate algorithm-stage usage CLAUDE.md calls out
  (`litellm_cost.py` uses "Phase 1"/"Phase 2" for a compute-then-mutate algorithm,
  which is allowed). Regex like `\bPhase [0-9]\b` in comments/docstrings, minus the
  exemption allowlist; wire as a `tests/test_custom_lint.py` class. Behavior is
  otherwise unguarded — only human review catches it today.

## From 2026-07-03 open-source docs cleanup

- [ ] **Dead-relative-link checker for `docs/**/*.md`** — resolve every relative
  `](target.md)` link against the tree and fail on a missing target. During the
  docs/features purge, the literal `git grep "docs/features"` gate missed 3
  dangling links written in relative form (`](features/...)` in
  TASK_DEFINITION_GUIDE.md ×2 and DOCKER_ISOLATION.md ×1); only a reviewer sweep
  caught them. The cleanup plan explicitly deferred this as YAGNI for the
  one-time purge, but any future doc rename/deletion re-opens the same blind
  spot — caught in the 2026-07-03 open-source-docs-cleanup implementation run.

## From PR #77 (command-executed shell-normalize) — CE030-to-criteria deferred

- [ ] **Extend CE030 doc/schema-parity to the `SuccessCriterion` union** so a new
  criterion (or field) can't ship undocumented. Attempted in PR #77 and reverted:
  CI installs `--extra uipath`, and in that environment `coder_eval.models.criteria`
  gains a `CliCalledCriterion` (fields `log`/`positional`) that is NOT present in a
  plain checkout (it did not reproduce on macOS, whose lockfile resolution omits the
  contributing linux-only component). It defeated every discriminator tried — union
  membership, a `__module__` string filter (it is spoofed to `coder_eval.models.criteria`),
  a genuine-module-attribute scan (it is `setattr` onto the module), and even an AST
  parse of the `SuccessCriterion` union literal in `criteria.py` source (CI's imported
  criteria module resolves to a file whose union literal already contains it). No
  runtime OR source signal available in the lint could separate the injected criterion
  from an in-tree one. Revisit only with a way to identify the in-tree criterion set that
  is provably immune to the uipath integration — e.g. a hardcoded name allowlist of the
  in-tree criteria (losing auto-coverage of new ones), or first understanding exactly how
  that environment injects the criterion. Until then CE030 stays scoped to the four
  top-level models; the `command_pattern`/`exclude_pattern` contract this PR changed is
  documented in the Field descriptions and TASK_DEFINITION_GUIDE regardless.

## From the evalboard Path-to-GA de-tag / mature-passes fix (4e5bbc4…dd5f7e9) — TS-side guards deferred

Context: the CExxx harness is a **Python** AST runner over `src/coder_eval/`, so none
of the invariants below are mechanizable in it. Each would need a TypeScript lint
harness (eslint config + custom rules) that `evalboard/` does not have today —
standing one up for three call sites fails the KISS/YAGNI gate. Deferring rather
than dropping; promote if a fourth TS-side invariant appears, and stand up the
harness once for all of them.

> **Update (PR #94 review round 2).** The *execution* half of this gap is closed:
> `evalboard/` is now gated by the `evalboard` job in `.github/workflows/pr-checks.yml`
> and reachable locally via `make evalboard-verify`, so the vitest suite (including
> the pricing drift guard) is enforcement rather than documentation. What remains
> deferred below is the *static-analysis* half — eslint has still not been stood up.
> The review that prompted this round names four more candidate TS rules (raw
> `status === "SUCCESS"` outside `lib/status.ts`; DOM-global shadowing in props;
> inline copies of the tag predicate; per-run tooltip copy reused on aggregate
> surfaces), which meets the "fourth invariant" promotion bar stated above —
> **stand up eslint next time `evalboard/` is touched substantively.**

- [ ] **"Every consumer of `RunOverviewTask.matureSkipped` must decide explicitly
  whether a carry-forward row counts."** Four consumers now, and they deliberately
  DISAGREE: `lib/trends.ts` and `app/runs/[id]/run-view.tsx` count a mature skip as
  a pass; `lib/overview.ts::buildTagTaskRows` excludes it from both terms
  (`/path-to-ga` is a GA-readiness page). A new consumer silently inheriting either
  convention is a real hazard. Guard shape: flag a file that reads `.matureSkipped`
  without a nearby comment naming its convention — weak, hence the deferral. Closed
  for now by unit tests that assert the exclusion from BOTH numerator and denominator
  (`lib/__tests__/overview.test.ts` → `describe("buildTagTaskRows")`).

- [x] ~~**`taskCarriesRepoTag` is the single repo-provenance tag predicate — but one
  duplicate survives.**~~ **RESOLVED in PR #94 review round 2.** The predicate moved to
  a dependency-free `lib/tags.ts` (structurally typed on `{skill, tags}` so
  `RunOverviewTask`, `TaskResultSummary` and `TaskTrend` all satisfy it), re-exported
  from `lib/overview.ts` for existing callers. Both inline copies now import it:
  `app/runs/[id]/run-view.tsx` (the `"use client"` one that could not before) and
  `lib/trends.ts::trendMatchesTag` (a third copy the original deferral missed).
  Still worth a lint rule ("no inline `tags.includes(x) || skill === x`") to catch
  future copies — folded into the eslint promotion noted above.

- [ ] **The de-tag rule fails CLOSED on a newest run that loads fine but stamps no
  `tags`** (`lib/overview.ts::buildTagTaskRows`): every tagged task would read as
  de-tagged and the table would empty, rendering an empty state indistinguishable from
  a genuine full de-tagging. Its sibling failure mode (`overview == null`, a transient
  blob read failure) IS guarded, with exactly this rationale. Currently unreachable —
  0 of ~116k date-shaped non-ad-hoc task rows in `runs-remote/` lack `tags`, and the
  six zero-tag runs found are all ad-hoc (filtered upstream by id shape + `meta.adhoc`)
  — so the barrier is two upstream filters rather than a check at the seam. Left
  unguarded on purpose: a `if (taggedInRun.size === 0) skip the de-tag signal` guard
  would also mask a real, total de-tagging. Revisit if the pipeline ever stops
  stamping tags, or if a non-ad-hoc run legitimately carries zero tagged rows.

- [ ] **Discriminating-test discipline for predicate narrowings.** Two tests in this
  change passed for the wrong reason — a downstream rule (the de-tag drop) masked the
  mutation they claimed to catch — and the plan leaned on a `grep` acceptance criterion
  that CI never runs. Both were found by mutation-testing the suite and fixed. No
  mechanizable guard; the durable lesson is: when a test names a narrowing, construct
  the fixture so the row SURVIVES every other rule, or the assertion proves nothing.

- [ ] **CE034 — runner-label registry + dogfood runner parity** over
  `.github/workflows/*.yml`. Two clauses: (a) every label a job can land on must appear
  in `.github/actionlint.yaml`'s `self-hosted-runner.labels` or a stock GitHub-hosted
  allowlist — including *both* branches of an expression-valued `runs-on:`, which
  actionlint treats as opaque; (b) `action-dogfood`'s label must equal the one the
  consumer snippet in `docs/tutorials/02-ci-pipeline.md` advertises. Nothing guards
  either today: actionlint is not wired into `make verify` or pre-commit (grep: the
  config file is its only mention), and CE026 parses that job's prerequisite *steps*
  but never its `runs-on:`. Why it matters: an undeclared label is not a runtime error,
  the job queues until GitHub cancels it hours later — indistinguishable from a pool
  outage; and a repo-wide `runs-on:` migration has twice swept up `action-dogfood`
  (#306, then 027121e in this PR), which exists precisely to prove the published Action
  works on the image external integrators use. Implemented and verified once (both
  clauses caught their regression class on the real tree) but reverted as out of
  proportion to a 16-line runner migration — ~240 lines including tests. Note when
  writing it: discriminate labels from expression operands structurally, on the
  preceding `&&`/`||`, NOT on the string's shape — a "contains 'ubuntu'" heuristic
  silently fails on `uipath-ubunut-latest`, the exact transposition typo the rule is
  for. Caught in the multi-model review of PR #86.
- [ ] OpenHands `_OPENROUTER_PROVIDER_ROUTING` (openhands_agent.py) duplicates the `provider.only` lists in `litellm/litellm-config.yaml`, synced only by a comment — a whole-tree lint rule could diff the two, but requires a YAML-vs-Python parse (>30 min). — caught in final review of c/2026-08-04-openhands-drop-litellm-proxy-path.md
