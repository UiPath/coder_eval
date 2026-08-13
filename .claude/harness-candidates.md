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

- [ ] **A run whose every task is skipped exits 0 — a green run of zero tasks.** When
  `resolve_all_tasks` demotes every task to `skipped_tasks` (a load failure, `skip: true`,
  or now a `--split` selector matching no labelled row), the run reports success: nothing
  failed, so the exit gate in `cli/run_command.py` — which keys only on failed/errored tasks
  and suite gates — passes. Verified directly: `coder-eval run <suite> --split holdou`
  prints one yellow "1 task file(s) skipped" line and exits 0. This is pre-existing, but
  `--split` makes it reachable by a one-character CLI typo rather than a broken file, and
  the whole point of a test confirmation is that you trust its verdict. Not guarded, and
  not a five-minute fix: making an all-skipped run non-green changes exit semantics for
  every skipped-task path (including deliberate `skip: true` suites and tag filters that
  match nothing), so it needs a decision about which of those should be fatal, plus tests
  per case. A narrower option is to fail only when a CLI *selector* (`--split`, `--tags`)
  eliminated everything, since that is unambiguously a user error rather than repo state.

- [ ] **Semantic answer-leak in a task prompt** — a prompt that describes the graded behaviour in *different words* ("list the paths explicitly rather than with a recursive wildcard" while grading an explicit glob) scores well whether or not the behaviour happened, and in an A/B an arm that deleted the rule still passes. CE036 catches only the verbatim form; the semantic form needs an LLM judge or a `lint-tasks` pass over this repo's own `tasks/`, neither of which is cheap or deterministic. — caught in the final review of c/2026-08-13-optimize-skill-fixes.md, where 4 of 10 rows in a shipped worked example had it.
- [ ] **A doc claim that contradicts merge semantics** — `optimize-skill` told users to declare `allowed_tools` in an experiment's `defaults: agent:`, which is a silent no-op because those fields merge by `replace` and the task layer outranks experiment defaults. Detecting "this prose recommends a config location that the merge order makes ineffective" would need the rule to model the layer stack against prose, which no existing rule shape supports. — caught in the final review of c/2026-08-13-optimize-skill-fixes.md.
- [ ] **`_normalized()` not used by every prose sensor** — 8 sensors in `tests/test_custom_lint.py` still inline `" ".join(path.read_text().split())`, so a future one copied from the wrong neighbour is defeated by a line wrap (the bug that let a stale skill count ship past 91 green tests). A rule forbidding the raw idiom in that file is easy; the conversion sweep was out of scope. — caught in the final review of c/2026-08-13-optimize-skill-fixes.md.
