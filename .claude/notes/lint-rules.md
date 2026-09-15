# Lint rules

> Conventions and authority order: see [README.md](README.md).

A rule's module docstring (or, for a doc-surface or whole-tree rule, its `@pytest.mark.lint` class in `tests/test_custom_lint.py`) states the invariant, the scope and the blind spots, and it wins on those. This file holds why each rule exists: the defect that caused it and the measurements behind it.

## CE009

Without `extra='forbid'` a misspelled key, for example `directry: foo/` for `directory:
foo/` on `ReferenceSource` or a typo on a criterion field, is dropped with no signal that
it was ignored. Forbidding extras surfaces the typo at load time with the concrete field
name.

Result and persistence models are exempt as deliberate round-trip leniency: they preserve
forward-compat fields through `model_dump_json` → `model_validate_json` round-trips of
`task.json` records. That is why `models/results.py` and `models/telemetry.py` are out of
scope and the result-shaped classes in `models/experiment.py` (`VariantResult`,
`ExperimentResult`, ...) carry per-class suppressions.

Only direct `BaseModel` subclasses are flagged, and same-file inheritance is the only
chain followed. The trade keeps the rule cheap: it does not walk the transitive base chain
across files, and it trusts the convention that every model class in the scoped files
eventually derives from `BaseModel`.

## CE013

no regex parsing of agent transcripts inside `evaluation/` or `criteria/` — the 2026-05-20
judge refactor replaced text-with-JSON verdict parsing with a typed tool channel
(`submit_verdict`). The legacy parser ran regex against structural transcript tags
(`[ASSISTANT]`, `[RESULT - …]`) and JSON-shape literals (`\{`, `"score"`, `"rationale"`),
all coupled to `ClaudeCodeAgent._format_messages` rendering choices that have no business
being a correctness contract. The rule keeps that pattern from returning.

## CE014

The declarative merge engine reads a per-field strategy off the Pydantic `FieldInfo` and
falls back to a type-aware default: nested `BaseModel` / free-form `dict` merge `deep`,
`list` and scalars merge `replace`. A `list` is the one type whose default (`replace`) is
easy to mean otherwise (`append`), so a list field that silently keeps `replace` when it
meant `append` is a latent resolution bug. For nested-model and dict fields the
nested-replace regression is structurally impossible, so they may keep a plain `Field`.

Scope is the set of model classes the engine feeds through `merge_layers` /
`resolve_root`: the three `-D`-reachable roots, the sandbox sub-models reached by deep
merge, and the two models merged outside the `-D` roots (`TaskDefinition` for
`pre_run`/`post_run`, `SimulationConfig` for `constraints`). Scoping by class name rather
than by file keeps the rule pinned to the engine's real roots and avoids flagging unrelated
list fields that share a file, such as `PreRunCommand` in `tasks.py`.

## CE020

`BaseAgentConfig` is the vendor-neutral Pydantic base shared by every agent kind,
including third-party BYOA configs. A field on it typed against `claude_agent_sdk` leaks a
Claude-Code-specific type onto agents that have nothing to do with the Claude SDK. The
refactor that removed that leak turned `plugins: list[SdkPluginConfig]` into a local
`LocalPluginConfig` and moved `setting_sources` down to `ClaudeCodeAgentConfig`. The
boundary is mechanically detectable, so the rule guards it, and it matches every import
style so the leak cannot come back through a different spelling.

## CE021

`task.json` is the harness's always-produce/always-consume artifact: the only thing that
crosses the container boundary, and the per-task record every dashboard and timeline reads.
A bare `EvaluationResult.model_validate_json(text)` turns a present-but-malformed file
into an uncaught exception that crashes the run. Two causes produce such a file: schema
skew between a stale `:latest` image and the host (the docker version checks only warn),
and a truncated or torn write. The incident was at `docker_runner.py`: the parse re-bucketed
the task to a non-persisted in-memory ERROR with no per-task report. The fix degrades:
catch `ValueError` and persist a synthetic ERROR record (`batch.py::_load_completed_result`,
`recover_task_results`, `docker_runner.py::_handle_malformed_task_json`).
`recover_task_results` catches `(OSError, ValueError)`, which is why a tuple member counts
as a guard.

Scope stays narrow to `EvaluationResult` because it is the always-produce/consume
contract.

## CE022

