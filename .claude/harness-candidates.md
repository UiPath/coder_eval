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

- [ ] **CE038 — runner-label registry + dogfood runner parity** over
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

## From the 2026-08-04 Claude Code plugin marketplace run

- [ ] **Plugin skills must not name a file that exists only in THIS repo** — the
  `test_bundled_files_reference_no_repo_paths` denylist (`docs/`, `src/`,
  `.claude/shared/`, `.claude/commands/`, `uv run`, `../`) deliberately allows
  `tasks/` and `.claude/skills/`, because those are user-workspace paths the
  skills legitimately scan and scaffold. So a skill body naming a specific repo
  file (e.g. `tasks/hello_date.yaml`) would slip past the guard even though an
  installed plugin is copied to `~/.claude/plugins/cache/` without it. The
  obvious rule — "extract path-shaped tokens, fail if the path exists at the repo
  root" — is NOT cheap: `init` legitimately tells users to scan `pyproject.toml`
  and `package.json`, and `pyproject.toml` exists here, so the heuristic
  false-positives on correct prose. Needs a token classifier that distinguishes
  "a file to look for in the user's repo" from "a file in ours", which is a
  design problem, not a 30-minute one. No skill violates it today (grepped) —
  caught in the 2026-08-04 claude-code-plugin-marketplace implementation run.
  *Update (2026-08-04, plugin-audit-p0-p1 run): the guard was renamed and widened
  from `skills/*/SKILL.md` to every shipped text file under `plugins/coder-eval/`
  (`PLUGIN_TEXT_FILES`), which closed the coverage half of this gap — a bundled
  reference now cannot name a repo path either. The token-classifier problem
  described above is unchanged and still deferred.*

## From 2026-08-04 plugin-audit-p0-p1 run

- [ ] **A skill's advertised `description` must not promise a check that no bundled
  reference declares.** `lint-tasks` ships a user-facing description claiming it
  finds "prompts that give away the answer", but that check was declared only in
  `skills/task/SKILL.md` prose — a file `lint-tasks` never reads — so the two
  rubric readers had already forked on it before the skill shipped. Caught by a
  reviewer, not by a test; fixed by promoting it to rubric check 7. A guard would
  have to map claim-phrases in a description onto declarations in
  `reference/task-rubric.md`, which is natural-language matching, not a token
  grep — the phrasings are deliberately different (a description sells, a rubric
  check instructs), so any cheap version either misses the real case or fails on
  correct prose. Needs a fixed vocabulary of claim tags shared between the two
  files to become mechanical, which is a design change rather than a 30-minute
  rule — caught in the 2026-08-04 plugin-audit-p0-p1 implementation run.

## From the PR #82 review follow-up (2026-08-10)

- [ ] **CE039 — documented `coder-eval` invocations must be executable as written.**
  `init/SKILL.md` told the agent to run `coder-eval plan <task-directory>` and
  "iterate until it exits 0", which the CLI rejects outright (`plan` takes files;
  a directory argument exits 1 with a hint) — an unreachable loop condition
  shipped in a skill. A rule would scan inline-code spans and fenced `bash` blocks
  across `README.md`, `docs/**/*.md` and `plugins/**/*.md`, assert the subcommand
  exists in the Typer app, and — the harder half — that the *argument shape* is
  one the command accepts. The subcommand check is cheap and would not have caught
  this; the argument-shape check is what matters and needs either a real
  invocation (see the live-smoke candidate below) or a per-command arity model
  that duplicates the CLI signature. Deferred on that split — caught in the PR #82
  review, fixed by hand in `init/SKILL.md`.

- [ ] **Documented-CLI live smoke.** The behavioural counterpart to CE039: in a
  `-m live`/`-m slow` test, materialize a fixture repo with one task YAML and
  execute every fenced `coder-eval …` command extracted from the shipped skills
  and docs, asserting exit 0 (or an explicitly-expected non-zero). This is the
  only form that proves argument shape rather than command existence. Not
  statically reachable, hence separate from CE039 — proposed in the PR #82 review.

## From the 2026-08-11 plugin generic-adopter run

