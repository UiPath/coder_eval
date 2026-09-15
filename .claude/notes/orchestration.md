# Orchestration

> Conventions and authority order: see [README.md](README.md).

## Config merging and CLI overrides

- **Single declarative merge resolver**: All five config layers merge through ONE engine
  (`orchestration/config_merge.py::resolve_root`) for the three `-D`-reachable roots
  (`agent`/`run_limits`/`sandbox`). Each field declares *how it merges* once, on the
  model, via `MergeField(strategy="deep"|"append"|"replace")` (or a type-aware default:
  nested `BaseModel`/free-form `dict` → `deep`; `list`/scalar → `replace`).
  `resolve_task_for_variant` (layers 1–4) and `apply_overrides` (layer 5) build `Layer`
  lists and call the same `resolve_root`, so a field merges identically regardless of
  which layer supplied it (the unification invariant, enforced by
  `tests/test_merge_unification.py`). Lint rule CE014 forces every list field to declare
  its strategy explicitly.

- **Generic CLI overrides (`-D`/`--set`)**: Layer 5 is a thin wrapper
  (`orchestration/overrides.py`) over the resolver above. `coder-eval run -D
  agent.model=opus -D run_limits.max_turns=30` overrides any field on the resolved
  `TaskDefinition` (`agent`/`run_limits`/`sandbox` roots), schema-validated with
  did-you-mean. Only `--model` (→ `agent.model`) and `--driver` (→ `sandbox.driver`)
  survive as active thin aliases that emit the equivalent `-D` entry; an alias and `-D`
  targeting the same path is a hard error. `--type` (→ `agent.type`) is a separate,
  lighter alias that does NOT route through that collision check — `--type` and `-D
  agent.type=…` last-win rather than hard-error (the `-D` value wins). Tools, plugins,
  and SDK options are `-D`-only.

## Execute vs. run: the grading switch

- **Execute vs. run (the grading switch)**: `coder-eval execute` is `coder-eval run`
  with grading removed — the agent runs and the full trajectory is captured, but no
  criterion is checked, `weighted_score` is `None` (never `0.0`, which would be
  indistinguishable from "graded and scored zero"), and the row finalizes as
  **`FinalStatus.NOT_GRADED`**, whose `category` is a **fourth** bucket, `"ungraded"`.
  Ungraded rows leave BOTH sides of every rate: `RunSummary.pass_rate` / `error_share`
  and `VariantAggregate.pass_rate` divide by `tasks_graded` (`tasks_run -
  tasks_not_graded`), and `tasks_not_graded` is part of the sum-to-`tasks_run`
  invariant, not a `tasks_failed` sub-counter. **Only SUCCESS/FAILURE collapse into it**
  — `ERROR`, `TIMEOUT`, `BUILD_FAILED`, `MAX_TURNS_EXHAUSTED` and the budget stops are
  facts about the *run*, not about grading, and still apply (so `execute` still exits
  non-zero on a crash). The switch is `BatchRunConfig.grade` → `Orchestrator(grade=...)`
  → the **four** grading call sites (single-shot, evaluate-only, the simulation dialog
  check, and post-failure diagnostics); it crosses the docker boundary in `context.json`
  (a required contract field with no default, so a host and image that disagree about
  it fail at parse time). It is **deliberately not a task-config field** — no 5-layer merge, no `-D` path — because
  a task YAML must never declare itself ungraded; only the invoking command decides.
  `run` and `execute` share one body (`run_command.run_pipeline`) and differ solely in
  that flag, so there is no third code path. Three things are refused rather than
  degraded: `--junit-xml` (a report of verdicts, and there are none — though
  `reports/junit.py` still emits `<skipped>` for an ungraded row it encounters),
  `--allow-host-grading` (it decides how an ungraded row is GRADED, and `execute` grades
  nothing), and simulation tasks (their turn-continuation logic reads criteria results,
  so an ungraded dialog would silently change its own stopping behavior). `stop_early:`
  blocks are inert under `execute` for the same reason the kill switch exists: the full
  trajectory is the deliverable. Motivating consumer: an external harness (Harbor /
  Terminal-Bench 2.0) that builds its own container, calls coder-eval as the agent, and
  grades with its own tests.

### The terminal-status chain

`Orchestrator._terminal_status` answers one question and `run()` answers several, which is
why it is extracted; inlining it pushed `run()` past its complexity bound the moment the
grading switch was threaded in. Its ORDER is load-bearing at every step.

**A detached grade may not overturn an execution fact.** The prior run's terminal status
(TIMEOUT, ERROR, a budget stop) describes an agent phase this pass neither repeated nor
observed. Without that first arm, a crashed run re-graded against its half-finished
workspace reports SUCCESS — with the original `error_message` still attached.

**The NOT_GRADED arm sits ABOVE `max_turns_exhausted`, and that order is what makes
`execute` + `evaluate` equal a single `run`.** MAX_TURNS_EXHAUSTED reads like an execution
fact but is not one: on the graded path it is subordinate to the verdict — `run` returns
SUCCESS for a max-turns trajectory whose criteria pass, and only falls through to
MAX_TURNS_EXHAUSTED when they do not — so it is not knowable under `grade=False`.
Consuming it first made it terminal AND permanent, so the same agent output scored
SUCCESS/1.0 under `run` and MAX_TURNS_EXHAUSTED under `execute` → `evaluate`; being
category `failed`, `run --resume` then called the row complete and left it forever
unscored. Nothing is lost by deferring: the fact lives on `result.max_turns_exhausted`,
which the seeding carries. The statuses that ARE execution facts differ in kind — they
abort the run before a verdict is reachable, so preserving them overturns nothing.

The `FinalStatus.is_execution_fact` table must agree with this order:
`_EXECUTION_FACT_STATUSES` maps MAX_TURNS_EXHAUSTED to `False` (the defect a disagreement
produces is under isolation.md § Detached grading and `Sandbox.adopt`).
`tests/test_seed_from_prior_result.py` pins the status in a test of its own, apart from
the loop over the real execution facts.

### The four grading sites

`grade=False` is checked in exactly four places, and they do not behave alike:

1. **Evaluate-only** refuses: with no agent run and no criteria there is nothing left but
   an empty `task.json`.
2. **The single-shot loop** stops after capturing the trajectory. Returning False keeps
   `FinalStatus` off SUCCESS and the status chain turns it into NOT_GRADED. The
   reference-integrity check is skipped too — it protects a grade that is not happening.
3. **The dialog loop** RAISES, and is unreachable today because `execute` rejects
   simulation tasks at the CLI: the dialog reads criteria results to decide whether to
   keep talking, so an ungraded dialog would silently change its own stopping behavior.
   An empty-list "defensive no-op" here would be worse than a refusal — both callers go
   straight on to the gate, which treats an empty criteria list as a vacuous pass.
4. **The diagnostics path** records nothing: a `not_evaluated` vector would imply criteria
   we were supposed to run and could not.

Facts about the run are recorded BEFORE the switch, because `execute` withholds the
VERDICT, never the facts — the seeding cannot restore a fact the execute phase never
captured. The budget gate runs AFTER the criteria on the graded path purely for
partial-credit visibility, and there is no partial credit under `execute`.

### Rates need verdict evidence, not bucket counts

A published rate divides by rows that actually carry a verdict, not by a bucket count. The
four category buckets cannot tell a graded FAILURE from a TIMEOUT no criterion ever saw, so
`tasks_measured` is counted evidence and sits beside them without being part of the
sum-to-`tasks_run` invariant.

`nothing_was_measured` is the ONE definition of "this rate has no numerator to be a fraction
of", shared by the run summary, the variant aggregate and the suite rollup — three copies of
a published rate is how one surface reports `n/a` and another reports `0.0%` for the same
run, which already happened when the guard shipped on only one of them. Its first version
tested `succeeded + failed == 0` and was wrong for the same reason the bug it fixed was
wrong: TIMEOUT and the budget stops are category `failed` and reachable under `execute`, so
ONE timed-out row in a 100-task ungraded night read as "something was measured" and published
a real 0% point on the evalboard trend for a run that graded nothing.

An ungraded score stays `None` everywhere it is published. A plain float would launder it
into 0.000, which renders as — and is picked as a best variant against — a real score of
zero.

Every status maps to exactly one reporting category EXPLICITLY, with no catch-all, so a new
status fails the classification assert until someone decides where it belongs rather than
silently collapsing into `failed` and skewing both the reports and the telemetry dimension.
A failed image build is grouped with ERROR, being an environment fault rather than a task
outcome the agent could have avoided. The ungraded bucket is a FOURTH category, not a fold:
folding into `failed` would depress every pass rate, into `succeeded` would invent verdicts,
and into `error` would report a healthy run as broken.

`is_execution_fact` is explicit for the same reason — each status is either "the agent phase
ended this way" (preserved by a detached grade) or "grading decided this" (replaced).
Defaulting either way silently is how an ERROR row becomes a SUCCESS.

### Refusing a criteria-free task under grade

`TaskDefinition.success_criteria` accepts an empty list at the model level, because the
Harbor agent-phase `task.yaml` is criteria-free by design and must round-trip through
`coder-eval execute`, which never grades. But scoring is vacuous over an empty list —
`all_criteria_passed` returns True and `calculate_weighted_score` returns 0.0 — so such a
task graded under `run` finalizes as SUCCESS at `weighted_score: 0.0`, an internally
contradictory result for what is actually a typo, a bad merge, or a `-D` override that
cleared the list. The refusal is therefore scoped to `grade`, exactly as the sibling
simulation refusal is.

It is checked against the POST-`--resume` set, never the full resolved list: an
already-finalized row is folded back from `prior_results` and never re-executed or
re-graded, so its own possibly-empty criteria are moot and must not block a run that is not
going to grade it. The `to_grade` rows ARE about to be graded, so they get the same check —
explicitly, rather than relying on `regrade_in_place`'s own per-row guard, so the whole
batch is refused up front instead of one row at a time turning into a mid-resume warning.
`evaluate` needs the same guard on its orchestrator-direct branch, which never calls
`regrade_in_place` at all.

### What the exit code counts

The command exits non-zero when any task failed, errored, or any suite missed its
thresholds — and, under `run` only, when any row came back ungraded. `run` was asked for a
verdict and did not produce one (the grade crashed, or `--resume` could not grade the row),
which is a failure of the command even though the row is neither `failed` nor `error`.
Under `execute` an ungraded row is the expected outcome for every task and must not fail
the command.

The JUnit report is written BEFORE that gate, so a failing run still produces one; a write
error propagates rather than being swallowed. Telemetry is flushed in a `finally` so it runs
on both the success and the raised path, without catching the `typer.Exit` decided after it.

Per-suite rollups are skipped entirely under `execute`: a rollup aggregates per-criterion
results and there are none, so running it would gate a suite on an empty aggregate and
report a threshold failure for a run that was never measured. The ungraded bucket is named
explicitly in the aggregate line for the same reason — `coder-eval aggregate <run>` is the
step right after `coder-eval execute`, so an ungraded run is the FIRST thing it renders, and
without the term it reads "Aggregated 12 task(s) (0 ok / 0 fail / 0 err)": four numbers that
no longer sum to `tasks_run`, with nothing on screen to say where the rest went. The
end-of-run summary likewise reports what happened instead of "0/N succeeded", which for a
clean `execute` reads as a total failure, and points at the run-dir resume form rather than
`evaluate <task.yaml> <workspace>` — the two-argument shape grades a bare directory with NO
trajectory, so `command_executed` / `skill_triggered` / trajectory-reading judges score
differently from what `run` would have produced.

## `--resume` is command-relative