`Orchestrator._simulation_dialog_loop` is the one function in the tree that keeps a
`# noqa: PLR0915`. It is a sequential dialog driver whose residual length is irreducible
without a state-object rewrite (the 2026-06-23 decompose-god-functions plan, Phase 5). The
`noqa` disables ruff's statement check entirely, so without a guard the function can
silently regrow toward its pre-decomposition size and nothing fails.

`_CAP` is the measured post-decomposition count (122) plus 6 headroom: ordinary edits do
not trip it, a real regrowth does. The rule is deliberately narrow, one named function
rather than a general size rule, because ruff's 80/25 ceiling already covers every other
function.

## CE024

A bare `A | B | C` union of tagged Pydantic models validates via smart-union: input with a
missing or typo'd `type` tag silently coerces to whichever variant happens to fit, instead
of raising a crisp discriminator error. The `SuccessCriterion` union shipped this way —
tag-less criterion dicts coerced to the structurally-nearest variant — and the fix wraps it
in `Annotated[..., Field(discriminator="type")]`.

The callable `Discriminator(...)` form is accepted because `CriterionResultUnion` in
`models/results.py` uses it. Unions with an imported member are out of scope for the same
same-file conservatism CE009 documents.

## CE032

`Sandbox.resolve_files` is the single place criterion `path` semantics live: literal-first
resolution (a real file named `report[2024].json` is not reinterpreted as a character
class), glob expansion for artifacts whose location the prompt does not pin, ignore-pattern
filtering (`.venv` / `node_modules` / `dist` cannot be graded as agent output), and
exactly-one enforcement on content reads.

A checker that builds its own path with `sandbox.sandbox_dir / <field>` and reads it
directly silently opts out of all of that, so path semantics differ per criterion. That is
how `reference_comparison.agent_file` drifted from every other path field. The rule fires
on the join, not on reading `sandbox_dir`, because the bare root has legitimate uses.

## CE037

no unreferenced module-level private helper in `src/` — a helper whose docstring documents a bug the live code still has is worse than none

The motivating `_rmtree_restrictive` explained, correctly and in detail, why
`rmtree(..., ignore_errors=True)` orphans a mode-000 reference tree, while both live
cleanup sites called exactly that. A reader auditing the cleanup path found a function
asserting the shipped code was broken, and a test that called the helper directly made the
real path read as covered.

The rule turns "helper written, never wired" into a `make lint` failure at the commit that
introduces it, which is the only moment anyone knows where it was meant to be called from.

Scope is narrow so it stays a bug detector rather than a style nag: methods are reached via
`self`, which a name search cannot see; public names have out-of-tree callers; dunders are
protocol; a decorator (`@register_criterion`, `@field_validator`, `@app.command`) is a
registration, so the reference is the decorator, not a call. The corpus is a whole-tree
text search rather than an import graph: a helper referenced anywhere — called, passed as a
callback, aliased — counts as wired, and a name that merely appears in a docstring is an
accepted false negative for a rule that must never block a legitimate refactor.

## CE038

in an `@asynccontextmanager`, the acquire must sit INSIDE the `try` whose `finally` releases it — `asyncio.shield` protects the inner task, NOT the await, so a cancel on `__aenter__` skips the unwind while the work completes

The leaking shape is `held = await acquire()` directly above `try: yield` /
`finally: release(held)`. The awaiting coroutine receives `CancelledError`, propagates it
out of `__aenter__`, and never reaches the `finally`.

The motivating bug held a reference directory at mode 000 with no matching restore: the
directory stayed unreadable for the rest of the run, and a stale registry entry poisoned the
next window on the same path. The comment above the acquire claimed shielding prevented
exactly that. The rule requires all four conditions so it stays specific.

## CE039

a criterion checker must not return a gating `score=0.0` from an `except OSError` over a path the *task author* named — that books an eval-config error as an agent failure; raise `CheckerMisuseError` instead, and `# noqa: CE039` the cases that really are the agent's

A gating `score=0.0` is a strict AND term in `all_criteria_passed`, and it flows into every
downstream count that consumes criterion scores: `CriterionAggregate` mean/median,
`suite_thresholds` gates on dataset-fanned suites, run and experiment pass rates, the JUnit
report, the evalboard.

The motivating case: a typo in `reference_comparison.reference_file` raised
`FileNotFoundError` (an `OSError`), which became a gating 0.0 and counted against the
agent's pass rate. It silently zeroed every row of a dataset-fanned suite while looking
like a genuine similarity failure.