- [ ] **`working-directory` input on `action.yml`.** A repository whose eval tree is
  nested (`tests/tasks/…`) has no way to tell the composite action to run from that
  subdirectory, so every path in every input has to be spelled from the repo root. The
  fix is a new input, and that is why it is deferred rather than cheap: `action.yml`'s
  inputs are a **published API** — CE026 clause 4 asserts every `with:` key across four
  onboarding surfaces is a real input, so adding one means updating those surfaces (the
  `ci` skill among them, whose output lands in other people's repositories), and it
  carries action tag/release implications. Out of scope for the plan that surfaced it,
  which worked around it in the `ci` skill's prose instead.

- [ ] **`shopt -s globstar` (or quoting `$CE_TASKS`) in `action.yml`'s run step.** The
  real fix for a degradation the `ci` skill currently works around in prose:
  `args+=($CE_TASKS)` is deliberately unquoted so a caller can pass several patterns, but
  with `globstar` off `a/**/*.yaml` expands to `a/*/*.yaml` and **silently drops every
  top-level task** — reproduced with `a/top.yaml` + `a/sub/deep.yaml`, which yields
  `deep.yaml` alone. `nullglob` is off too, so an unmatched pattern reaches the CLI
  literally and exits 1 (`Error: Task file not found: …`). One line in the action fixes
  the first half; the second half is arguably correct-as-is (failing loudly beats
  silently running nothing). Deferred alongside `working-directory` because both change
  the action's observable contract and belong in one considered change.

## From the final review of the 2026-08-11 plugin generic-adopter run

Two **pre-existing `action.yml` defects** surfaced by an external reviewer during that
run's final review. Neither is caused by the change, and `action.yml` was explicitly out
of that plan's scope, so both are recorded here rather than fixed in passing. They belong
with the two `action.yml` items above — one considered change to the action's contract.

- [ ] **The score gate silently drops a malformed `weighted_score`.** `action.yml`'s
  minimum-task-score step filters `task_results` rows down to usable floats; a row whose
  score is a string, a bool, `NaN`/`inf`, or out of `[0, 1]` is omitted from the
  comparison rather than failing it. So a `run.json` carrying one corrupt row **and** one
  valid row above the floor gates **green**, which contradicts the fail-closed intent
  stated in that step's own comment. The fix is to error on a present-but-invalid score
  while still skipping `None` (errored tasks are already covered by coder-eval's exit
  code). Wants a test over a synthetic `run.json` per bad-value class, which is why it is
  not a five-minute change.

- [ ] **`tasks:` is declared optional but omitting it cannot work.** The input defaults to
  empty and the run step then appends no path arguments, so `coder-eval run` is invoked
  bare — and zero-argument discovery resolves against the *installed package's* location,
  finds nothing, and exits 1. The input is therefore effectively required, and the action
  advertises otherwise. Either mark it `required: true` (a published-input contract change,
  see the `working-directory` item) or fail with a clear message instead of an obscure
  discovery error.

## From the coder-eval-code-review of fix/antigravity-wait-for-wakeup (2026-08-12)

- [ ] **A retry/poll loop's continuation state must derive from a stable per-entity
  key, never a mutable monotonic counter used as an id fallback.** `_AntigravityTurnState._handle_tool_call`
  minted a synthetic tool-call id from `f"{raw_name}_{self._next_seq}"` when the SDK's
  `call.id` was falsy; since `_next_seq` advances between a tool call's ACTIVE and DONE
  emissions, the DONE step computed a *different* fallback id than the ACTIVE step,
  stranding the ACTIVE entry as a permanent orphan and stalling `communicate()`'s new
  poll loop for its full `_MAX_BACKGROUND_POLLS` budget on every id-less turn. Fixed by
  deriving the fallback from `(step.step_index, call_index)` instead (stable across a
  step's own re-emissions, per this class's own docstring) -- then, in the same PR,
  further folded in `step.trajectory_id` (falling back to bare `step_index` when it's
  empty, mirroring the SDK's own `trajectory_id:step_index` id scheme), since a
  sub-agent trajectory can reuse the same low `step_index` values as the main one and
  two id-less calls across trajectories would otherwise collide. Not promoted to a CExxx rule:
  this is the only id-fallback-driving-control-flow site in the codebase today (a
  single call site, not a recurring class per the existing "single call-site fix, no
  recurring pattern to guard" convention) — a mechanical AST rule for "no mutable
  counter in a dict-key fallback" would need real design work to avoid false-positiving
  on ordinary sequence-numbering counters elsewhere in the file. Caught by two
  independent reviewers (Opus fallback pair) in this run's final code review.

- [ ] **A `while` loop built around a cooperative-cancellation watchdog should read the
  watchdog's own "already decided to fire" flag in its condition, not rely solely on a
  later exception handler to notice.** The antigravity poll loop's condition checked
  `not state.stopped_early_hit and state.has_orphaned_tool_call() and poll_count < cap`
  but not `state.timeout_hit`, so if `ThreadedWatchdog`'s background thread set the flag
  before its `task.cancel()` actually landed on this coroutine, the loop kept
  sleeping/re-draining for up to the full poll budget before the pre-existing
  post-loop `if state.timeout_hit:` check ever got a chance to run. Fixed by adding
  `and not state.timeout_hit` to the condition, plus a mid-body early exit right after
  the sleep (`if state.timeout_hit: break`) so a flag landing DURING the sleep skips
  the following re-drain too, instead of waiting for the loop's next head check. Not
  promoted: `ThreadedWatchdog` + a bespoke poll loop reading its own state flag is a
  one-off shape unique to this agent; no second instance exists to generalize a rule
  from. Caught in the same
  final review as above.

- [ ] **A regression test's fake dependency must model every layer the fix under test
  actually touches, not just the outermost one.** `_drain()`'s cooperative-stop path
  wraps a real SDK call (`Conversation.receive_steps()`) that is itself a delegating
  async generator over an inner, connection-layer generator holding the real
  re-entrancy guard. The first regression test written for this fix used a
  single-layer fake (the guard lived on the SAME generator `_drain()` iterated), which
  passed against an incomplete fix (`contextlib.aclosing` on the outer generator only)
  that does not work against the real two-layer SDK shape — confirmed live that the
  inner generator's cleanup is deferred to a LATER event-loop turn, not synchronous
  with the outer's `aclose()`. Caught by a reviewer re-deriving the real dependency's
  shape from its installed source, not by the test itself. Not promoted: detecting "a
  test double is missing a delegation layer the source has" is a semantic match
  against third-party source, not an AST pattern in our own code — no cheap mechanical
  check exists. Caught in the round-3 coder-eval-code-review of this same branch.

- [ ] **An agent's internal sleep-and-retry loop must derive its own exit bound from
  the turn's actual `timeout`, never a fixed cycle count picked independently.** The
  poll loop's own graceful exit path (force-close a never-resolving orphan as
  unresolved, finalize and grade normally) was bounded by `_MAX_BACKGROUND_POLLS * _BACKGROUND_POLL_INTERVAL_SECONDS`
  (120 × 5s = 600s) — DOUBLE `experiments/default.yaml`'s own default `turn_timeout: 300`.
  Since the pre-existing `ThreadedWatchdog` enforces `timeout` by cancelling the whole
  turn, it always won that race under default settings, making the graceful path dead
  code: a tool call spuriously left ACTIVE with no real background job behind it (a
  real, observed case — see the final validation run) went from "finalizes immediately,
  graded on whatever the agent wrote" pre-fix to "burns the full 300s, then crashes as
  `TurnTimeoutError` with zero criteria graded" post-fix — a strict regression for that
  input class. Fixed by deriving a `poll_deadline` from a fraction (0.8x) of the actual
  `timeout` passed to `communicate()`, falling back to the cycle cap only when
  `timeout is None`. Caught independently by two reviewers (`bai-uipath`, `uipreliga`)
  on the PR, both citing the exact same arithmetic mismatch. **Not promoted in this
  pass**, but a stronger candidate than most entries here: `uipreliga` proposed a
  generic whole-tree rule (proposed as CE035, renumbered CE042 here — CE035 shipped as
  the workflow-outputs resolver on the published-action branch) — for every sleep-loop under
  `src/coder_eval/agents/**`, assert its own cycle-count × interval either references a
  timeout-derived name or is provably below `experiments/default.yaml`'s baseline — that
  would catch this class of bug in ANY agent, not just this one (confirmed zero
  violations on `main` before this bug, one on this PR). Worth a real look next time
  `agents/` is touched, since a second agent adding its own disconnected sleep-loop
  constant would reintroduce the exact same shape.

## From 2026-08-04 published-action verification review

- [ ] **CE041 — `VAR=$(… | grep …)` under `set -e` followed by an emptiness check
  is a dead diagnostic.** With `set -euo pipefail`, a pipeline whose `grep` matches
  nothing exits 1, so the assignment aborts the step *before* the
  `if [ -z "$VAR" ]; then echo "::error::…"` branch that was written to report it —
  the operator gets a bare exit 1 with no message. Also applies to `head -1`
  closing the pipe early (SIGPIPE 141). Fix is `|| true` on the substitution,
  letting the emptiness check own every failure mode. Detectable by matching
  `\w+=\$\(.*\|\s*(grep|head)\b` inside a `run:` body whose script sets `-e`, then
  requiring `|| true`/`|| :` on the same logical line. Caught by a reviewer in
  `verify-published-action.yml`; **`actionlint` + shellcheck do NOT flag it**
  (verified against the exact snippet), so the actionlint candidate above does not
  subsume this one.
- [ ] **CE036 — ban the skipped-green job gate.** Fail a job-level `if:` in
  `.github/workflows/**` whose only discriminator is an emptiness/equality test on
  `needs.<job>.outputs.<key>`. A lost output on a partial "Re-run failed jobs" resolves
  the job to SKIPPED-**green**, so an operator sees a green re-run while nothing ran.
  Fixed by hand twice now: `promote` was designed around the hazard, and
  `publish-pypi`'s `if: needs.release.outputs.version != ''` (dead *and* dangerous — a
  skipped publish also skipped `promote`) was removed in the follow-up review. CE035
  catches the *typo* class; this catches the *shape*. Escape hatch: inline
  `# noqa: CE036 — <reason>` for value-driven gates that cannot strand a release.
- [ ] **CE037 — `if: failure()` is wrong in a job containing a `continue-on-error`
  step.** Require `always()` (or a reference to the tolerated step's
  `steps.<id>.outcome`) on diagnostic/upload steps in such a job. Fixed by hand in
  `verify-published-action.yml`: the run dir was discarded in exactly the tolerated-red
  case the gate is designed around, because a tolerated red leaves the job green and
  `failure()` never fires. Pure YAML shape check, ~30 lines.
- [ ] **CE040 — cap inline `run:` bodies; oversized decision logic belongs in
  `.github/scripts/`.** `verify-published-action.yml`'s parity step (~70 lines, 7
  decision points) and its e2e gate (~66 lines, switching from bash to a `python3`
  heredoc mid-step) are 10-20-branch units invisible to `make check`, `make lint`,
  `pyright` and coverage — which is the structural reason the `steps.parity.outputs.version`
  bug survived to `main`. Analogous to CE022's statement cap; composes with CE032/CE033.
  Deferred as a refactor, not a fix: extraction touches all 423 lines of a workflow that
  cannot be exercised before merge, and CE035 + `tests/test_verify_published_workflow.py`
  now cover the specific failure classes. Precedent for the extraction:
  `.github/scripts/release_notes.py` + `tests/test_release_notes.py`.
- [ ] **Exercise the Action's score gate in the FAILING direction.** Both
  consumer-simulating jobs pass `minimum-task-score: "0.0"`
  (`verify-published-action.yml`'s `e2e`, `pr-checks.yml`'s `action-dogfood`), so the gate
  is only ever proven to *pass*. The new exit-contract assertion catches a gate that
  wrongly fails; nothing catches one that wrongly passes — the direction that silently
  disables every consumer's quality gate. Needs a second invocation with an unmeetable
  score floor, i.e. a second paid agent run per nightly; deferred on cost, and better
  placed in `action-dogfood` (PR-time, already paying) than in the cron.
- [ ] **Extend CE026's `REQUIRED_PREREQ_TOKENS` anchor to the `e2e` job.** The lint pins
  the documented Node + `@anthropic-ai/claude-code` prerequisite steps to a single
  executable reference (`action-dogfood` in `pr-checks.yml`, via
  `tests/lint/action_docs.py::DOGFOOD_JOB`). `verify-published-action.yml`'s `e2e` job is
  now a third copy of the same two steps — and the truer consumer proof (no checkout,
  published action, default pin) — so the two can drift while the docs follow only one.
- [ ] **Runtime-key parity for `run.json` consumers outside `src/`.** The e2e gate in
  `verify-published-action.yml` reads `task_results[*].status` / `weighted_score` /
  `total_tokens`, and `action.yml`'s score gate reads `weighted_score` / `task_id`.
  These are string keys in shell/YAML that no test or type-checker binds to
  `eval_result_to_task_dict` (`reports_experiment.py`), so renaming a key there
  silently turns an external gate into a no-op — a reviewer here proposed
  `final_status`, which does not exist in `run.json` and would have made a new
  assertion dead on arrival. Guard: assert the key set that non-Python consumers
  depend on, mirroring how CE030 pins doc/schema parity.
## From the split-field / optimize-skill plan (2026-08-12)