- **`--resume` is command-relative**: `partition_for_resume(tasks, *, grade)` returns a
  four-way `ResumePartition` (`to_run` / `to_grade` / `prior_results` /
  `prior_resolved`), because **"finished" is not absolute — it depends on what the
  resuming command still owes the task**. A `NOT_GRADED` row carries a final status, so
  the original "has any final status" test called it complete: right for `execute
  --resume` (it finished executing), and wrong for `run --resume`, which was asked to
  grade and would instead report "already complete", grade nothing, and **exit 0**. The
  routing test is the row's **evidence** (`weighted_score is None and not
  success_criteria_results`), not its category: keying on `category == "ungraded"`
  missed every `execute` row that ALSO carries an execution fact — a TIMEOUT or budget
  stop aborts before grading, so it lands unscored with category `error`/`failed`, and
  resume filed it as complete while `evaluate <run_dir>` graded the identical bytes
  happily. Under `grade=True` those rows route to `to_grade`, where
  `_grade_resumed_tasks` runs the criteria against the trajectory and workspace already
  on disk via `orchestration/regrade.py::regrade_in_place` — reusing the agent spend,
  which is the entire reason `execute` and `run` are separate. The carve-out is **only**
  for `NOT_GRADED`: `FAILURE`/`ERROR` stay complete under both commands (resume has
  never retried failures — delete the task.json), and `clear_rerun_artifacts`
  deliberately skips `to_grade`, whose artifacts are the very thing being graded. A
  per-task grading failure is warned, STAMPED onto the folded-back row's `error_message`
  (the console line alone is not durable), and folded back in with its ORIGINAL ungraded
  result, so one bad row neither aborts the resume nor vanishes from run.json — and the
  exit gate counts `tasks_not_graded` **when `grade` is True**, so a `run` that graded
  nothing exits non-zero instead of telling CI the suite is fine. Under `execute` an
  ungraded row is the expected outcome and never fails the command. A row is owed a
  grade only when it was **executed** AND is unscored: evidence of "no verdict" alone
  routed every dead container and failed image build (`_write_synthetic_task_json`
  writes those with no verdict either) into grading, where the fold-back replaced the
  real diagnostic with a wrong-cause grading error and left `task.json` and `run.json`
  disagreeing about the same row — so the test is `final_status is NOT_GRADED or
  iteration_count > 0`, and that fold-back now APPENDS to `error_message` instead of
  replacing it. A re-grade also writes its log to **`grade.log`**, never `task.log`:
  `task_log_handler` opens `mode="w"`, so grading into the row's own directory truncated
  the agent trajectory log the run had already paid for — contradicting
  `_apply_resume`'s own "to_grade is deliberately NOT cleared" contract. `grade` is in
  `_FINGERPRINT_DIFF_EXEMPT` because `execute` → `run --resume` is a supported flow, not
  config drift — and the warning's "already-finalized tasks keep their original-config
  results" text is actively wrong for it. **`orchestration/regrade.py` is the single
  implementation** shared by that path and `evaluate`'s run-dir mode, which DELEGATES to
  `regrade_in_place` rather than restating it (it originally hand-built its own Sandbox +
  Orchestrator and had already drifted — hardcoding `replicate_index=0`, so every
  replicate but the first was relabelled — which is exactly how two copies become two
  verdicts for the same run); it raises plain `RegradeError`, which the CLI wraps, since
  `orchestration/` must not import the CLI layer (CE004). One fidelity rule it enforces:
  a re-graded row keeps the **agent run's** `started_at`/`duration_seconds`, not the
  grading pass's — a 10-minute run re-graded in 2s would otherwise report 2s into
  `average_duration`, the report tables and the evalboard; the grading cost is preserved
  separately as `environment_info["grading_duration_seconds"]`.

### When a resumed grade crashes

A row that could not even be READ has no recorded result to fold back, but dropping it
removes it from `run.json` AND from `tasks_not_graded`, which is what the exit gate counts —
so a resume whose rows were all unreadable reported success. A minimal ungraded placeholder
stands in instead, keeping the task visible and the command non-zero. The read happens
inside the `try`: outside it, one bad `task.json` propagates out of the loop and aborts the
whole resume before `run_batch`, so none of the `to_run` tasks execute either — the opposite
of "one bad row never aborts".

An orchestrator-level grading crash is not a verdict about the run, and `Orchestrator.run()`
converts internal failures into a populated ERROR result rather than raising, so the
`except` above never sees them and the ERROR row replaced a perfectly re-gradeable
NOT_GRADED one — ERROR being "complete" for both commands, the row could then never be
graded again. Fixing the in-memory result is only half of it: `_finalize_result` has already
written the ERROR `task.json` into that same directory, so `run.json` would say NOT_GRADED
while the row on disk says ERROR, and the on-disk one is what a later `--resume` reads. The
pre-grade record is put back.

The `--resume` config-drift warning is best-effort and informational: the per-task path key
does not encode the run config, so resumed tasks keep their original-config results, and
surfacing the mismatch makes the resulting mixed-config `run.json` visible instead of
silent. A missing stamp (a run predating the feature) is tolerated.

## Early stop on criterion

- **Early stop on criterion (opt-in, per-criterion arming)**: a `stop_early:` block
  (`StopEarlyPolicy`) on a criterion ends a single-shot run early once the run's
  **armed** criteria decide the outcome, so a raised `max_turns` isn't wasted on the
  smoke flavor. The block's PRESENCE is the arming and alone activates the watcher —
  there is **no run-level master switch**: `run_limits.stop_early: false` is the
  run-level KILL SWITCH that force-disarms every block (the one-line
  experiment-variant/`-D` override for an authoritative full run), and
  `run_limits.stop_early: true` (the removed master arm) is a hard
  `EarlyStopConfigError` at resolution. The block exists on `LiveSuccessCriterion` only
  (currently `skill_triggered`, `command_executed` — so arming an unobservable criterion
  is unrepresentable, a pydantic extra-forbid error). Arming carries one implicit
  trigger (a native live-fail may fail-stop the run); its keys refine it: `on_pass:
  stop` (pass-stop the moment the criterion live-passes; default `continue` just
  latches) and `decide_within: N` (still undecided after N tool-call steps latches an
  **effective fail**, fed through the same fail-stop rule, reported as
  `decision_budget_exceeded` — an ordinary weighted fail, NOT a gate-bypassing
  force-fail; cumulative across retry attempts of the same turn). A trigger whose
  polarity the instance can't decide (per the abstract, checker-independent
  `live_decidable_polarities()`, a pure function of the criterion's own fields, paired
  with the checker's `live_verdict` override by lint rule CE025, a registry-based
  whole-tree check) is **inert by design** — one dataset-fanned YAML line serves both
  positive rows (pass/timeout live) and distractor rows (fail live). Verdicts **latch**:
  once a criterion decides, its `live_verdict` is never polled again. Stop rule is
  weighted, not strict-boolean: `run_limits.stop_early_gate_threshold` (default `1.0`,
  reproducing strict-AND behavior exactly) is the minimum weighted score (`Σ
  weight·score / Σ weight` over the armed subset) required to pass; a fail-stop fires
  once the armed set's **ceiling** (best case for everything still undecided) can no
  longer reach the threshold — so a low-weight fail or timeout that can't doom the gate
  is absorbed and the run continues — and is **deferred while any pass-capable armed
  criterion is undecided** (a distractor misfire never truncates a positive row's recall
  signal); a pass-stop fires once the `on_pass: stop` subset's **floor** (worst case)
  already meets the threshold, and is symmetrically **deferred while any pass-capable
  armed criterion outside the `on_pass: stop` subset is undecided** (so an early pass
  never freezes a sibling `on_pass: continue` criterion's signal out of the trajectory).
  A fail-stop is therefore verdict-preserving; a pass-stop can miss a *later* distractor
  misfire, so authoritative P/R/F1 comes from a kill-switched (`stop_early: false`) run.
  Driven by `orchestration/early_stop.py::EarlyStopWatcher` (built when
  `early_stop_active(task)`: ≥1 armed criterion, kill switch not thrown) through the
  agent's cooperative `should_stop` seam (tool-call granularity, no SIGKILL); live
  verdicts only *trigger* the stop — the standard `check_all_async` on the frozen
  trajectory is authoritative. Gating is **FIRED-ONLY**: a run the watcher actually cut
  gates on the **armed subset** via the weighted
  `EvaluationResult.armed_criteria_passed`; a run that completes naturally — armed or
  not — gates strict-AND via `all_criteria_passed`, so adding a block never changes the
  verdict of a run it didn't cut. Note the gate keys on the watcher having FIRED
  (`result.early_stop is not None`), not on confirmed truncation — an agent that ignores
  `should_stop`, or a stop firing on the final message, still gates armed-only. Every
  resolution-time guardrail violation is a hard error at resolution (plan *and* run);
  the one load-time case — a `stop_early:` block on a non-live criterion — is a pydantic
  schema error at task load, which the run surface reports as a skipped task like any
  other malformed task. A runtime verdict bug **fails open** to a full run. Surfaces:
  `EarlyStopInfo` (incl. `gate_threshold` at stop time), report notes/badges,
  `stopped_early` run.json rows, `EarlyStopped`/`EarlyStopReason` telemetry dims. Worked
  rationale: docs/TASK_DEFINITION_GUIDE.md § `stop_early`. No blocks anywhere ⇒ behavior
  byte-for-byte unchanged.

### Gate selection is fired-only

The weighted armed gate applies IFF the watcher actually cut the run. On a truncated
trajectory the unarmed criteria never had the chance to be satisfied, so they stay
advisory; a run that completed naturally — armed or not, watcher never fired or disarmed
fail-open — has a full trajectory and gates strict-AND over every gating criterion.
Arming a criterion must never change the verdict of a run it did not cut.

**Both single-shot grading paths must call the same selector.** A detached grade reaches
the verdict through the evaluate-only branch, where `early_stop` arrives from the seeding
rather than from a live watcher; selecting the gate there in a second, hand-written place
is exactly how the seeded field came to be carried but never read, so re-grading an
early-stopped run under the full-run gate flipped its verdict.

One gate for every early-stopped run, with no per-reason branches: a decision-budget stop
is just a fail-stop whose deciding criterion timed out. The ceiling is an upper bound on
the authoritative armed score only because the watcher reduces the SAME trajectory the
checker scores — it records UNRESOLVED tool ends exactly as the agent's `EventCollector`
does — so the weighted armed gate is correct whether the watcher fired on a pass, a fail
or a timeout.

**The simulation dialog path does NOT route through this seam**, and `result.early_stop`
is never assigned there, so an armed simulation task gates strict-AND on a possibly
truncated trajectory. Wiring the dialog path through it means also setting `early_stop`
there; until then the limit is stated rather than implied.

The watcher is built ONCE, in `_setup`, so its turn/tool counters and wall-clock origin
accumulate across retry attempts. It is built before the evaluate-only early return, so an
armed evaluate-only re-grade builds an inert, never-fed watcher — harmless, and one
creation point. Under `execute` it is armed but stays disabled: there is no outcome to
decide and the trajectory is the deliverable, so an armed criterion must not truncate it.

### Verdicts latch, and the decision happens on the CALL

Once an armed criterion decides on a RESOLVED round, its `live_verdict` is never polled
again — the checkers' documented monotonicity makes re-polling pure waste. Latching happens
only on resolved rounds, so a dispatched call that never resolves (a crashed attempt)
cannot leave a stale verdict behind across retries; an in-flight round's fresh verdict can
still FIRE a stop, it is just not persisted.

Deciding on the tool CALL rather than its result is what makes the stop robust: for an
observable criterion the verdict is fully determined by the call's inputs (which skill,
which command), so the watcher latches the instant the call is dispatched — before a
cut-short turn can strip the result and leave the call unresolved. The agent polls
`should_stop` immediately after dispatching each message, so a stop on the call breaks the
loop before the result is ever pulled. The matching `ToolEndEvent` still evaluates, which
covers a verdict that only becomes decidable once the result is known.

**UNRESOLVED tool ends are RECORDED but never counted or evaluated on.** Agents force-close
orphaned tools as UNRESOLVED *after* the message loop has ended and the terminal status is
already chosen, so they are not live tool activity — treating them as such would let a run
that completed, timed out or crashed without a real in-loop decision latch a false early
stop.

Live verdicts only TRIGGER the stop; the authoritative scores always come from the standard
check on the frozen trajectory after the cut.

### The ceiling and floor bounds

Both the stop rule and the post-hoc gate consult `run_limits.stop_early_gate_threshold`
(default `1.0`) rather than treating every armed criterion as equally decisive.

A **fail-stop** fires once the armed set's CEILING — best case, every still-undecided or
passing criterion scores 1.0 and every effectively-failed one scores 0 — can no longer
reach the threshold, i.e. the gate is mathematically guaranteed to fail however the
trajectory continues. A `decide_within` timeout participates as an ordinary weighted fail,
so a low-weight criterion's timeout that cannot doom the gate does not stop the run.

A **pass-stop** fires once the `on_pass: stop` subset's FLOOR — worst case, every
still-undecided member scores 0 — already meets the threshold. Criteria armed only on the
FAIL side (distractors) are excluded from both the numerator and the denominator of that
bound: they can never live-pass and exist only to guard the fail side, so folding them in
would veto every pass-stop and penalise the bound for a criterion it was never scoped to
cover. With no `on_pass: stop` criteria at all the bound is vacuous and returns nothing —
there is no pass-stop to take, and the run continues to the cap.

At the default threshold both bounds collapse exactly to "any single armed criterion's
effective fail stops the run" and "every `on_pass: stop` criterion has live-passed".
Lowering it lets a low-weight armed criterion's failure be absorbed without truncation.

### The armed gate is not the watcher's bounds

The ceiling and floor above decide WHETHER to stop. `armed_criteria_passed` is the
separate, post-hoc question of how a run that was cut gets graded, and it runs over the
ARMED subset only — an unarmed criterion took no part in the decision to truncate, so
gating on it would judge the run against evidence the truncation guaranteed would be
missing.

Each armed criterion is BINARISED against its own `pass_threshold` before weighting, so
the gate asks "did this criterion pass?" rather than averaging raw scores — which is what
makes `gate_threshold=1.0` an EXACT equivalence with the strict-AND rule, not an
approximation of it. That exactness is what the fired-only rule above rests on.

It raises rather than returning False on a criteria/results length mismatch or an empty
armed set. Neither is a verdict — the first is a caller bug, and the second means the
caller reached a fired-only gate for a run the watcher never fired on, which
`early_stop is not None` is there to rule out.

### Precision is traded, recall is not

A pass-stop cuts the run the instant the floor locks in, so a fail-armed criterion (a
distractor) that would only misfire on a LATER tool call is never observed, and the frozen
trajectory scores that row a clean pass. That is an intentional precision-for-budget trade
of the opt-in smoke flavor; **authoritative precision must come from a non-early-stop run.**

Recall is never truncated, because BOTH stops defer on it. The fail-stop is DEFERRED while
any pass-capable armed criterion is still undecided and within its budget, so a distractor
misfire on an early tool call cannot cut a positive row before its expected signal has had
the chance to appear — which would freeze a would-be true positive as a false negative and
deflate recall. Symmetrically the pass-stop is DEFERRED while any pass-capable armed
criterion OUTSIDE the `on_pass: stop` subset is still undecided; subset members are already
accounted for by the floor bound itself. Otherwise an `on_pass: stop` criterion passing
early would freeze a sibling `on_pass: continue` criterion as an unearned fail on the
truncated trajectory.

**Neither deferral LOSES the trigger.** Verdicts latch monotonically, so a held stop fires
the moment every pass-capable armed criterion decides — the fail-stop is evaluated BEFORE
the pass-stop each round — and if none ever decides, the run simply continues to the cap.
A row with zero pass-capable armed criteria (a negative row stacking only distractors) has
nothing to defer for and fail-stops on the first misfire.

### Inert triggers are by design, and the watcher fails open

A trigger whose polarity an instance can never decide is INERT, not an error — one
dataset-fanned YAML line serves both positive rows (pass and timeout live, fail inert) and
distractor rows (fail live, pass and timeout inert) without per-row conditionals. That is
why the validator carries NO per-instance polarity guards. Arming an unobservable criterion
is structurally impossible, since the block exists only on `LiveSuccessCriterion`, so a
`file_exists` criterion carrying one is an `extra='forbid'` error at load. An armed-but-
empty set needs no guard either: with no blocks present there is simply no watcher.

The watcher keeps its OWN `EventCollector`, independent of the one the agent builds its
returned `TurnRecord` from, so each `live_verdict` sees a fresh single-element partial
trajectory.

**Fail-open:** a `live_verdict` that raises disarms the watcher, logs loudly, and degrades
to a full run. Because live verdicts are triggers and not truth, this can never produce a
FALSE early stop — it only ever errs toward running more.

### Why the guardrails are not model validators

A degenerate gate threshold on an ARMED task, and the removed master arm, are both rejected —
but in `orchestration/early_stop.py`, not on the model. Whether a task is armed lives on the
CRITERIA, which the run-limits model cannot see, and that model is field-merged across five
layers, so a model-level validator cannot tell a real mistake from a value merged forward
from a sibling layer.

The placement also decides what the failure DOES. A dedicated error gets the same hard-stop
CLI treatment as every other early-stop guardrail — it flips the plan exit code and aborts
the run — whereas a plain pydantic error would land in the plan command's generic per-variant
"resolution failed" branch, which prints red text but deliberately does not flip the exit
code, so a model-level raise would silently pass CI. Cross-field semantics that are warnings
rather than errors live in the run-limits validator for the same post-merge visibility.

## Recording the task as authored

`task_config.resolved` and `source_file` describe the task as AUTHORED, which is NOT
always what this process runs.

`run_task_internal_command` rewrites `driver: docker` → `tempdir` before building the
in-container orchestrator, because it is already inside the container the driver asked
for. Recording that rewrite made the run's own record deny it ever used docker — and a
later `evaluate <run_dir>` reads the driver back out of the record, so the host-grading
refusal never fired and the `graded_on_host` stamp was never applied. A container task's
criteria ran against the host filesystem silently, which is the exact outcome that gate
exists to prevent.

The PATH is the same seam for the same reason. `task_file` is what this process resolves
`TASK_DIR` and the reference against; in a container that is `/work/task_dir/task.yaml`,
correct there and meaningless anywhere else. Recording it made a container row's
`source_file` name a path that exists on no host, so a later `evaluate` rebuilt the task
around it: the docker dispatch guard saw a non-None `Path` and let it through, and the
task-dir mount then silently mounted nothing, so every `$TASK_DIR` criterion resolved
against the wrong tree and scored a verdict nobody could explain.

### The in-container driver rewrite

CE051 forbids rewriting `sandbox.driver`, and this is its single exemption: the process is
already inside the container the docker driver asked for, so the isolation the driver names
is present rather than bypassed, and a nested docker would be both wrong and impossible (no
docker CLI in the image). The rewrite goes through `model_validate` rather than
`model_copy(update=...)`, matching its sibling in `regrade.grading_sandbox_config`: `update`
skips BOTH pydantic and pyright, so a typo produces a `SandboxConfig` violating its own
`Literal` and only surfaces far downstream. Two driver-rewrite sites landing in one change
with two different levels of type safety is how the weaker one becomes the pattern people
copy.

## Three routes, resolved separately

The agent's route, the judge's and the simulated user's are resolved independently and
recorded separately, so a run artifact shows what actually ran, graded and talked to it.

- **`route`** is the agent's own.
- **`eval_route`** (llm_judge / agent_judge) is pinned to a constant Claude backend when
  the agent runs on an open-weight LiteLLM model, so grading stays comparable. Its model
  is recorded separately from the agent's, so a `checker_context.api_route` override is
  visible in run artifacts rather than merely inferable.
- **`simulator_route`** is resolved through the same pinning guard but with NO
  `checker_context` overrides: that knob is llm_judge-only and has no bearing on the
  simulator. The simulated user is part of the MEASURING INSTRUMENT and must not run on
  the agent's own gateway either — but it is also a real Claude Code CLI subprocess, like
  the agent under test, not a checker concern. Aliasing it to `route` would drop the
  pinning; reading `checker_context` would couple it to the judge.

All three equal `route` on the Direct and Bedrock backends.

## Where the orchestrator's own time is booked

(The bucket definitions live in [timing.md](timing.md); this is only where the
orchestrator starts and stops its own clocks.)

`setup_ms` covers everything before the agent runs — environment capture, criterion
discovery, sandbox provisioning, `agent.start()` and `pre_run` — as ONE task-level bucket.
Its mark sits at the top of `run()`, not at `_setup()`, because the single largest item is
already behind us by then: `get_version_info()` shells out for the git commit and every
CLI's `--version` and costs 733 ms measured. Starting the mark at `_setup()` put that
outside every named bucket, so it landed in the report's residual — 733 of the 758 ms that
made "Unaccounted" look like a real unknown when it was one nameable call.

The bucket is roughly harness-independent — measured within ~10 ms of each other for
claude-code and pi on the same machine — which is the tell that it is the orchestrator's
own cost rather than any harness's. Left in the residual it read as 10% of a 19 s task,
and would read 60% of a 3 s one.

A dimension is OMITTED rather than coalesced to `0`, in telemetry and in the report alike.
Dashboards average with no status filter, so a laundered zero for an ungraded night drags
every tile toward zero and is indistinguishable from a genuinely bad night, or from a
harness that booted instantly. An absent dimension drops out of the average instead. The
same argument makes the log line read `n/a` rather than `0.000`.

## Teardown must be interrupt-proof

The task-timeout watchdog can fire while post-run commands are awaiting and deliver its
`CancelledError` inside the `finally` block, which used to abort teardown wholesale —
skipping cleanup (tempdir leaked) AND result finalization (`task.json` lost, so the task
silently drops out of the run). The interrupt is caught, the full teardown runs, and it is
re-raised at the end so callers observe the same exception. The watchdog cancels exactly
once, so the awaits after the catch run normally.

## Restoring a PATH from a run directory

A run dir is a shareable artifact — that is the whole point of detached grading — and
under `driver: docker` it is bind-mounted writable into the container the agent runs in.
The PATH recorded in its own `task.json` is PREPENDED ahead of the host PATH, so taken
verbatim it lets a run dir decide which binary `pytest` resolves to on the grader's host.

Four filters, all about what PATH parity actually needs:

- **Absolute only.** A relative entry resolves against the grader's current working
  directory, which has nothing to do with the run, so `evilbin` becomes `$PWD/evilbin` at
  the front of every criterion subprocess's PATH. It also cannot be the toolchain location
  it claims to be, since the run resolved it somewhere else.
- Drop anything that is not an existing directory — a dead entry buys no parity.
- Drop any entry inside the WORKSPACE being graded: that tree is agent-writable, so a shim
  dropped there would shadow a real tool.
- Drop any entry inside the RUN DIRECTORY as a whole. The workspace is only part of it;
  `artifacts/`, a sibling replicate's tree and the run root all travel in the same shared
  artifact and are equally attacker-chosen.

The PATH is captured only on the per-turn happy path, after a successful turn, which
leaves three gaps: an agent crash or turn timeout (the sync never runs, and a crashed
agent's SDK PATH may itself be unreliable), evaluate-only mode, and the window before the
first turn. Persisting it is what closes the evaluate-only gap for a LATER detached grade,
which would otherwise resolve `run_command` criteria against ambient PATH and could reach
a different verdict than the run it claims to be grading.

A sandbox-setup-time sync was considered and rejected: the agent SDK's effective PATH is
only knowable after the SDK initializes, so it would capture the configured prepends
rather than the full agent environment.

## The dialog loop

(The per-site mechanics stay as comments in `_simulation_dialog_loop`; this is only the
shape of the loop and the reasoning that does not fit beside one statement.)

One invocation runs exactly one dialog trajectory; parallel trials are expanded upstream.
It replaces the criteria-feedback iteration loop for tasks carrying a `simulation` block,
and emits the same streaming events as the single-shot loop, so downstream renderers work
unchanged.

**It is the one grading path that does not route through the shared gate selector** — see
§ Gate selection is fired-only — so `result.early_stop` is never assigned here and an armed
simulation task gates strict-AND on a possibly truncated trajectory.

`execute` rejects simulation tasks at the CLI, because the loop reads criteria results to
decide whether to keep talking: an ungraded dialog would silently change its own stopping
behaviour rather than merely skipping the score.

## Embedded commands

A recorded config is UNTRUSTED INPUT. `evaluate <run_dir>` rebuilds the task from the
run's own `task_config.resolved`, and a run directory is a shareable artifact — so
everything the rebuilt config would run executes on the GRADER's host, under the grader's
credentials. `embedded_commands` enumerates that surface and `check_embedded_commands`
refuses, or at minimum names it, before anything runs.

The enumeration deliberately includes capabilities that are not shell lines, because the
question the consent prompt answers is "what will this do on my machine", not "what will
it exec":

- **`agent_judge`** has no command string of its own; it spawns a tool-using SDK agent
  (Bash included) under the grader's credentials, which is strictly WIDER than one shell
  line.
- **`llm_judge`** executes nothing locally, but it spends the grader's model budget and
  ships the graded artifacts — optionally the trajectory — to a provider of the recorded
  config's choosing. That is a capability an operator should approve.
- **`uipath_eval`** shells out with every argument `shlex`-quoted, so it is disclosure
  rather than injection — but it is still a subprocess the recorded config chose to start.

### Why the container dispatch renders as ONE command

`check_embedded_commands` joins the list with `"; "` and interpolates `len(commands)` into
the consent prompt, so every entry MUST be a command. The first version appended argv
FRAGMENTS separately, so a task with two build args and one mount asked the operator to
approve "4 shell command(s)" reading `docker build -f Dockerfile;   --build-arg FOO=bar;
--build-arg BAZ=qux;   -v /a:/b` — one docker invocation described as four commands, three
of which are not commands. The consent prompt is the one place this text has to be exact.

It also names every HOST PATH the dispatch exposes, not just those under
`sandbox.docker`. Three families reach the record-named image without the record
mentioning them in a `docker` block: the TASK DIRECTORY, copied wholesale from the
recorded `source_file`'s parent (a record whose `source_file` is `~/.ssh/config` copies
all of `~/.ssh` in); every `agent.plugins[].path`, `TemplateDirSource.path` and
`agent.system_prompt_file`, auto-mounted read-only at their host paths; and a WRITABLE
copy of `~/.claude`, `.credentials.json` included. Networking defaults to bridge, so
anything the container can read it can also send. Disclosing only `sandbox.docker.*`
would ask the operator to consent to a strict subset of what actually happens.

`docker build` deserves its own line in the prompt because it runs every RUN step in the
recorded Dockerfile on this host and expands recorded build args against the GRADER's
environment — so a `${ANTHROPIC_API_KEY}` arg is exfiltratable by a RUN step, and
`extra_args` is spliced into the argv unfiltered.

A warning is not a control: `_sensitive_source_paths` only warns, and about a fixed list,
and it prints as the command is already being prepared.

### What the gate covers, and why each part is in scope

`include_setup_phase` covers the two capability families that exist only on the `--copy`
path: `pre_run`, and the sandbox's own provisioning. Both are SKIPPED when grading in
place, so on that path they are not a capability the run dir has.

**`post_run` is deliberately NOT behind that flag**, and this is the one place the
distinction bites. It used to be, back when the hooks were skipped as a pair — but
`post_run` belongs to the GRADING phase, so it now runs on EVERY grading path, in place
included. Leaving it inside `include_setup_phase` made the in-place path — the DEFAULT for
a run directory — execute recorded shell with no consent prompt at all.

It is filtered against the operator's own baseline for a reason worth stating precisely:
the gate asks the operator to approve shell THE RECORD CHOSE, and a single `coder-eval
run` runs `post_run` with no prompt, because the config came from the operator. The
grader's own default experiment appends the same `post_run` to every task it runs, so
finding one of those in a record reveals no choice the record made. Prompting on it would
fire on 100% of run directories, and **a refusal that always fires is read as a formality
and waved through** — which is how the gate would stop protecting the authored commands
that DO represent a choice.

**Sandbox provisioning is the half the gate originally missed, and the worst one.** The
recorded `sandbox` block is carried through untouched, and the `--copy` branch then calls
`Sandbox.setup`, which reaches `uv pip install <recorded packages>`, `npm install
<recorded packages>` and `git clone <recorded url>`. A package name is arbitrary code at
install time. Because the scan walked only `success_criteria`, a shared run directory
whose criteria were all `file_exists` sailed through and still ran installers of the
attacker's choosing.

**`include_container_dispatch` is the same omission again, one layer up**, reintroduced by
the very change that made a docker row gradable. The grade is DISPATCHED INTO A CONTAINER
built from the recorded `sandbox.docker` block — so the record chooses the image that runs
on this host, with the default credential allowlist forwarded into it, a writable copy of
`~/.claude`, and a pinned `--entrypoint` the image itself supplies. That is arbitrary code
execution from a shareable artifact, and strictly WIDER than the `run_command` strings the
gate already refuses. Like `post_run`, it is a capability of the IN-PLACE path, so it
cannot hide behind `include_setup_phase`.

`grade_in_place` is the ONE lever selecting which families are disclosed, and deliberately
one parameter rather than two. It shipped beside an `include_setup_phase` that every
caller passed as its exact complement — two names for one fact, with nothing rejecting the
incoherent pairings. The flag gates a SECURITY disclosure, so a caller that set one and
forgot the other would drop the container-dispatch half with nothing failing. Both derived
values are computed once. `allow_host_grading` participates only through that derivation:
with it set no container is dispatched, so naming one would ask the operator to approve
something that never runs.

The scan uses `isinstance` narrowing, never `getattr(c, "command", None)`: an untyped
string probe over a discriminated union is invisible to pyright, so renaming a field
silently degrades the only guard on this path to a permanent no-op. It also cannot reach
`agent_judge`, whose tooling is the widest blast radius of the three.

### Why the container dispatch requires an EXISTING task file

The image is built or named by the task's own sandbox config, and the Dockerfile and
reference directory resolve relative to the task file — so without one there is nothing to
build from.

Testing only for `None` was not enough, and failed on exactly the rows the guard was
written for: the recorded `source_file` of a container row is a real, non-`None` path that
exists on no host, which is the defect § Recording the task as authored describes. Nothing
else here repeats that argument.

Requiring existence also closes a second hole: on the detached path `task_file` comes
straight from the untrusted record, and its PARENT is what gets copied into the container,
so a recorded `source_file` of `~/.ssh/config` would copy all of `~/.ssh`. Existence alone
does not make the path trusted — that is the consent gate's job — but it removes the
silent-wrong-verdict half.

## Locating the workspace a finished run left behind

`sandbox_path` is authoritative when it still exists. Otherwise the preserved artifacts
tree, where preservation nests the workspace under the task id — the EXACT path, not "the
single child of `artifacts/`", because a dataset row's `task_id` contains `/` and the
heuristic resolves one level too high for every row task.

It RAISES rather than guessing when neither is conclusive. Guessing is worse than failing:
grading the wrong directory makes every path-relative criterion fail as a locating
artifact rather than as a verdict, and reports that as an ordinary score.

**Every return goes through the containment check, rooted at the RUN DIRECTORY.** Both
`sandbox_path` and `task_id` are unvalidated strings out of the run's own `task.json`, so
`"../../../../home/victim"` joins to a real directory `is_dir()` happily confirms. The
check originally covered one branch of four and rooted the `task_id` case at `artifacts/`
rather than at the run directory, which made it vacuous the moment `artifacts` was ITSELF
a symlink — and `artifacts/` is attacker-supplied for a shared run dir just like the other
two. The escaped tree then became the grading root via `Sandbox.adopt`, `run_command`
criteria ran with it as cwd, and the resulting verdict was written back into the run's own
`task.json`.

## Experiment resolution

**Dataset fan-out runs BEFORE variant resolution**, one task per row, each treated as an
independent task for the four-layer merge — which is what locks the invariant that a
variant cannot override the dataset.

A per-task resolution failure is COLLECTED, not raised inline: a task whose own YAML is
incompatible with the resolved run (Claude-only `sdk_options` surviving a `--type codex`
override, which `CodexAgentConfig` forbids) would otherwise abort the entire run. The
file's resolved tasks are buffered and committed only once the whole file resolves, so a
mid-file failure discards that file's fan-out as a UNIT rather than leaving a partial,
lopsided one behind.

The `except` sets are deliberately NARROW — `FileNotFoundError`, `OSError`, `ValueError`,
`yaml.YAMLError`. `AttributeError` / `TypeError` / `ImportError` signal a regression in the
loader and must crash loudly rather than silently demote every task to "skipped". Pydantic
`ValidationError` is a `ValueError` subclass in v2, so it is covered.

**Early-stop arming errors always propagate** and are never demoted to skipped, so a
misconfigured run fails loudly instead of quietly shrinking the suite.

If EVERY task that reached resolution failed, the run refuses rather than producing an
empty one — and it surfaces the FIRST task's own error rather than a synthesized "global
misconfig" message, because a genuine global cause (a bad `--type`, an invalid `-D`) is
indistinguishable from N tasks each independently incompatible for the same reason. A
`ValueError` is re-raised verbatim so its message stays clean; anything else is normalized
to `ValueError` so it still lands in the caller's clean handler instead of escaping as a
raw traceback.

Tasks are sorted to run INTERLEAVED — replicate 0 of every (task, variant) first, then
replicate 1 — preserving declaration order within a replicate. Duplicate detection keys on
`(task_id, variant_id, replicate_index)`, because simulation replicates legitimately share
the first two.

`skip: true` is honored BEFORE dataset expansion, so a quarantined task skips row fan-out,
variant resolution and any further I/O. It is still reported in `skipped_tasks`, so the
suite shows which YAMLs were intentionally excluded rather than failed to load.

### How a resolution failure reaches the operator

A GLOBAL failure raises and is surfaced as a clean CLI error instead of a traceback;
`run_batch`'s own resolution-time guards raise plain `ValueError` and are converted to that
same error, so every refusal in the pipeline reads alike. Per-task failures never raise --
see above.

Every task-file pattern the caller wrote is checked for a match, not just their union.
Accumulating and checking only the total meant one stale entry among several -- a renamed or
moved suite -- silently ran the surviving subset and exited 0, so a CI gate reported green
over tasks it never measured.

### No-op tasks need no special case anywhere

`type` is a replace-scalar, so a task-level `type: none` wins over a baseline coding agent
injected by the default experiment, and the merged config validates as `NoneAgentConfig`.
A suite-wide `--model` or `-D agent.*` lands on it harmlessly, and `type: none` already
satisfies the `agent.type` contract. An explicit `--type <x>` is highest precedence and
replaces it, turning the no-op task into that agent.

### Aggregation drops ungraded rows rather than zeroing them

Ungraded replicates — and ONLY those — drop out of a mean entirely: `or 0.0` would average
a clean `execute` run down to a real-looking zero, while dropping an ERRORED one would pay
it a bonus.

Only graded variants can win or set a spread; including ungraded ones at 0.0 would name an
arbitrary "best" among scores that do not exist. When NOTHING was scored there is no
winner, and the fallback must not invent one — `variants[0]` is whichever arm the input
happened to list first, so swapping the inputs flipped the reported winner while
`is_tie=False` asserted it was a real result. It now sorts by `variant_id` for determinism
and marks a tie among all arms, which is what "no arm outscored another" means.

`tasks_measured` counts rows with a score, because a TIMEOUT lands in the `failed` bucket
without any criterion having run — so the buckets alone cannot answer "was this measured
at all". `average_score` is `None` rather than `0.000` when nothing was graded, since a
zero beside "Pass Rate: n/a" is indistinguishable from "scored zero".

The status-priority map is annotated with the SAME `Literal` that `FinalStatus.category`
returns and indexed directly rather than via `.get(..., -1)`. Adding the fourth `ungraded`
bucket was a manual step no checker could verify — an untyped `dict[str, int]` proves
neither that every category is present nor that no stray key is — while the `-1` default it
leaned on was already unreachable, and, though documented as "fail-closed", sorted BELOW
error, so a fifth category would have silently outranked ERROR as the worst status.