## CE043

`result_tokens` is the cost simulator's cache-independent measure of tool-output size, so an
agent that clips captured output before storing it silently under-reports every tool
result. The Codex agent shipped with `f"Output: {output[:100]}"`, which pinned ~77% of its
Bash results at ~31 tokens and skewed the cost model.

Storing the output whole is safe because it is already bounded by the harness's own
exec-output truncation; trimming belongs to display, in the renderers and reports.

## CE046

`Agent.get_environment_info` emits the `system_prompt_semantics` marker from the ClassVar of
the same name, so every run, including out-of-tree SPI agents, records which system-prompt
regime built its prompts. Dashboards read an ABSENT marker as "a run from before the marker
existed" and pool it into a legacy bucket, so a bare-dict override does not merely omit a
key: it mis-buckets every run of that agent.

The motivating bug: `OpenCodeAgent.get_environment_info` returned
`{"opencode_model": ..., "opencode_pure": ...}` with no `super()` spread, so no OpenCode run
carried the marker and no test caught it. The base's docstring states the contract
("Overrides should spread `super().get_environment_info()` rather than returning a bare
dict"); the rule makes it mechanical. The base itself is exempt so the rule does not flag
its own source.

## CE047

every onboarding/marketing surface — README, `docs/index.md`, `docs/comparison.md`, `docs/llms.txt`, `mkdocs.yml`'s `site_description`, the Pages stub, and pyproject's `description`/`keywords` — must name every built-in `AgentKind`; OpenCode shipped while four of those seven still listed three harnesses, and nothing failed

## CE048

never call a Typer command function in process — its parameter defaults are `OptionInfo` sentinels, not values, and the sentinel is TRUTHY, so `in_place=None` silently selected the wrong branch; the fix is the `run_pipeline` / `run_evaluation` / `run_plan` split, and this rule is the one that also scans `tests/`, since that is the only place the defect occurs

The failure is silent, which is what makes it worth a rule. `evaluate`'s
`in_place: bool | None = typer.Option(None, "--in-place/--copy")` reads as "no preference"
and selects copy-vs-in-place from the target shape. Called in process, the tests graded in
place and the default they meant to cover was never exercised.

In each half of the split the Typer signature is a thin wrapper and the body is a plain
function with real Python defaults.

## CE049

never coalesce a possibly-unmeasured score to a numeric literal — `score or 0.0` publishes "measured and scored zero" while meaning "never measured", which is how an ungraded night reached four unfiltered `avg(Score)` dashboards as a real zero

The motivating bug: `build_task_event` published
`Score = float(result.weighted_score or 0.0)` on every `CoderEval.Task.End`. Four App
Insights tiles compute `avg(todouble(customDimensions.Score))` with no status filter, so one
`coder-eval execute` night dragged every score tile toward zero, indistinguishable from a
genuinely bad night. `orchestrator.py` documents the same hazard in prose ("every
downstream `score or 0.0` would launder it into a real-looking failure"); the rule makes it
mechanical.

The `# noqa` example, `orchestration/experiment._measured_scores`, sums a variant's scores
where an errored row must count as 0.0; it makes that decision explicitly and states why.

## CE050

no untyped `getattr` probe for a discriminated-union field — pyright cannot see the string, so a rename degrades the guard to a permanent no-op; scoped to criterion-shaped receivers because `command`/`tool`/`prompt` are far too common to flag on their own

The motivating bug: `orchestration/regrade.warn_on_embedded_commands` — the only
disclosure of what shell a rebuilt, untrusted run config would execute on the grader's
host — probed with `getattr(c, "command", None)`. Besides being rename-fragile it
structurally could not name `agent_judge`, the criterion that spawns a tool-using agent
and so has the widest blast radius of all. `models/tasks.py` states the same convention
in prose ("isinstance narrowing, NOT getattr(c, 'files'/'command')"); this rule promotes it
to a gate.

A field-name-only rule would fire a dozen times on agent code that legitimately probes raw
SDK event objects for `command`/`tool`/`prompt`, and would be turned off within a week.
Scoping to the receiver's name catches the real shape (`for c in task.success_criteria:
... getattr(c, "command", None)`). The field list is derived from the models rather than
hardcoded, so it tracks renames instead of going stale.

## CE051

a sandbox driver may not be rewritten silently — the driver IS the isolation boundary, so a downgrade must be an explicit, stamped, operator-visible decision

Rewriting `docker` to `tempdir` behind the caller's back does not degrade gracefully: it
moves execution from a container onto the operator's own machine, where the task's
criteria address paths and toolchains that do not exist. They score 0.0, the row is
written back FAILURE for a trajectory that passed, and the same commands (`rm -rf
/verifier`, `mkdir -p /logs/verifier`) run unsandboxed on the grading host.

The motivating bug: `regrade.grading_sandbox_config` rewrote the driver unconditionally
on BOTH new grading entry points, which also neutralized the `driver: docker` refusal in
`Sandbox.adopt` — a guard added in the same change specifically to catch this. The
legitimate suppressions are the in-container rewrite in `run_task_internal_command` and
the opt-in host-grading branch, which refuses by default and stamps `graded_on_host` on
the row.

## CE052

an `os._exit` must sit inside a branch testing `CODER_EVAL_IN_CONTAINER` — it is the right primitive only for reaping the container's own disposable main process, and `run_task_internal_command` armed its heartbeat watchdog, a daemon thread whose whole authority is `os._exit(137)`, unconditionally: a test that invoked the command in-process left the pytest worker holding that thread, which exited the worker 40s later inside an unrelated test file, naming a different test on each run and on each platform with no traceback — and the dead worker's lost coverage data then failed the gate as `65.13 < 80.00`, naming neither the test nor the cause

`os._exit` bypasses `atexit`, buffered IO, `finally` blocks and every exception handler.
It is safe for the container's main process only because that process is ours to destroy;
in any other process the outcome is not degraded, it is unattributable.

The test that armed the watchdog invoked `run_task_internal_command` in-process on
purpose: the command must refuse a malformed `context.json`, and asserting that means
calling it. The 40 s is the 20 s grace plus the 20 s stale window. The failure was
invisible at low load: with 14 local workers the file finished and the run ended before
the timer fired, so it reproduced only on CI's 2 workers.

`CODER_EVAL_IN_CONTAINER` is the established in-container predicate
(`Sandbox.enforces_permission_windows`, `orchestration/evaluation.resolve_reference_dir`).
A lexical check is enough because the missing property was a guard written down at the
site, not a proof that the guard is correct.

## CE053

no bare run-record or run-LOG filename literal outside `path_utils` — widened to `docker.log` / `grade.docker.log` / `task.log` / `grade.log` after the same shape recurred: `docker.log` was produced in `isolation/` and consumed in `orchestration/` as three unrelated literals, and because the consumer guards its copy with `is_file()`, a rename would have silently discarded the only record of why a grading container failed — `TASK_JSON_FILENAME` shipped with a rename-safety rationale while twelve exact literals stayed unmigrated, including all three `rglob("task.json")` sites the constant's own comment cites as its reason to exist, so it created the second source of truth it argues against

Two half-copies of the same string in different packages is how a rename becomes a silent
no-op on the sites it missed. The unmigrated literals sat in `orchestrator.py`, `batch.py`, `docker_runner.py`,
`reports.py`, `reports_junit.py`, `reports_stats.py` and `report_command.py`, while only
the new modules used the constant. A rationale that only a human remembers is not a rule.

## CE054

an `environment_info` key that is READ must be WRITTEN somewhere in `src/` — the bag is `dict[str, Any]`, so nothing connects reader to writer, and the `reference_digest` anti-cheat guard shipped as a read with no writer anywhere: `.get()` returned `None`, the guard took its early return, and CLAUDE.md plus the user guide both described it as protection it never provided

The reader was `verify_reference_unchanged`, which read
`environment_info.get("reference_digest")` to refuse a re-grade whose answer key had
changed. A whole-tree grep found exactly one occurrence of the key — the read itself — and
every automated gate in the repo was green. The check is one-way because an unread key is
ordinary (recorded for a human or a downstream consumer), while an unwritten key is always
a bug.

## CE055

a criterion `path:` in `tasks/` must be sandbox-relative — an absolute path is joined onto the sandbox root, which DISCARDS the root, so containment refuses it and the criterion can never match whatever the agent does; two in-tree tasks were broken this way and the pair is the argument for a static rule on top of the runtime `CheckerMisuseError`: `byod_smoke_test` IS in a CI bucket and produced only `Results: 7/8 succeeded` plus a gating 0.0 reading "file does not exist" for a file that existed, while `dockerfile_build_example` is in NO bucket, so nothing ran it and no runtime guard was ever reached — the fix is never to relax containment but to say what the criterion means, `run_command: test -f /opt/marker`, a claim about the container IMAGE rather than about the agent's workspace

## CE056

no bare `CODER_EVAL_IN_CONTAINER` literal outside `models/container_paths.py` — the CE053 shape again: a rename-safety constant that shipped beside the literal it replaced, and the straggler was the single WRITER, so a rename would have disarmed four security/correctness gates at once with nothing failing; CE052 cannot catch it because that rule inspects `if` guards and the writer is not one

`models/container_paths.py` states why the constant exists: "two half-copies of the same
string in different packages is how a rename becomes a silent no-op". Every reader moved
to `IN_CONTAINER_ENV`; the writer, `docker_runner`'s `--env CODER_EVAL_IN_CONTAINER=1`,
did not. A rename would have left the container exporting the old name, and each gate
would read "not in a container":

- `Sandbox.enforces_permission_windows`: the reference-solution anti-cheat window stops
  being applied, and an unprotected run scores like a protected one;
- `resolve_reference_dir`: the `/work/references` branch is skipped;
- `_should_grade_in_container`: a grading container dispatches another grading container;
- the orphan-container heartbeat watchdog's `os._exit(137)` gate.

The match is exact or `NAME=`-prefixed, not a bare substring test, because a docstring or
error message names the variable on purpose and the rule must not push authors to
obfuscate their own explanations.

## CE057

a module copied into the recorder directory beside a generated sandbox shim — `models.sandbox.SIDECAR_MODULES`, currently `argv_match.py` — may import stdlib only. The failure is silent: the sidecar runs where `coder_eval` and its dependencies are not installed, so one package import makes every shadowed CLI die with an ImportError the agent reads as "the tool is broken", costing a whole run to diagnose. The rule derives its target set from that exported tuple and a test asserts it matches a file that exists — a lint rule guarding zero files must fail, not pass

Import-time enforcement, a test that renders and executes a shim, catches the defect only
when some test happens to declare a response rule; the lint rule catches it the moment the
import is written. `STDLIB_ALLOWED` is an allowlist rather than a check against
`sys.stdlib_module_names` so that growing the sidecar's import surface is a decision
someone makes on purpose, and it stays small because every entry must exist in whatever
interpreter the sandbox's shebang resolves to.

`from __future__ import ...` falls outside the allowlist but is not an import hazard: every
interpreter that can run the shim supports it. It gets its own message so that the fix is
to drop the line; the generic message would suggest widening the allowlist, which would
retire the rule's own guard on future-import syntax.

## CE058

in `src/coder_eval/`, an unknown timing value may not become a numeric literal — `duration_ms is None` means *never timed* and `0.0` means *timed and instant*, so writing the literal publishes the second while meaning the first. One invariant, one id, six syntactic forms, listed in the rule's docstring. The `model_copy(update={...})` dict form exists because the Antigravity DONE path writes through that shape, which a keyword-only rule cannot see. Antigravity constructed EVERY message with `generation_duration_ms=0.0`, so the task page's Generation cell read `0ms` and its breakdown rendered `0%` for months with nothing failing; Codex published the SDK's `0.0` as a measured command duration, so `avg_command_time_ms` divided real milliseconds by a command count of which 70 of 211 in one nightly had never been timed. The fourth form is the one no existing rule shape covered and is where a live instance was hiding — `claude_code_agent._finalize_commands` set `0.0` on every command force-closed without a tool result, in the one harness a timing audit had called healthy. BLIND SPOT, stated in the rule's docstring: form 1 keys on the callee's spelling, so renaming the `AssistantMessageTelemetry` import alias silently disarms it there

Every consumer downstream of a timing field (an average, a breakdown percentage, a
timeline cell) treats an invented literal as a measurement. This is the same reasoning as
CE049 on the score side.

The rule covers three field families: `*_duration_ms` together with
`total_command_time_ms` / `avg_command_time_ms`; `harness_startup_ms` /
`harness_teardown_ms`; and `tool_union_ms`. `TurnRecord.harness_startup_ms` and
`harness_teardown_ms` are the turn's head and tail buckets, and they are the same
invariant one level up. A turn whose stream carried no assistant message was never timed
at either end. A `0.0` there claims the harness started instantly, and that reading sends
a real gap into the evalboard's `Unaccounted` cell while a named bucket says it was
measured at zero. `tool_union_ms` is the third bucket, under the same None-vs-0.0
contract. A measured `0.0` remains a legitimate answer: a window subtracted down to
nothing by the tool execution inside it, or a clamped inversion where both ends really
were observed.

A sixth form, `cmd.duration_ms = 0.0` as a plain assignment, exists because form 4 passed
the live `_finalize_commands` defect only by coincidence. Form 4 keys on the `if` test
naming a timing attribute, and the shipped bug spelled it `if cmd.duration_ms is None:`.
But the assignment sat inside an outer `if cmd.result_status is None:` block. Setting the
literal under THAT guard instead reads just as naturally, books the identical lie, and is
invisible to forms 1-5. A guard is evidence about the value only when it names the value;
with no such guard there is no evidence at all, which is strictly worse.

Form 6 uses `_zero_literal`, not form 4's broader `_numeric_literal`. Under
`if x is None` the guard proves the value was never measured, so any invented number is a
defect. A bare assignment proves nothing: `cmd.duration_ms = elapsed_ms` is how a measured
value is written, and a literal `1234.0` is a legitimate test factory or replay. Only the
placeholder zero is the tell, the same narrowing form 1 makes for the same reason. Form 4
keeps its wider literal set, so a guarded `= 1234.0` still fires exactly once, from form 4.

## CE059

in `src/coder_eval/agents/`, an `AssistantMessage` may not receive the same `ast.Name` for both `started_at` and `completed_at` — the Antigravity reducer read `datetime.now()` once and passed it as both bounds, so `started_at == completed_at` on 368 of 368 sampled messages. A separate id from CE058 because it is a separate invariant, a zero-length window whatever the duration field says, and one invariant per id is what makes a `# noqa` mean one thing. It does NOT fire when the same call passes `generation_duration_ms=None`: a call that says, in the field built to say it, that no window was measurable is not claiming one — that exemption is what keeps the rule pointed at the misleading case instead of accumulating four permanent suppressions on the rollout-rebuild and sub-agent-synthesis sites

Every consumer that derives a window from the two stamps saw nothing at all on those
messages; a window needs two reads at two moments. The exempt sites are Codex's rollout
rebuild and the sub-agent syntheses on Codex and Claude Code, where the generation arrives
as a tool result and is never streamed, so collapsing both bounds to one `now()` is a
formatting choice, not a false measurement.

The blind spot has a live shape in Codex: `started = _ms_to_dt(self.open_start_ms)` and
`completed = _ms_to_dt(self.open_end_ms if ... is not None else self.open_start_ms)`
collapse to one instant whenever `open_end_ms` is None. `assert_timing_captured` runs the
real reducer on a replay and asserts that a non-zero window came out.

## CE060

in `src/coder_eval/agents/`, every `AssistantMessage(...)` must pass `message_id` explicitly — an identity invariant, which is why it is its own id rather than a second arm of CE058/CE059, both of which are about timing. Antigravity omitted the kwarg, so the field defaulted to `None` on every message it ever recorded, and the evalboard — which groups assistant emissions by `message_id` and falls back to a `SAME_EMISSION_GAP_MS` wall-clock gap when either side lacks one — collapsed a whole turn's generations into ONE timeline row as soon as the harness's generation windows became contiguous (the gap is then exactly 0 ms, always). Nothing failed: the consumer SUMS the group, so the totals and the reconciliation invariant stayed right, and the golden snapshots had ratified the `null` on the day they were written — a snapshot is regenerated from whatever the code currently does, so it catches a later change and never an initial omission. The damage was not confined to the timeline, which is why "only granularity is lost" was the wrong way to describe it: a grouped emission is one API call to the evalboard's thinking-cost simulator, whose prompt-cache cascade is quadratic in that count, so a single-shot Antigravity run had every cascade coefficient pinned at zero; the `Messages` count and the 10 s slow-generation bar were per-turn too. Unlike its two siblings it **derives its constructor set from each module's own `coder_eval.models` imports** instead of hardcoding the spelling, which closes exactly the blind spot CE058's clause concedes: `claude_code_agent.py` binds only `AssistantMessage as AssistantMessageTelemetry`, so a name list guards that file's two construction sites purely by coincidence, and an arbitrary `as Msg` is missed outright. Widening CE058/CE059 the same way changes two shipped rules, needs its own mutation checks, and is recorded in `.claude/harness-candidates.md`. BLIND SPOT, in the rule's docstring: the runtime `None` — the kwarg must be PRESENT, not statically non-`None`, because OpenCode's `messageID` and Pi's `responseId` legitimately evaluate to `None` when the CLI omits them, and passing a fallback expression *is* deciding

The mechanism and its blast radius (evalboard grouping, the thinking-cost simulator) are
specified in `docs/agents/HARNESS_PARITY.md` § Timing capture.

The shared resolver, and why it lives outside this rule, is under `_model_ctor` below.

On the runtime `None`: `opencode_agent.py` passes `str(part.get("messageID") or "") or None`
and `pi_agent.py` the same shape for `responseId`. The golden snapshot
`pi_a_single_text_turn.json` carries that shape, but its `null` comes from a fixture that
emits no `responseId`, not from a live CLI omission.

A `**`-expanded call has no carve-out because no site in `src/coder_eval/agents/` uses
`**` expansion for these constructors.

## CE061

a generation window must come from `coder_eval.timing.close_window` — Pi shipped measuring
its window from its own `turn_start` while four sibling reducers tiled from a mark, so the
wall clock between one turn's end and the next turn's start (the model time that PRODUCED
that turn) fell into no bucket at all. Nothing failed: `docs/agents/HARNESS_PARITY.md`
states the four-bucket identity, and the golden-stream sensor
(`tests/_fixtures/golden_streams/_scrub.py`) checks ONE side of it — it catches a bucket
claiming more time than the turn contains and says nothing about one claiming less. Pi's
own tests passed because they were written against Pi's own arithmetic. The hazard is
therefore not a reducer that computes the window wrongly but one that computes it AT ALL:
a new harness whose author reimplements the arithmetic inline arrives with a green test
suite by construction. `tests/test_timing_identity_contract.py` is the two-sided check.

A separate id from CE058, CE059 and CE060: those are about the VALUES a message carries —
an unknown duration published as a literal, a window built from one clock read, a missing
identity. This one is about PROVENANCE, where the arithmetic came from, and one invariant
per id is what makes a `# noqa` mean one thing.

The rule proves only an import because the value passed to `generation_duration_ms=` is
always a local (`generation_ms`, `gen_parts[idx]`), so no AST rule can trace it back to a
call; it adds the cheap structural half that the arithmetic tests cannot reach — a sixth
harness rolling its own.

Tool subtraction is not part of a window's geometry: a call issued by an earlier emission
can still be running when the next window closes, and folding that into `close_window`
would put a mode flag on a helper whose value is having one shape. The collector already
knows every span, so `timing.subtract_tool_time` removes tool time once for every harness.
CE061 is exemption-free: claude-code calls the same `close_window` as the other reducers
with no suppression.
The explicit-`None` exemption covers codex's rollout rebuild and claude-code's sub-agent
synthesis.

## CE063

no module in `src/coder_eval/agents/` may import `busy_ms` — tool execution comes out of a generation window in exactly ONE place, `timing.py::subtract_tool_time`. Five reducers each did it themselves while the head and tail were computed centrally at the same seam, and that asymmetry is where every timing defect on this branch lived — none of them in the arithmetic, all of them in the bookkeeping AROUND it: when to reset a per-step span list (clearing it at `step_start` wiped a span before the flush could subtract it, a 100% overstatement of that window), when to clear a spent start stamp (a second flush with no intervening start republished the previous span — 3000 ms of generation for a 2000 ms turn), when to advance the mark. A sixth harness reaching for `busy_ms` rebuilds that, and its tool time is then subtracted TWICE — by the reducer and again by the collector — under-reporting generation on one harness only, which takes a corpus comparison to notice. A separate id from CE061 rather than a rebody: CE061 asks where a window's ARITHMETIC came from and all five reducers call `close_window`, so its property is live and unsuperseded; this asks whether a reducer subtracts at all. It deliberately does NOT reuse CE061's `_imports_the_helper`, whose bare-module-import branch exists so `timing.close_window(...)` counts as reaching the helper — inverted into a ban, that branch flags any reducer that imports the `timing` module to call `timing.close_window(...)`; all five reducers today import `close_window` by name, so the ban would fire on the first working call site spelled the other way

The collector subtracts from every window it is handed.

In `_imports_the_helper`'s own words, a rule that missed `timing.close_window(...)` "would
tell an author to change a working call site." CE063 therefore keys on the `busy_ms` name
binding plus an `ast.Attribute` match for `timing.busy_ms`.

`tests/test_timing_identity_contract.py` drives every harness off a scripted clock and
asserts that the four buckets tile the turn to the millisecond. CE063 adds only the cheap
structural half that a static check can reach.

## CE064

in `src/coder_eval/agents/`, a module that imports `TurnClock` must pass an explicit `timestamp=` to `AgentStartEvent` and `AgentEndEvent` — the turn's OUTER bounds, which no other rule looks at, since CE058-CE061 all scope to `AssistantMessage` and the bracket is not one. `timing.decompose_turn` produces `harness_startup_ms` / `harness_teardown_ms` by subtracting a generation-window bound from a bracket timestamp, so the two must share a basis; all three clocked harnesses derived their bounds from the `TurnClock` and let the bracket fall back to `StreamEvent.timestamp`'s `default_factory=datetime.now`, putting a monotonic-derived stamp and a raw wall stamp inside one subtraction — the exact split `TurnClock` exists to remove, reintroduced at the one seam the clock did not own. Measured on a live antigravity turn: an `AgentEndEvent` stamped **17 us BEFORE its own last message finished**, which cannot happen (the event is constructed strictly after the final flush), and `decompose_turn` clamped that negative and published `0.0` — "measured, and instant", the CE058 confusion arrived at from the other direction — for a harness whose real tail is ~0.1 ms; the fixed code records 0.035 ms. It surfaced on one harness only because the drift is tens of microseconds and antigravity is the only one that holds its process across turns, so nothing happens between its last flush and its end event; every other harness books a tail of 7-543 ms, where the drift is invisible rather than absent — which is why the fix is at every clocked site rather than at that one. SCOPE IS DERIVED, never a harness list: codex and opencode take their spans from the CLI's own epoch stamps, deliberately have no `TurnClock`, and are correctly invisible to the rule — a raw bracket is CONSISTENT with their bounds — and the day either adopts a clock the rule starts applying with no edit. BLIND SPOT, in the rule's docstring: presence, not correctness. It cannot tell `self.clock.now()` from a `datetime.now()` spelled out at the call site, because the three harnesses legitimately reach their clock three ways; the guard for the SOURCE is behavioural (`tests/_bracket_clock.py` injects a stand-in anchored a year from real time, so a reverted argument fails by a year rather than by the microseconds that separate the two clocks), which is the division of labour CE060 states — a rule removes the SILENT case, a default nobody chose

The measurement came from instrumenting `decompose_turn` on a live antigravity turn:

```
PROBE tail: elapsed=-0.017000ms busy=0.000000ms raw=-0.017000ms
            last_completed = 09:05:22.033099
            agent_end      = 09:05:22.033082
```

Every other harness also books a head of 0.2-6 s. The fix belongs at every clocked site
because that makes the subtraction single-basis rather than usually-close, and
"usually-close" is not a property a millisecond field can rest on. The noop agent has no
windows at all.

The three clock expressions are a local `clock` in `communicate`, `state.clock` from the
caller, and `self.clock` inside the state. Demanding one spelling would make the rule a
syntax check on three harnesses' internal structure.

## _model_ctor

CE060 and CE061 ask the same first question: is this call building an `AssistantMessage`?
Answering it takes more than matching a name. A module may bind the class under any
alias, reach it through a relative import, or never bind it and spell it
`models.AssistantMessage(...)`. CE060 worked that out first. Duplicating the resolver into
CE061 means a model rename or a new import spelling needs two fixes in two rules, and the
second fix is the one that gets missed. CE064 asks the identical question about
`coder_eval.streaming.events` and `coder_eval.timing`, which is why `bindings_from` takes
the module as a parameter instead of a third copy.

The class name is taken from the model (`AssistantMessage.__name__`) rather than written as
a string, the way CE056 imports `IN_CONTAINER_ENV` and CE057 derives its target set from
`SIDECAR_MODULES`: renaming the model moves the rules with it. Alias resolution removes the
*local binding* spelling, not every rename; taking the name from the model covers the rest.