- [ ] **A run whose every task is skipped exits 0 — a green run of zero tasks.** *(Narrow case
  CLOSED 2026-08-13: a `--split` selector that matches no labelled row now raises
  `SplitSelectorError` out of `expand_dataset`, which `resolve_all_tasks` re-raises and the CLI
  turns into a `typer.BadParameter` — exit 2. The GENERAL case below stays open: `skip: true`,
  load failures and tag filters that match nothing all keep today's exit-0 behaviour, because
  making those fatal changes exit semantics for deliberate quarantine workflows and needs its own
  decision plus tests per case.)* When
  `resolve_all_tasks` demotes every task to `skipped_tasks` (a load failure or `skip: true` —
  no longer a `--split` typo, see above), the run reports success: nothing
  failed, so the exit gate in `cli/run_command.py` — which keys only on failed/errored tasks
  and suite gates — passes. Verified directly before the narrow fix: `coder-eval run <suite>
  --split holdou` printed one yellow "1 task file(s) skipped" line and exited 0. `--split` was
  what made it reachable by a one-character CLI typo rather than a broken file, and
  the whole point of a test confirmation is that you trust its verdict. Still unguarded for
  the remaining paths, and
  not a five-minute fix: making an all-skipped run non-green changes exit semantics for
  every skipped-task path (including deliberate `skip: true` suites and tag filters that
  match nothing), so it needs a decision about which of those should be fatal, plus tests
  per case. A narrower option is to fail only when a CLI *selector* (`--split`, `--tags`)
  eliminated everything, since that is unambiguously a user error rather than repo state.

- [ ] **Semantic answer-leak in a task prompt** — a prompt that describes the graded behaviour in *different words* ("list the paths explicitly rather than with a recursive wildcard" while grading an explicit glob) scores well whether or not the behaviour happened, and in an A/B an arm that deleted the rule still passes. CE036 catches only the verbatim form; the semantic form needs an LLM judge or a `lint-tasks` pass over this repo's own `tasks/`, neither of which is cheap or deterministic. — caught in the final review of c/2026-08-13-optimize-skill-fixes.md, where 4 of 10 rows in a shipped worked example had it.
- [ ] **A doc claim that contradicts merge semantics** — `optimize-skill` told users to declare `allowed_tools` in an experiment's `defaults: agent:`, which is a silent no-op because those fields merge by `replace` and the task layer outranks experiment defaults. Detecting "this prose recommends a config location that the merge order makes ineffective" would need the rule to model the layer stack against prose, which no existing rule shape supports. — caught in the final review of c/2026-08-13-optimize-skill-fixes.md.
- [x] **`_normalized()` not used by every prose sensor** — CLOSED: all 9 sites converted, and `test_no_sensor_inlines_the_normalization_idiom` now forbids the raw form. Original note: — 8 sensors in `tests/test_custom_lint.py` still inline `" ".join(path.read_text().split())`, so a future one copied from the wrong neighbour is defeated by a line wrap (the bug that let a stale skill count ship past 91 green tests). A rule forbidding the raw idiom in that file is easy; the conversion sweep was out of scope. — caught in the final review of c/2026-08-13-optimize-skill-fixes.md.

## From the optimize-skill review v2 plan (2026-08-13)

- [ ] **"The ToolStart seam decides" is now a PER-CRITERION property, not a global invariant.** `command_executed`'s verdict is decidable from the tool call's inputs **only while `require_success` is unset** — with it set, `_matching_commands` drops the in-flight call, whose `result_status` is `None`, and the criterion decides at ToolEnd like any other (corrected in Plan D Phase 4; the entry originally stated the unconditional form, which is false for the configuration CE034 mandates). `skill_triggered`'s is never decidable there (for the `Skill` tool the body is delivered AS the result, so an in-flight call engaged nothing). A new `LiveSuccessCriterion` must state which seam its `live_verdict` is decidable at, and a criterion that decides at the ToolStart on information only the result carries silently diverges from its own frozen check. Not mechanically detectable today: the property is about what a `live_verdict` implementation *reads*, which no AST rule can infer — a rule would have to know that `result_status` is the field distinguishing the two seams. A cheaper partial guard would be a test-level convention (every live criterion has a "not decided before the result" or "decided on the call" test), which is a sweep rather than a rule. — caught implementing Phase 1 of c/2026-08-13-optimize-skill-review-v2-fixes.md.

- [x] ~~A prose claim in `plugins/` about `src/` behaviour that no sensor checks — the token sensors
      check PRESENCE, never TRUTH. Shipped false twice in one change: "the gate cannot be computed
      from one run dir" (it can) and a halving cost saving that was arithmetically a premium. Only
      `test_optimize_skill_snippet_names_the_public_gate_api` checks a claim against the code, and
      each such sensor is bespoke — there is no general form. — caught in the optimize-skill gate
      corrections review, 2026-08-13.~~ **CLOSED 2026-08-14 by CE039** (`tests/lint/computed_claims.py`
      + `tests/test_custom_lint.py::TestCE039ComputedClaims`). The general form is a `ComputedClaim`
      registry whose entries *compute* the claim, plus the coverage rule that makes it a class
      rather than N bespoke sensors: an arithmetic-bearing table in the two optimize surfaces that
      no registered claim names **fails**. Three claims shipped with it — `cost-table` (asserts
      invariants of the cost model: halved is never cheaper than flat at any N in 2..32, activation
      Stage B is exactly 3x Stage A per arm, the control row's paired figure is 2x its unpaired one,
      and Stage C alone is priced in `M_test`), `halving-premium` (recomputes every cell and the
      standing never-saves claim), and `interval-from-one-run-dir` (**behavioural** — builds a
      one-run-dir fixture and asserts `activation_gate` returns an interval but no MDE, which is
      the exact claim whose false version shipped). Both self-tests are committed: one proves the
      real matcher catches a wrong `premium` cell, one proves the real coverage matcher reports an
      unregistered table — neither needs a shipped file edited to demonstrate.
- [ ] Changing a module-level statistical constant in `reports_stats.py` silently reddens
      `tests/_fixtures/report_snapshots/`, which no phase's scoped tests run. A blast-radius check
      ("these fixtures are downstream of these constants") is not obviously expressible without
      hardcoding the pairing it is meant to discover. — caught in the same review.
- [ ] A note appended to a local list AFTER that list has been passed into a Pydantic model
      constructor is silently discarded — pydantic COPIES the list during validation, so the
      append mutates a detached object nobody reads. Cost a High finding in `execution_gate`
      (the below-MDE warning and the zero-variance effect-size explanation never reached a
      reader, on exactly the cases they exist for), and `activation_gate` avoids it only by
      appending before its return. Mechanically detectable in principle — flag a `X.append(...)`
      on a name previously passed as a constructor argument in the same function scope — but the
      alias analysis to do it without false positives (the list may legitimately be rebuilt,
      reassigned, or passed by `model_copy`) is not a 30-minute rule, and a noisy version of this
      one would be ignored. — caught in the optimize-gate v8/v2/v3/v5/v1/v4/v6 review.
- [ ] A prose surface must not restate a formula `src/` owns. The two optimize surfaces already
      have sensors forbidding rendered CONSTANTS (`MATERIALITY_FLOOR`, `GATE_RESAMPLES`,
      `DEFAULT_ALPHA`), but nothing stops a closed form being retyped into a paragraph — and one
      was, with a wrong factor, in the same change (`2*(1-R/M)^M` and its limit). It is the
      CE037/CE040 class one level up, in prose. A narrow `^M`-shaped detector is cheap but would
      claim more generality than it has; defining "a formula" precisely enough to gate on is the
      part that is not cheap. CE039 does not reach it: that rule covers arithmetic-bearing
      TABLES, and this was a sentence. — caught in the same review.
- [ ] A helper that takes caller-supplied keys must not index its own mappings directly.
      `cost_latency_guardrails` did `rows[rid]`, which was safe while its only caller passed the
      intersection of those maps — and became a `KeyError` the moment a second caller
      (`execution_gate`) passed row ids derived from `experiment.json` instead. The general shape
      is "a parameter documented as caller-supplied is used as a subscript into a parameter
      documented as the callee's own data", which needs the two to be related by more than types;
      an AST rule would either miss it or flag every legitimate lookup. — caught in the
      optimize-gate v8/v2/v3/v5/v1/v4/v6 final review.
- [ ] Two code paths computing the SAME metric over the same rows must apply the same
      normalization. `activation_gate` balances per-row replicate counts before pooling for the
      primary criterion — with a six-line comment about why — while `_sibling_checks` pooled raw,
      so byte-identical arms differed by 0.1 of recall on a check that gates `promoted`. Detecting
      "these two call sites should share a preprocessing step" is a semantic claim about intent,
      not a pattern; the realistic guard is a unit test per metric asserting invariance to
      replicate imbalance, which is what was added here. — caught in the same review.
- [ ] A prose surface's claim about a NUMERIC CONSTANT in the code must be checked by reading the
      constant. `SKILL.md` shipped "`failed_samples[]` is capped, so it will not hand you fifteen"
      while `_FAILED_SAMPLE_LIMIT = 20` — the cap is *larger* than the number the sentence budgets
      against, so the stated consequence was the reverse of the real one. CE039 cannot reach it:
      that rule covers arithmetic-bearing TABLES, and this was a sentence naming a bare number.
      A `ComputedClaim` could bind this one instance, but the general rule ("every number in these
      surfaces that shadows a constant is derived from it") needs a way to know WHICH constant a
      given number refers to, which is the part that is not cheap. — caught in the ReAPO
      optimize-skill final review, by a fact-checker that ran the code rather than read it.
- [ ] The exempt-locator list is criterion-agnostic and is now read by a second consumer pointing
      the other way. `LEAK_LOCATOR_FIELDS` omits `llm_judge.files` / `agent_judge.files`,
      `cli_called.log` and `uipath_eval.eval_set`, all locators by the module's own definition.
      For CE036 that is pre-existing scope; for `candidate_leaks` it is a NEW false-positive
      channel in a checker whose whole design rationale is not firing more than it has to (a body
      that names its own output path gets flagged). Not done here because widening the list also
      weakens the shipped CE036 rule and changes its derived CLAUDE.md sentence — a separate
      decision, not a refactor. The mechanical half is easy: derive the list from every criterion
      field whose name matches a locator vocabulary, and fail when a criterion grows a
      location-shaped field nobody classified. — caught in the same review.
- [x] **CLOSED.** The search loop's accept/revert arithmetic lived in a markdown snippet rather
      than a tested function, against `models/optimize.py`'s own stated principle ("the gate's
      verdict is a typed value the skill prints, instead of arithmetic an agent performs by hand").
      Now `optimize.search.search_compare` + `lineage_head_scores` + `render_search_comparison`, with
      18 unit tests: the four guards (shared-row intersection, no-overlap-before-holes, refuse on a
      hole, corpus regression blocks an accept) are asserted rather than copied. The deferral
      reasoning — that every snippet in this skill is hand-written the same way — was right about
      the general case and wrong about this one: these four guards are the only ones whose omission
      is *silent and score-changing*, which is what makes them worth the API. — caught in the ReAPO
      optimize-skill Phase 1 quality review, closed the same day.
- [x] **CLOSED.** `model_copy(update={...})` is not validated by pydantic even under
      `extra="forbid"`, so a mistyped key is set as a bare instance attribute and dropped from
      `model_dump()` entirely —
      no raise, no log, and the field it was meant to set stays at its default. This is the exact
      hole CE041 closed for *construction*, still open for *update*, and it matters most where it
      is worst: `ActivationGateVerdict.promoted` / `holm_alpha` and their execution-track twins are
      written ONLY this way (`optimize.activation.holm_promote`, `holm_promote_execution`), so the two
      fields that ARE the promotion decision are the two the runtime backstop does not cover. Not
      done with CE041 because the fix is not a matching rule: `update=` legitimately takes a dict
      in every one of this repo's ~9 call sites, so a rule that flags the CALL is wrong and one
      that validates the KEYS has to resolve the receiver's model type, which an AST walk cannot do
      in general. The tractable shapes are a runtime helper (`copy_with(model, **kwargs)` taking
      literal keywords, plus a rule forbidding bare `model_copy(update=)`) or a
      `model_validate(instance.model_dump() | update)` convention — a design decision, not a
      bolt-on. — caught in the Plan A Phase 3 quality review; CE041's docstring points here.
      **Resolution (Plan D Phase 3):** the helper, not the convention. `models/copy_with.py`
      takes literal keywords and raises on an unknown field name; **CE048** forbids the bare call
      shape in `src/`; all 21 call sites converted, with one documented exemption
      (`criteria/agent_judge.py` — a dict VARIABLE from user YAML, plus `deep=True`; **that
      exemption was retired in Plan A Phase 2** and `src/` now holds exactly one live
      `model_copy(update=)`, the helper's own). The
      re-validating convention was rejected: it re-runs every validator on every field on paths
      that run once per candidate per round. Note the inventory here said "~9 call sites" and the
      plan's single-line grep said 17 — the AST count is **22**, because four are wrapped across
      lines, one of them `holm_promote`'s main promotion write. The reasoning above that "a rule
      that flags the CALL is wrong" did not survive contact either: 21 of the 22 sites needed no
      dict at all, so flagging the call shape and routing every author to a validating helper is
      exactly what CE048 does, with one documented exemption.
- [x] **REFUTED, not built — a SHA-pinning rule over `templates/**/.github/workflows/`.** A review
      raised it on the premise that *"users copy this template into their own repos"*, which would
      make a floating action tag a supply-chain exposure. The premise is false, and the file's own
      header says so: `templates/ci-outcome-fixture/.github/workflows/lint.yml` is a **graded eval
      fixture** mounted into a sandbox for the `ci` skill's outcome suite — deliberately
      "unrelated to evaluation", present only so `ci` does not take its no-`.github/` branch.
      GitHub never runs it, no user copies it, and a floating tag therefore has no effect at all.
      Worse, editing it perturbs the fixture the `ci` skill is *scored on*, so the "fix" would move
      an A/B baseline. One file, zero exposure. Recorded here so the next review does not re-raise
      it. — checked and refuted in the Plan D scoping pass, confirmed against the file at
      implementation time.
- [ ] **`estimator_ledger.WATCHED_CONSTANTS` is one-directional — nothing forces a statistical
      constant onto the list.** It shipped with the gap already LIVE, not merely prospective: the
      final review found `optimize_execution.FLOOR_RESOLUTION` and `NEAR_FLOOR_MULTIPLE` unwatched in a
      file four of whose constants were, and both move rendered output (the first decides whether
      an MDE is measurable at all, and therefore whether the execution gate REFUSES). They are
      watched now; the class is not closed. Both watch lists have anti-rename parity tests (a renamed constant
      or a moved fixture directory would make the job match nothing and pass silently), but a
      newly introduced resample count, alpha or tolerance is unwatched by default, and deleting a
      tuple entry passes both `make test` and the job. CE039 solved exactly this rot for prose
      tables with a COVERAGE check — a table no claim names is a failure — and the same shape
      belongs here: "a module-level constant that looks statistical and is not watched is a
      failure". Not built with the protocol because "looks statistical" needs a design pass (a
      name heuristic? a `Final[float]` in two named modules? an explicit opt-out list?), and a
      noisy version of this rule in a merge-blocking job is worse than none. — caught in the
      Plan D Phase 6 quality review.
- [x] **`agent_judge` silently drops an off-kind judge config's fields.** `_build_agent_config`
      copies the user's `criterion.agent` dump onto a `ClaudeCodeAgentConfig`, but
      `AgentJudgeCriterion.agent` is the four-way `AgentConfig` union — so `type: antigravity`
      supplies `thinking_level`, which `ClaudeCodeAgentConfig` does not declare. Verified: it lands
      as a bare instance attribute, absent from `model_dump()`, and the same call writes
      `type="antigravity"` onto a claude-code model unvalidated. This is exactly the key hole
      `copy_with` closes, at the one site CE048 exempts — the exemption is legitimate (a dict
      variable plus `deep=True`) but it is NOT harmless, and the comment there now says so instead
      of claiming the hole cannot open. Not fixed with CE048 because the fix is a behaviour
      decision — reject an off-kind judge config, or coerce it to the judge's own kind — rather
      than a mechanical conversion, and either answer changes what a currently-accepted task YAML
      does. — caught in the Plan D Phase 3 quality review.
      **Resolution (Plan A Phase 2): REJECT.** `AgentJudgeCriterion.agent` is narrowed from the
      four-way union to `ClaudeCodeAgentConfig`, so an off-kind block is a `ValidationError` at
      task load rather than a silent coercion. Coercion was rejected because the offending field
      is meaningless on the target model — there is nothing to coerce `thinking_level` INTO, and
      `model: gemini-3` reaching the Claude SDK is not a config to repair. Blast radius measured
      as zero: every in-tree `agent:` block under an `agent_judge` criterion already spells
      `type: "claude-code"`, and all 46 task YAMLs load under the narrowed schema. Out-of-tree
      YAML carrying a non-Claude judge block was already silently broken; it now fails loudly.
      The overlay at the same site moved to
      `ClaudeCodeAgentConfig.model_validate({**defaults.model_dump(), **user_overrides})`,
      retiring the tree's last `# noqa: CE048`.
- [ ] A no-op "absence" assertion: `assert "X" not in text.replace("NOT X", "")` is vacuous whenever
      the fixture cannot contain `X` at all, and reads as a strong guard. The named instance —
      `tests/test_optimize_gate.py`'s `render_search_comparison` blocked-path test — **is FIXED**
      (Plan D Phase 4: it now asserts `block.splitlines()[0] == "### Search round — CANNOT
      COMPARE"`, and a sibling pins `DO NOT ACCEPT` as PRESENT on the input that produces it), so
      do not go looking for a live example; the general RULE is what remains deferred. The correct form is to read the discriminating
      LINE — `_headline()` in that file is the worked example, and it proves itself non-vacuous by
      rendering a fixture where the forbidden string DOES appear. Not done here because catching it
      mechanically means an AST rule over `tests/` matching a `Compare(NotIn)` whose right operand is
      a `.replace()` call whose first argument CONTAINS the left operand, plus a repo-wide sweep of
      the hits before it can land green. — caught in the Plan A Phase 1 review.
- [ ] An `if` whose body is only `notes.append(...)` sitting beside a structurally identical `if`
      that ends in `return` — the shape of the incumbent-variant fall-through this plan fixed, where
      one validation branch failed open while its sibling three lines above failed closed. AST-
      detectable within a single function body (same-parent `If` nodes, one terminating in `Return`,
      one not, both guarding a comparable predicate). Not done with the fix because landing it green
      needs a sweep of every multi-branch validator in `src/` to separate the real fall-throughs
      from the deliberate accumulate-then-continue ones — the same reason the
      OSError-in-a-`try` rule was deferred. (That entry once reserved the number "CE046"; Plan D
      Phase 2 spent CE046 on the CLI-flag documentation rule, so the number here would now point at
      a shipped, unrelated rule. Numbers reserved in this file are enforced nowhere —
      `runner.py`'s uniqueness assert covers `ALL_RULES` only.) — caught in the Plan A Phase 2
      review.
- [ ] A duplicated long prose literal (≥60 chars) across two functions. Phase 3 of the optimize-gate
      module split collapsed four such strings — the notes both Holm wrappers emit, which sat 600
      lines apart as byte-identical copies, two of them wrapped differently in source while producing
      the same string. A wording fix applied to one would have left the two tracks describing the
      same decision differently in a ledger read back weeks later. The interim guard is
      `test_neither_wrapper_respells_a_shared_note` in `tests/test_optimize_gate.py`, which pins
      those four strings only. The general rule is **Plan D's proposed CE049** and is deliberately
      not built here: unlike CE042 (a one-allowed-site seam rule copied wholesale from CE040), this
      is a heuristic whole-tree rule needing its own design pass — a length threshold, a
      normalisation for source wrapping, and an allowlist sweep before it can land green. Recorded
      here because Plan D lives in an untracked planning file and this is the committed surface. —
      caught in the optimize-gate module-split run, Phase 3. **A second real instance, Plan D
      Phase 4:** `early_stop.py` stated the ToolStart decidable-seam claim in BOTH its module
      docstring and `_on_event_impl`'s, so correcting one left the other contradicting it — and the
      module copy survived the grep that found the method copy only because it wrapped mid-phrase.
      A length-and-wrapping-tolerant rule would have caught exactly that.
- [ ] A raw-substring prose sensor firing on its own documentation. `test_module_imports_no_cli_machinery`
      scans module source for banned tokens (`import typer`, `coder_eval.cli`, …); the new
      `reports_optimize.py` tripped it by *documenting* that it imports no such module. The instance
      was fixed by rewording (and saying why in the docstring), but the class is live for every
      substring-scanning sensor in `tests/test_custom_lint.py` — the same fragility CE039 exists to
      discourage for arithmetic claims. A real guard means parsing rather than scanning: check
      `ast.Import`/`ast.ImportFrom` nodes instead of text, which is a sweep of every such sensor and
      a decision about the ones that legitimately scan prose. — caught in the optimize-gate
      module-split run, Phase 6.

- [ ] **`fingerprint_diff` cannot see a config key that MOVED, only one that changed value.**
      `compute_run_fingerprint` dumps the whole `BatchRunConfig`, and `fingerprint_diff` compares
      only keys present in BOTH stamps — so when Phase 2 collapsed the three flat selector fields
      into one nested `row_selection`, a `--resume` into a run dir stamped before the change
      silently skipped the config-drift warning instead of reporting it. Verified: a prior stamp
      with `"split": "train"` against a current stamp with `row_selection: {"split": "test"}`
      yields `{}`. Not a correctness break (resume matches on `id_field`-derived row ids, and the
      warning is informational) and it self-heals after one run, so it was not worth blocking on.
      A real guard is a rule that flags a key present in exactly one of the two fingerprint
      schemas — which needs a notion of the PREVIOUS schema that nothing in the tree currently
      carries, so it is a design question rather than a test. Note also that nothing pins that a
      changed `row_selection` surfaces in the diff at all. — caught in the row-selection
      integrity run, Phase 2 review.

- [ ] **The cross-split gate refusal compares `--split` only, not the samplers.** `run.json`
      records `max_rows` and `sample_per_stratum` beside `split`, and `read_split_provenance`
      reads none of them. A `--sample` draw is fixed-seed, so two arms run at DIFFERENT counts
      score largely disjoint rows (`random.sample` with a different `k` is not a prefix) and the
      preflight passes silently — the same failure the refusal exists for, one field over. It is
      narrower than the split case: a sampler mismatch surfaces downstream as a small
      `rows_paired` beside a large `rows_excluded`, which the verdict already reports, whereas a
      split mismatch can leave both arms fully paired on rows that merely share ids. Widening the
      comparison is a behaviour change beyond what the preflight was scoped to, and it needs a
      decision about whether an intentional `--sample` difference should ever be gateable at all.
      The message and a comment beside the check now state the scope so it is at least not a
      false claim. — caught in the row-selection integrity run, final cross-phase review.

- [ ] **Nothing PREVENTS a run dir from accumulating rows across invocations — the gate only
      refuses afterwards.** `coder-eval run --run-dir <existing dir>` writes into the tree without
      noticing that a previous invocation's `<row>/<NN>/task.json` are still there, and rewrites
      `run.json`'s `row_selection` to describe only the current call. `activation_gate` now
      reconciles the tree against `run.json` and refuses (`reconcile_tree_against_run_json`), but
      that is detection at the far end: the user has already paid for both runs, and a run dir
      *already* contaminated before the check landed stays unusable rather than being repaired.
      The prevention half is a behaviour change to the primary entry point, which is why it was
      left out: either `run` refuses a non-empty `--run-dir` unless resuming, or every `task.json`
      stamps the `--split` (and sampler) that produced it, so a row carries its own provenance and
      no reconciliation against a per-invocation artifact is needed at all. The second is the
      better shape and the larger change — `EvaluationResult` would gain a field, and every
      reader of a run dir could then answer "which selection produced this row?" directly.
      — caught in the top-10 review-fixes run, Phase 4.

- [ ] **The eight CE041 splat sites are exempted, not converted.** CE041 now fires (it never had
      — it reported 0 against 8 real model-constructor splats until `resolved_module` landed), and
      all eight carry a reasoned `# noqa: CE041` rather than the `Model.model_validate(payload)`
      the rule's message asks for. That was a deliberate scope call: converting them changes the
      raised exception type on YAML-parsing paths, so each call site's `except` clause and
      user-facing message has to be traced — `task_loader.py:85` in particular is wrapped in a
      handler producing `ValueError: Invalid task definition: ...` that other code and tests
      depend on. What makes the exemptions honest rather than a dodge, and what a converter must
      re-check: `ExperimentDefinition`, `SimulationConfig`, `RunLimits`, `SandboxConfig` and the
      agent configs all declare `extra="forbid"`, so a mistyped key RAISES there today rather than
      landing at a default; `TaskDefinition` is the one that does not (its top-level schema is in
      soft launch) but it emits `UnknownTaskFieldWarning` via `_warn_on_unknown_fields`, which
      `coder-eval plan` renders inline. So no site is silently wrong — the noqas give up the
      STATIC half of the guard only. Sites: `cli/evaluate_command.py`, `orchestration/config_merge.py`
      (x3), `orchestration/experiment.py` (x2), `orchestration/task_loader.py` (x2).
      — caught in the top-10 review-fixes run, Phase 6.

- [ ] **Ratchet ruff `C90` down from 30 toward 20, one function at a time.** `C90` is now enabled
      at `max-complexity = 30` — one above the worst function in the tree — so it costs no
      refactor and still fails a NEW god-function. It is a floor under the debt, not a fix for it.
      Four functions sit above 20 and each needs its own decomposition and review:
      `isolation/docker_runner.py::_build_argv` (29), `reports_experiment.py::generate_variant_report`
      (24), `orchestrator.py::_simulation_dialog_loop` (22),
      `agents/claude_code_agent.py::communicate` (21). Lower the ceiling by one step per landed
      refactor; do NOT add a `per-file-ignores` entry instead — a second entry in that list means
      the ceiling is wrong, not that a file is special. Note the plan that introduced `C90` assumed
      `optimize_gate.py` would need the single exemption. It does not — but NOT because the module
      split landed: that is still undone (3250 lines, `radon cc` reports one E-grade and seven
      D-grade functions). Its mccabe peak is 17 (`execution_gate`), unchanged since `fa69cdf`, so
      mccabe and radon disagree about that file and only the ceiling of 30 is why no exemption
      exists. The module split is still owed.
      — caught in the top-10 review-fixes run, Phase 7.

- [x] **CE050, CE051 and CE052 are TAKEN** by the top-10 review-fixes run: CE050 (escape untrusted text in a Rich-markup `console.print` under `cli/`), CE051 (a lint rule matching an import's module string must route through `tests/lint/import_resolution.py::resolved_module`), CE052 (every task YAML under `templates/` must load through the real `load_task`). CE049 remains reserved by the backlog entry above. The next free id is **CE053** — but re-run `grep -rhoE "CE0[0-9][0-9]" tests/ .claude/ src/ docs/ pyproject.toml | sort -u` before claiming it: `tests/lint/runner.py`'s uniqueness assert covers `ALL_RULES` only, so a class-wired id (CE052 is one) can collide with a `BaseRule`'s without failing anything.

- [ ] **CE025 pins that a live criterion DECLARES its polarities, not that its checker AGREES
      with the declaration.** `TestCE025LiveVerdictConsistency` asserts a `LiveSuccessCriterion`
      model has a checker overriding `live_verdict` and vice versa; nothing checks that a verdict
      the checker actually RETURNS is one `live_decidable_polarities()` declared. A checker
      returning `"fail"` while its model declares `{"pass"}` produces a verdict that
      `EarlyStopWatcher._evaluate_impl` classifies as neither a native fail (needs
      `_fail_trigger[i]`, which is False) nor a budget fail (needs `_budget_expired[i]`) — so a
      definitively-failed armed criterion whose ceiling is below threshold fires **no stop at
      all**. Reproduced during the Phase 1 review. Pre-existing, not a regression: the
      `_budget_drove` arm deleted in Phase 1 never rescued the latched case either, and the
      unlatched case it did rescue is unreachable (an armed budget and a declarable live-fail are
      mutually exclusive). Recorded in `_budget_drove`'s docstring rather than papered over.
      NOT cheap: agreement is a BEHAVIOURAL property — you have to exercise each checker over
      inputs and compare the returned polarity against the declaration — so it is a property test
      over the criterion registry, not an AST rule, and it needs a decision about what input
      corpus is representative. — caught in the top-10 review-fixes run, Phase 1 review.

- [ ] **Nothing requires a new CExxx rule to ship with a test proving it FIRES.** Three guards
      written during this run were themselves broken in the fail-open direction and only found by
      review: CE051's docstring exclusion compared `ast.get_docstring`'s *normalised* text against
      raw `Constant.value` (so it excluded nothing), CE051's GAP violation was anchored on the
      Module node (line 0, which no `# noqa` can ever suppress), and `resolved_module` FABRICATED
      module paths for files outside `src/coder_eval` instead of returning `None`. All three
      looked green. The existing convention — a positive fixture per rule — is real but
      unenforced, and a rule shipped with only negative fixtures is indistinguishable from a
      working one. A mechanical version ("every `ALL_RULES` member is constructed somewhere in
      `tests/test_custom_lint.py`") is cheap but would not have caught any of the three, since all
      were constructed. The version that WOULD catch them — "at least one test per rule asserts a
      NON-EMPTY violation list" — needs a reliable way to tie a test function to the rule it
      exercises and to recognise a non-emptiness assertion, which is heuristic enough to deserve
      its own design pass. — caught in the top-10 review-fixes run, Phase 6 review.

- [x] **The Stage A / floor surfaces read the run-dir tree without reconciling it.**
      `activation_gate` and `execution_gate` both refuse a run dir holding results its own
      `run.json` never wrote (`reconcile_tree_against_run_json`). `measure_noise_floor`,
      `noise_floor_mde`, `arm_row_scores` and `cost_quality_points` do not — measured, on
      contaminated dirs that `activation_gate` correctly refuses, `measure_noise_floor` returned a
      floor computed over an extra pooled row and `arm_row_scores` returned the stale row in its
      vector. They are not gates, which is why this was left: each returns a float or a list with
      nowhere to put a refusal, so closing it needs a decision about the return contract (log and
      degrade? an optional strict flag? a `Reconciliation` alongside the value?) rather than a
      copy of the preflight. The floors feed the MDE a gate reports and the vectors feed the three
      Pareto fronts, so a wrong number here is not cosmetic. — caught in the top-10 review-fixes
      run, final adversarial review.

      **Resolution (Plan B+C Phase 1):** closed, and the return-contract question resolved by
      SPLITTING on it rather than picking one answer. The DETECTION is shared —
      `optimize.load._reconcile_arms` is the one sweep every whole-arm reader routes through
      (`execution_gate` still calls `reconcile_tree_against_run_json` directly — it works one run
      dir per variant and needs the per-dir result), and `_stale_tree_reason` is the one message
      the readers share — while the RESPONSE follows the return type:
      `measure_noise_floor` and `measure_execution_noise_floor` return `None` through the existing
      `_no_floor` channel, and `arm_row_scores` logs a WARNING and returns its vector, because
      `ArmRowScores` has no field a refusal could live in. A shared `_refuse_or_warn` helper was
      considered and rejected — it would take a mode flag, which is two functions in a trench coat.
      Two readers deliberately do NOT reconcile and carry a reasoned `# noqa: CE053` naming who
      reconciles for them: `_load_and_pair` (its only caller `activation_gate` sweeps both arms
      first) and `cost_quality_points` (it reaches the tree through `arm_row_scores`, so a second
      sweep would read every `run.json` twice per arm and warn twice about one fault). The
      correction the entry itself needed: the four readers are `measure_noise_floor`,
      **`measure_execution_noise_floor`**, `arm_row_scores` and `cost_quality_points` —
      `noise_floor_mde` reaches the tree only through `measure_noise_floor`. **CE053** is the
      standing guard, and its path scope is the `optimize/` DIRECTORY rather than a list of module
      names (it was the `optimize_*` filename prefix until the family became a package), so moving a
      reader — or adding a module — cannot take it out of reach.

- [ ] **A dotted `coder_eval.<module>.<name>` reference in PROSE is unchecked.** The snippet
      sensor (`tests/test_custom_lint.py::_snippet_binding_failures`) resolves imports inside
      ` ```python ` fences only. The six-module split (Plan B+C Phase 7) left **fourteen** files
      naming a moved symbol by its old dotted path — including
      `plugins/coder-eval/reference/proposal-prompt.md`, which told the proposer to call
      `coder_eval.optimize_gate.candidate_leaks(...)` (now `optimize.search`, so the import
      raises), and two Pydantic FIELD DESCRIPTIONS, which are public model documentation. All
      were found by a reviewer reading files, not by any sensor. The guard is a scan for
      `coder_eval\.[\w.]+\.[A-Za-z_]\w*` across `src/`, `docs/`, `plugins/` and `.claude/`,
      resolving each through `importlib` — the same existence check the fence sensor already
      does, one syntax over. Not built here because the reference forms vary (`:func:` roles,
      backticked prose, bare dotted paths) and deciding which are claims about the API versus
      incidental mentions needs a design pass. — caught in the Plan B+C final cross-phase review.

- [ ] **A contaminated `ArmRowScores` vector is persisted with no marker.** `arm_row_scores` warns
      to stderr and returns its vector (Plan B+C Phase 1 — `ArmRowScores` has nowhere to put a
      refusal). `record_round_scores` then writes that vector into `measurements.json`, and
      `lineage_head_scores` reads it back rounds later, when the run dirs may be gone and the
      warning is long out of scrollback. Nothing marks the stored vector and `render_row_matrix`
      prints no footnote, so the only trace of contamination is a stderr line from the snippet that
      produced it. The plan's B1 table asserts "no field can carry a refusal"; that is true only
      because no field was added — a defaulted `stale: bool = False` on `ArmRowScores` is additive
      under `extra="forbid"`. Not done here because it ripples into `RoundScores`,
      `record_round_scores` and the matrix footnotes, which is a scoped change rather than a guard.
      — caught in the Plan B+C Phase 1 quality review.

- [ ] **CE055: a module-size / complexity ratchet over the optimize family.** The six-module split
      landed (Plan B+C Phase 7), and this is the baseline it would ratchet against, measured at
      that commit:

      | module | lines | D-grade functions |
      |---|---|---|
      | `optimize_load.py` | 713 | `_load_and_pair` D(30) |
      | `optimize_gate.py` | 374 | `cost_latency_guardrails` D(24) |
      | `optimize_activation.py` | 1159 | `_sibling_checks` D(23), `activation_gate` D(23) |
      | `optimize_execution.py` | 1033 | `execution_gate` D(30), `_execution_diagnostics` D(29), `holm_promote_execution` D(21) |
      | `optimize_fronts.py` | 327 | none |
      | `optimize_search.py` | 286 | none |

      **No E-grade function anywhere in the family**, down from one file of 3,664 lines with one
      E-grade and seven D-grades. Not built here because a ratchet needs a checked-in baseline
      (this table) plus a design pass on what cap a NEW function gets — the existing `C90`
      ceiling is a mccabe number and radon disagrees with it (mccabe peaks at 17 on
      `execution_gate` where radon reports D(30)), so a ratchet has to pick one metric and say
      why. Deferred to Plan D, which is where the decision belongs.

- [ ] **The execution track's "the floor came back unavailable" advisory names no cause.**
      `activation_gate` now threads the real reason out of `measure_noise_floor` through a
      `reasons` sink, so its MDE note says WHICH of five causes fired instead of naming one
      unconditionally (Plan B+C Phase 3). `_execution_diagnostics`' twin advisory
      ("this suite's minimum detectable effect came back unavailable / 0.000") still names none,
      because `measure_execution_noise_floor` does not thread the sink — the parameter was added
      and then removed, since nothing called it and speculative surface is worse than a recorded
      gap. Closing it is the same shape as the activation side: forward `reasons` from
      `measure_execution_noise_floor`, collect it in `execution_gate`, and pass it into
      `_execution_diagnostics` beside `mde`. Not done there because that advisory's text is pinned
      in `optimize_verdicts/execution_gate.json`, so it needs a fixture regeneration and an
      estimator-ledger row of its own. — caught in the Plan B+C Phase 3 quality review.

## From 2026-08-16 xlsx execution-track dogfood run

First real `/coder-eval:optimize-skill` execution round against a THIRD-PARTY skill
(Anthropic's `xlsx`, 24-row outcome suite). Working tree: `tmp/xlsx-opt/`, plan and run
log in `c/2026-08-16-optimize-public-skill-blog.md`. Findings 1, 2 and 4 are defects in
**shipped guidance**, not in the experiment.

- [ ] **The engagement criterion halves every effect the execution gate measures.** The
  bundled `reference/templates/outcome.yaml` stacks `skill_triggered` on every row at the
  default `weight: 1.0`. That criterion scores exactly 1.0 on every row *by design* — it is a
  gate, and the same skill requires `recall.yes: 1.0`. `weighted_score` averages it with the
  real grader, so a measured 0.16 difference reaches `execution_gate`'s paired *t* as 0.08.
  Nothing in `optimize-skill` or the template mentions it, and the template's own recommended
  shape is what causes it. Measured on the xlsx suite: a row grading 0.857 reports 0.929.
  Fix is guidance, not code: the template should set a near-zero weight and say why.
- [ ] **...and the obvious fix is rejected by a validator, so two shipped instructions
  conflict.** `weight: 0` raises "weight=0 makes the criterion informational (non-gating), so
  it cannot also set suite_thresholds". The template tells you to gate on `recall.yes` AND the
  method tells you the gate compares `weighted_score` — you cannot satisfy both cleanly.
  Worked around with `weight: 0.05` (~5% shrinkage rather than ~50%). Decide which of the two
  gives: either the validator learns that a zero-weight criterion may still carry
  `suite_thresholds`, or the template stops stacking a constant-scoring criterion into the
  compared statistic and documents the near-zero weight instead.
- [x] **DONE (2026-08-17, outcome-suite-mode plan Phase 4): No preflight on whether the INSTRUMENT
  is fair.** Shipped as `/coder-eval:task` **Step 6.5** — build a known-good and a known-bad
  artifact, grade both, report the SEPARATION MARGIN, before any stage is paid for — with the three
  fairness questions consolidated into `reference/task-rubric.md` § "Grader fairness" (one
  declaration; `task` and `optimize-skill` both point at it) and guarded by
  `test_task_skill_has_discrimination_gate` / `test_grader_fairness_is_declared_once`. The original
  entry follows. `optimize-skill` hard-gates
  engagement but has nothing on "is the grader discriminating and unbiased". Two checks in
  this run's grader were wrong in ways only the baseline could reveal: one penalised a
  legitimate alternative implementation (nested `IF` where the body never mandates `IFS`), one
  double-charged a single mistake (a workbook with no formulas failed both "use formulas" and
  "recalculate"). **Both biased every arm equally**, so no cross-arm comparison could ever have
  surfaced them — the same blind spot `skill-creator`'s analyzer calls a "non-discriminating
  assertion". Candidate: a Step 6.5 that requires grading a known-good and known-bad artifact
  and asserting the scores separate, before any stage is paid for.
- [ ] **Answer-key leakage into the fixture is unguarded, and measurably inflates scores.**
  `candidate_leaks` checks whether a CANDIDATE reproduces train-row text; nothing checks
  whether the FIXTURE ships the grader's expectations into the sandbox. Reproduced
  accidentally here (a row generator wrote `expectations/*.json` under the fixture root, which
  is copied into every sandbox). Measured against a clean run on the same 11 rows:
  **mean 0.9158 clean -> 0.9461 leaked**, two rows flipping partial->perfect. That is larger
  than most effects the gate exists to detect. Candidate: a lint/preflight that fails when a
  `run_command` criterion's script or data lives under a `template_dir` the sandbox mounts.
  **RESERVED AS CE056 (see the entry at the end of this file), and partially closed:** the shipped
  layout is asserted by `TestPluginArtifacts::test_outcome_grader_lives_outside_any_mounted_fixture`
  and the grader template now addresses its script through `$TASK_DIR`, beside the suite rather than
  inside the fixture. What remains un-guarded — and what CE056's promotion trigger waits for — is
  the general tree-walking rule, which today has one discoverable subject and would pass vacuously.
- [ ] **`--sample 1` is the missing cheap preflight.** One row for ~$0.50 proved the whole
  pipeline (skill engages, artifact lands, grader scores) before any stage. `optimize-skill`
  Step 6 goes straight to a full baseline. Pure guidance fix, one sentence.
- [ ] **`plan --split` is a capability the version string does not carry.** The PATH binary and
  the tree-local editable install BOTH report `0.9.6`; only the second accepts `plan --split`.
  Step 1 already warns about this in prose and it still cost a cycle — the check it describes
  should be a copy-pasteable command in the skill rather than a paragraph.

## From Plan A (scoring-correctness fixes, 2026-08-16)

- [ ] **`agent_judge`'s `allowed_tools` and `ignore_patterns` are order-nondeterministic across
  processes.** `criteria/agent_judge.py` builds both through a set literal
  (`list({*config.allowed_tools, SUBMIT_VERDICT_MCP_TOOL_NAME})`, and the same shape for the
  ignore-patterns floor), and Python's string hash randomization reorders a set per interpreter
  run. Measured three consecutive runs of the same config: `['mcp__…', 'Glob', 'Bash', 'Grep',
  'Read']`, `['Glob', 'Grep', 'mcp__…', 'Bash', 'Read']`, `['Glob', 'Bash', 'Grep', 'mcp__…',
  'Read']`. Behaviourally harmless — both are membership sets downstream, and neither affects
  cost or scoring — but it has two real costs: **any test asserting LIST equality on either is
  flaky in CI** (Plan A's own prescribed test would have been, and now compares as a `set`), and
  the persisted `agent_config` dump in `task.json` differs run-to-run for byte-identical config,
  which is noise in any artifact diff. The fix is one `sorted(...)` per line. Not taken in Plan A
  because it changes the bytes of a persisted artifact, which is a different decision from a
  scoring fix and does not belong in the same commit. A sensor is also available and is the
  cheaper half: a test asserting no test in the tree compares `allowed_tools`/`ignore_patterns`
  by list equality. — found by the Plan A spike (S6), recorded rather than smuggled in.
- [ ] **A `result_status` membership DENYLIST has no guard — CE018's shape, one field over.**
  `CommandTelemetry.result_status` classification is the `FinalStatus.name` problem in a
  different field, and CE018 already forbids the denylist form there. Not built alongside
  **CE054** (which confines the comparison to one site per criterion module, and IS built)
  because the two catch different things and only one of them was the bug: the drift that
  produced the false positive was between an ALLOWLIST (`!= "success"`) and a DENYLIST
  (`in ("error", None)`), so a denylist rule catches one of the two sites and the seam rule
  catches the pair. Independently, `result_status` is a closed
  `Literal["success","error","unknown"] | None`, so CE018's actual motivating failure — a new
  enum member silently falling through a stale denylist — requires a model edit a reviewer
  sees, which is a much weaker case than CE018's own. It would nonetheless be cheap and
  NON-NOISY: after Phase 1 there is no `result_status` membership test left anywhere in `src/`,
  and the adjacent shapes are safe by construction (`command_executed.py` and
  `reports_html.py` use `!= "success"` / `== "success"`, `analysis.py` uses `==`/`is None`
  chains to bucket report counts). The rule must match the `in` / `not in` form ONLY — a
  broader "any comparison against a non-`success` literal" version fires on `analysis.py` x3
  and `codex_agent.py` and should not be built. — raised by the Plan A final review
  (multi-model + Opus, both independently).

- [ ] **CE056 (RESERVED): a grader's answer key must never ship inside a mounted fixture.**
      (The same rule as the "Answer-key leakage into the fixture is unguarded" entry above, which
      states the measurement in its original context; this entry is the ID reservation and the
      promotion trigger. Do not treat them as two candidates.)
      Everything under a `template_dir` is copied into every sandbox, so a `run_command` grader's
      `expectations/` placed there hands the agent exactly what it is being marked against — and
      the run looks completely normal. Measured on a real suite when it happened by accident: on
      the same 11 rows the mean went **0.9158 clean -> 0.9461 leaked**, two rows flipping from
      partial to perfect. That is larger than most effects an optimization round exists to detect,
      and it inflates **every arm**, so no cross-arm comparison can reveal it.
      `optimize.search.candidate_leaks` does not cover this — it asks whether a CANDIDATE
      reproduces train-row text, not whether the FIXTURE ships the marking scheme.

      **Not built now, deliberately: it would pass vacuously.** The only discoverable subject in
      the tree is the bundled `reference/templates/outcome.yaml`, whose mounted
      `./outcome-fixture` is a one-file placeholder — so a tree-walking rule would report clean
      whether or not it worked, which is the exact CE044/CE045 failure. The assertion exists
      instead as `TestPluginArtifacts::test_outcome_grader_lives_outside_any_mounted_fixture`,
      scoped to that one file by construction and carrying a non-empty-mount-set GAP check.

      **Promotion trigger:** a SECOND outcome suite with a `run_command` grader appears (in
      `tasks/`, `templates/`, or the plugin). Fold in the sibling rule rejected for the same
      one-subject reason at the same time — **a grader criterion must set
      `score_from_stdout: true`**, since a binary grader over a dozen-odd rows manufactures the
      execution gate's zero-variance refusal (today's single subject is pinned by
      `test_outcome_template_grader_slot_is_continuous`). — caught in the
      2026-08-17 outcome-suite-mode plan, Phase 5.

- [ ] **Nothing binds a shipped surface's PROSE cross-reference to the step it names.**
      `test_bundled_plugin_root_references_resolve` checks `${CLAUDE_PLUGIN_ROOT}` FILE paths in
      `.md` files only — it cannot see "see `/coder-eval:task` step 6.5", and it does not scan `.py`
      or `.json` surfaces at all. Four shipped files cited step 6.5 while it did not yet exist, and
      an author following the pointer would have found nothing at the one moment the safeguard
      matters. Closed for THIS reference by `test_shipped_surfaces_cite_a_step_that_exists`, which
      is bespoke: it hardcodes the four citing files and the string "step 6.5". The general rule —
      extract `<skill> step N.N` / `§ <heading>` references from every bundled surface and assert
      the target exists — is a Markdown-structure walk over headings, which is why it is deferred
      rather than written here. — caught in the 2026-08-17 outcome-suite-mode plan, Phase 3 review.

- [ ] **Nothing binds a CLI command's DESCRIPTION to what it does.** CE046 pins that every visible
      long flag appears in `docs/USER_GUIDE.md`, but nothing checks the prose around it. `plan`
      gained filesystem validation while the guide still called it "task syntax, required CLI
      tools, API keys, and schema validity" — and `check-skill/SKILL.md` has said "`plan` is a
      schema check only — it does not read the dataset file" since before the dataset preview
      landed, which was already false. Both were fixed by hand, twice, by reading. A rule would
      have to compare a docstring's claims with a command's behaviour, which is not mechanically
      decidable; the tractable subset is a sensor per claim, on the CE039 `ComputedClaim` model.
      — caught in the 2026-08-17 outcome-suite-mode plan, Phase 1 and final reviews.

- [ ] **Nothing checks that a CE039 `ComputedClaim` still fails on a MUTATED table.** The
      `headroom-ceiling` claim shipped able to pass over a table trimmed to a single row: every
      remaining cell recomputed correctly, so the check returned `[]` while the table said the
      opposite of what the claim exists to assert (deleting the one non-gap rule leaves "three of
      four were unpromotable" describing three rows that are all gaps). Fixed for that claim by
      asserting the rule set against the fixture and deriving the headline count from the cells —
      both bespoke. The general rule is a MUTATION test over the registry: for each claim, perturb
      each covered table (drop a body row, bump a numeric cell) and assert the check now fails.
      Deferred rather than written because the perturbation has to be claim-shaped to be fair — a
      dropped row is caught by the cost table's exact-label lookup and by the sizing table's
      per-row recompute, but the halving table iterates whatever rows it finds, so a naive
      row-drop mutation would report a gap in a pre-existing claim rather than in the harness, and
      deciding whether that gap is real is the actual work. `covers` already gives the rule its
      table set, so the registry half is free. — caught in the 2026-08-17 outcome-suite
      measurement-quality plan, Phase 3 review.

- [ ] **Nothing runs a shipped SKILL.md snippet, so a track-specific one can crash on the other
      track.** `_snippet_binding_failures` binds keyword arguments against the real signatures and
      the import sensor asserts every name exists — but neither executes anything, so Step 11's
      ledger snippet shipped calling `subprocess.run` on a grader the ACTIVATION track has no such
      thing as, in a file whose own prose calls the snippet a runnable continuation. Fixed by hand
      (the fingerprint half is commented out per track, as the floor already was). A real guard
      would execute each fence against a fixture run directory per track, which needs a fixture
      builder per snippet and a way to neutralise the paid calls — well past the promote threshold.
      The cheap subset, "a fence that names one track must not call anything unconditionally", is
      a heuristic that would fire on every correct block too. — caught in the 2026-08-17
      outcome-suite measurement-quality plan, Phase 5 review.

- [ ] **A CLI mode that bypasses a protocol can still fall through that protocol's error handler.**
      `verify.py --fingerprint` prints only a hash, but an exception inside it reached the
      always-exit-0 guard that exists to protect a computed SCORE — printing `0.0000\ngrader
      failed: …`, which `subprocess.run(check=True)` cannot catch and a caller records AS the
      fingerprint, so every later round reports a changed instrument from a permissions error.
      Fixed and regression-tested (`test_a_fingerprint_that_cannot_be_computed_exits_non_zero`).
      The general rule — every declared output mode has its own failure protocol, asserted — needs
      a machine-readable declaration of the modes, which this scaffold does not have and should not
      grow for one rule. — caught in the 2026-08-17 outcome-suite measurement-quality plan, Phase 5
      review.

- [ ] **A python fence in a shipped SKILL.md must be executable as written, not merely
      import-resolvable.** Step 11's ledger snippet shipped with `suite = suite_fingerprint(suite_task, …)`
      whose only assignment of `suite_task` was inside a COMMENT — a `NameError` in the user's
      terminal after the round is already paid for. The existing snippet sensor asserts every
      imported NAME resolves, which is satisfied here, and the prose-token sensors assert strings
      are present; neither can see an undefined local. The cheap version — `ast.parse` each fence
      and check every load is bound by an earlier store, an import, or a builtin — is defeated by
      the placeholders these snippets legitimately carry (`<runs>`, `<the suite yaml>`,
      `gate_dirs` carried from an earlier step), so it needs a per-fence declaration of what the
      step inherits before it can tell a placeholder from a bug. That declaration does not exist
      and should not be grown for one rule. Note this is the SECOND time a fence defect shipped
      past every sensor (see the entry above about executing fences per track), so the pair is
      the argument for building it properly rather than cheaply. — caught in the 2026-08-17
      optimize measurement-unit / confirm-gate plan, Phase 3 review.

- [ ] **A moved WATCHED constant leaves `docs/REPORT_SCHEMA.md`'s boundary paragraph attributing it
      to the old module, with every assertion green.** `FLOOR_RESOLUTION` moved from
      `optimize_execution` to `optimize_gate`; `WATCHED_CONSTANTS` was updated and both anti-rename
      parity tests passed, because `test_the_documented_watch_list_matches_the_code` matches the
      constant NAME only. A module check was written and then REMOVED for being unfailable: the
      section legitimately names an old module inside a ledger row describing the move, and every
      watched module is named somewhere in the section anyway, so neither a subset rule nor a
      proximity rule distinguishes a correct mention from a stale one. Doing it properly means
      parsing the boundary paragraph's `<module>'s <A> / <B>` structure — a one-paragraph grammar,
      which is more machinery than the defect (a stale attribution in prose) justifies today. The
      limitation is stated in the test itself so the next reader is not misled. — caught in the
      2026-08-17 optimize measurement-unit / confirm-gate plan, Phase 6 review.
