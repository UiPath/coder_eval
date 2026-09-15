# Lint rules

> Conventions and authority order: see [README.md](README.md).

A rule's module docstring (or, for a doc-surface or whole-tree rule, its
`@pytest.mark.lint` class in `tests/test_custom_lint.py`) states the invariant, the scope,
the exemptions and the blind spots. This file does not repeat them. It holds only why each
rule exists: the defect that caused it, the measurements behind it, and why each trade-off
was chosen.

## CE004

CE004 once borrowed CE066's core predicate whole and inherited its `reports/` exemption.
Nothing had imported `cli` from `reports/` yet, so the hole was latent rather than live.
Re-enumerating the packages in the rule is what let that list rot in the first place,
which is why the scope now comes from `_layers`.

`harbor/` belongs in scope because its reward writer raises a plain exception
(`RewardWriteSkippedError`, or the re-exported `RegradeError`) and lets the CLI wrap it
into an exit code — the `orchestration/regrade.py` -> `evaluate` shape.

## CE009

Without `extra='forbid'` a misspelled key, for example `directry: foo/` for `directory:
foo/` on `ReferenceSource` or a typo on a criterion field, is dropped with no signal that
it was ignored. Forbidding extras surfaces the typo at load time with the concrete field
name.

Result and persistence models are exempt as deliberate round-trip leniency.

Base chains stop at the file boundary because that keeps the rule cheap: it trusts the
convention that every model class in the scoped files eventually derives from `BaseModel`.

## CE013

The 2026-05-20 judge refactor replaced text-with-JSON verdict parsing with a typed tool
channel (`submit_verdict`). The legacy parser ran regex against structural transcript tags
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
boundary is mechanically detectable, so the rule guards it.

## CE021

`task.json` is the harness's always-produce/always-consume artifact: the only thing that
crosses the container boundary, and the per-task record every dashboard and timeline reads.
A bare `EvaluationResult.model_validate_json(text)` turns a present-but-malformed file
into an uncaught exception that crashes the run. Two causes produce such a file: schema
skew between a stale `:latest` image and the host that the image preflight did not catch,
and a truncated or torn write. The incident was at `docker_runner.py`: the parse re-bucketed
the task to a non-persisted in-memory ERROR with no per-task report. The fix degrades:
catch `ValueError` and persist a synthetic ERROR record (`batch.py::_load_completed_result`,
`recover_task_results`, `docker_runner.py::_handle_malformed_task_json`).
`recover_task_results` catches `(OSError, ValueError)`, which is why a tuple member counts
as a guard.

Scope stays narrow to `EvaluationResult` because it is the always-produce/consume
contract.

## CE022

`Orchestrator._simulation_dialog_loop` is a sequential dialog driver whose residual length
is irreducible without a state-object rewrite (the 2026-06-23 decompose-god-functions
plan, Phase 5), so it keeps its `# noqa: PLR0915`.

`_CAP` is the measured post-decomposition count (122) plus 6 headroom: ordinary edits do
not trip it, a real regrowth does. Ruff's ceiling for every other function is 80/25.

## CE024

A bare `A | B | C` union of tagged Pydantic models validates via smart-union: input with a
missing or typo'd `type` tag silently coerces to whichever variant happens to fit, instead
of raising a crisp discriminator error. The `SuccessCriterion` union shipped this way —
tag-less criterion dicts coerced to the structurally-nearest variant — and the fix wraps it
in `Annotated[..., Field(discriminator="type")]`.

The callable `Discriminator(...)` form is accepted because `CriterionResultUnion` in
`models/results.py` uses it.

## CE026

Several surfaces introduce the same composite Action — `README.md`, `docs/CI_GATE.md`,
`docs/tutorials/02-ci-pipeline.md`, and the plugin's `ci` skill, whose emitted workflow
users copy verbatim — and each was hand-maintained, so they drifted. The motivating bug:
`docs/CI_GATE.md` claimed "there is nothing to install" and offered a copy-pasteable
`uses:` step with no agent runtime. The action is agent-agnostic, so an integrator who
copied it got a run that died on a missing `claude` binary. The correcting paragraph was
11 lines away; the tutorial's snippet showed the prerequisite steps, the reference
page's did not.

Only the first Action block on a page is held to prerequisite parity because it is the
page's quickstart; later blocks are single-input illustrations, and skipping them is what
keeps the rule quiet.

The zero-install clause does not judge whether a paragraph 11 lines later states a real
prerequisite: that is semantic reasoning no static rule should attempt. It forces the
absolute to be scoped where it is written instead ("no Marketplace install step").

Slug parity exists because `action.yml`'s `name:` is the Marketplace listing title, and a
rename silently 404s every Marketplace link and leaves every badge naming the old listing.

Input parity exists because GitHub does not fail a workflow on an unknown input: a
renamed input leaves every snippet promising something the step does not do — silently,
and worst in the `ci` skill, whose output lands in other people's repositories where
this repo's CI never sees it.

## CE027

A documented env var whose name matches no `Settings` field or `AliasChoices` is dropped
with zero signal. That is the failure behind the `CODER_EVAL_API_BACKEND` doc bug: the real
field is `API_BACKEND`, so the prefixed spelling selected no backend and the run fell back
to Direct Anthropic. The framework also reads a handful of vars directly through
`os.getenv` (for example `CODER_EVAL_SKILLS_DIR`, `CODEX_BASE_URL`,
`CODER_EVAL_IN_CONTAINER`), and those are legitimately documentable, so a `src/` consumer
also backs a name.

Prose mentions are not scanned because they are too prone to false positives (markdown
links like `CODEX_AGENT_GUIDE.md`, secret references like `secrets.BEDROCK_TOKEN`,
regex-pattern examples like `API_KEY = "…"`), and `NAME=value` is the form users copy into
a workflow, so it carries the real risk. Broad third-party namespaces (`AWS_`,
`ANTHROPIC_`, `GEMINI_`, `GITHUB_`, `EVALBOARD_`, `PLUGIN_`) are skipped because SDKs and
CI consume them, not necessarily through `Settings`, so scanning them gives false
positives on names that are legitimately external.

The `src/` scan recognises a consumer through a named constant because
`CODER_EVAL_IN_CONTAINER` has one definition (`models/container_paths.py::IN_CONTAINER_ENV`)
and its consumers spell it `os.environ.get(IN_CONTAINER_ENV)`. A literal-only scanner
reports the repo's own gate as unbacked and pushes the author to paste the literal back:
the scanner argues against the SSOT it should reinforce. Resolution is two-step so the scan
stays strict: a constant that nothing reads is still unbacked.

## CE028

The docs overhaul's root cause was doc/code drift, and the flat index surfaces drift the same
way: a page is added to the nav and forgotten in the three flat lists, or a page is deleted
and left dangling in them. Generating them from the nav removes the second source.

A separate `--check` entry point would be untested duplication, so CE028 is the only
checker. The "every published `docs/*.md` is in the nav" check is the one that would have
caught the overhaul's whole bug class. The `docs/tutorials/README.md` table carries a
"you'll learn" column the nav does not, so generating it would need a second per-page
field and bring back a dual source of truth.

## CE029

A published example that does not parse is worse than no example: readers copy it, hit a
`ValidationError`, and conclude the feature is broken. The rule caught exactly that. The
`prompt_mutations` recipe in `docs/AB_EXPERIMENTS.md` used `text:` where the field is
`content:`, and every mutation model declares `extra="forbid"`, so the published snippet
raised `variants.1.prompt_mutations.0.suffix.content Field required`.

Scope is deliberately narrow: a false positive on an illustrative fragment would make
`make lint` a nuisance and get the rule deleted. A bare `success_criteria:` list is the
single most common doc shape, and the task guide's overview block uses the schematic
`agent: { ... }` form on purpose. The `<!-- lint-skip: doc-yaml -->` marker is for an
example that is intentionally partial in a way the heuristic cannot see.

## CE030

The defects that a docs overhaul fixed were all one failure: a doc claim that does not
match the code. The two worst (P0 and P1) were "a Pydantic field the user must set,
documented nowhere." CE030 is the sensor that keeps that class from coming back, for a
small, explicit registry of user-facing models.

Nested models are not walked because that would silently expand the documentation
commitment to dozens of models nobody signed up for (`AgentConfig`, `SandboxConfig`,
criteria). `CliMatch` is absent for that reason: its fields are documented in the
`cli_called` reference, and registering a third nested model under `SandboxConfig` would
start exactly that tree-walk.

The inline-code match stays simple because the rule exists to catch *entirely
undocumented* fields; a fuzzier "documented in the right section" rule invites false
passes that erode trust in the gate.

`RecordedCli` and `CliResponse` both declare `exit_code` / `stdout` / `stderr`, so
registering the second newly guards only `when`. Registering both is still net-positive.

## CE031

A config field that users set in a task YAML but that no code reads is dead config: it
silently does nothing, and the author has no way to know. `SimulationConfig.parallel_trials`
was that — documented, set in a shipped task YAML, defaulting to `True`, and read nowhere
(trial concurrency is entirely `--max-parallel`'s job).

"Consumed" means an attribute access by name because that is how a behavior-driving config
takes effect: the orchestrator and validators must read the field. `TaskDefinition` stays
unregistered because the dataset expander round-trips its fields through `model_dump`.

The name-collision blind spot is accepted because a sensor with false negatives only never
wrongly breaks the build.

## CE032

`Sandbox.resolve_files` is the single place criterion `path` semantics live: literal-first
resolution (a real file named `report[2024].json` is not reinterpreted as a character
class), glob expansion for artifacts whose location the prompt does not pin, ignore-pattern
filtering (`.venv` / `node_modules` / `dist` cannot be graded as agent output), and
exactly-one enforcement on content reads.

A checker that builds its own path with `sandbox.sandbox_dir / <field>` and reads it
directly silently opts out of all of that, so path semantics differ per criterion. That is
how `reference_comparison.agent_file` drifted from every other path field.

## CE033

An installed Claude Code plugin is copied to `~/.claude/plugins/cache/` without its parent
directories, so a skill cannot read `docs/TASK_DEFINITION_GUIDE.md` at runtime — every
reference a skill needs ships inside `plugins/coder-eval/`. A bundled copy of the
criterion vocabulary is exactly the kind of file that drifts: a criterion gains a field,
or a new criterion lands, and the copy keeps teaching the old schema to every plugin user.
A separate `--check` entry point would be untested duplication.

A hardcoded inherited-field list would be a second declaration of the base schema. When
`stop_early:` replaced `stop_when` + `max_steps_to_decide`, such a list would have
rendered the new field into all 14 per-criterion sections and leaked two dead names; the
computed set absorbed the change with no edit.

Full descriptions matter because what a field means is the half of the schema an authoring
agent gets wrong (that `min_count: 0` lets a criterion pass when nothing matched, that
`weight: 0` makes a criterion informational). A truncated or curated subset would need a
hardcoded name list, and so a second declaration of the schema. Defaults and types are
left out because rendering defaults means handling `default_factory` (whose
`FieldInfo.default` is `PydanticUndefined`), and rendering types means normalizing
`X | None` annotations — two helpers serving the half of the reference an authoring agent
needs least. `coder-eval plan` and the model docstrings cover the rest.

## CE034

`require_success` defaults to False, so a `command_executed` criterion counts an
invocation that CRASHED. On an unarmed criterion that is merely generous. On an armed one
it corrupts the run's verdict, because three behaviours compose:

1. `live_verdict` and `_check_impl` share `_matching_commands`, so a failed invocation
   live-PASSES a positive criterion (`min_count > 0`, no `max_count`) the moment it is
   observed;
2. `stop_early.on_pass: stop` ends the run on that pass — and `decide_within` latches
   it, so the timeout never fires either;
3. gating is FIRED-ONLY: a run the watcher cut gates on the ARMED SUBSET
   (`armed_criteria_passed`), so unarmed criteria are never consulted.

The defect this caught was in `tasks/early_stop_weighted_low_weight_absorbed.yaml`: an
agent that ran `python app.py` BEFORE it created app.py scored a weighted 1.0 over the
armed subset and reported SUCCESS — with no app.py and a crashed script — because the
unarmed `file_exists` was bypassed. Running the plugin's own `lint-tasks` skill against
this repository's tasks found it.

Pass-capability is read off the model's `live_decidable_polarities()` so the rule does not
re-derive the shape a second time.

## CE035

The motivating bug shipped in `verify-published-action.yml`: two steps read
`steps.parity.outputs.version`, but the `parity` step writes only `pin` / `newest` /
`lagging` (the shell variable was `VERSION`, the output key was `newest`). GitHub expands
an unwritten output to the empty string, so `TAG_REF: v${{ steps.parity.outputs.version }}`
became the bare string `v`, `git show "v:action.yml"` exited 128 under
`set -euo pipefail`, and the preflight job was red on 100% of triggers. Through
`needs: preflight`, the paid end-to-end tier could never run at all.

Nothing caught it. The workflow is invisible to ruff, pyright, pytest and the AST lint
runner, and `actionlint` models `steps.*.outputs` as an open string map, so an unwritten
shell key is untyped and unflagged there too.

A missing step id or an undeclared `needs` output is always a finding because both are
fully enumerable from the file.

## CE036

The contract is documented on `LiveVerdict` / `BaseCriterion.live_verdict`
(`criteria/base.py`), but nothing enforced it before CE036: a criterion (in-tree or
plugin) that implements `live_verdict` non-monotonically type-checks, passes CE025, and
silently corrupts `EarlyStopWatcher`'s deferred fail-stop, verdict latching and
`_prev_verdicts` flip-attribution — it latches a verdict the run then contradicts. See
GitHub issue #61 item 2. Why replay is the only sound check is in
`contracts.md § The live_verdict contract` (subsection "Why replay is the only sound
check"); it is not repeated here.

### Design choices

Fixtures are mandatory. A property test over random trajectories returns `"undecided"`
almost always and passes vacuously. So each live type supplies cases in `CASES`, and
`missing_case_types`, driven by the `SuccessCriterion` union like CE025, fails when a
new `LiveSuccessCriterion` has none. The author demonstrates the contract in the same
change that adds the criterion.

Each case declares what it reaches. `ContractCase.reaches` pins the verdict on the FULL
trajectory, so a fixture that stops exercising its decision path (a renamed tool, a
changed regex) fails loudly instead of degrading into a vacuous all-`undecided` replay.

Polarity honesty. `live_decidable_polarities` is documented as a subset of what the
checker's `live_verdict` can emit for that instance. A case that terminally decides a
polarity the instance does not claim is a real bug: the watcher treats that trigger as
inert while the checker decides it.

It is a pytest class because it reasons over the criteria registry and executes checkers,
which an AST-only runner cannot do.

### Honest limits, expanded

The rule raises the cost of the bug and puts the contract in front of the next
implementer; it does not close the hole, and nothing short of a proof would. A plugin
copies the pattern — a `ContractCase`-style fixture plus the prefix walk — because this
module lives under `tests/` and is not shipped in the wheel; this module is the reference
implementation. The determinism probe does catch RNG and per-call mutable state. For a
`datetime.now()` dependency the monotonicity replay is a likelier tripwire, and only if
the fixture straddles the flip.

### The prefix walk

A raise is reported as a labeled violation (case and prefix length) instead of crashing
the walk, and the remaining prefixes still replay, so one bad prefix does not mask
breaches elsewhere. The watcher runs mid-turn, where a raise takes down the stop logic;
the shape `command_executed` pins for a malformed regex — degrade to `"undecided"`,
never raise — is the contract for every implementation. The terminal verdict is `None`
when the last prefix raised because there is then no verdict to compare against, and the
previous prefix's stale value would stack a bogus `reaches` breach on the real one.

### Seeded permutations

`contract_violations` walks one ordering, the one the fixture author wrote, but the
contract quantifies over any trajectory. The orderings an author does not think of are
where an order-sensitive bug hides — a verdict computed from the latest command instead
of the accumulated set can look monotone on the authored ordering and flip on a
reordering. Seeded shuffles probe those orderings for free.

Each shuffle is renumbered because `EarlyStopWatcher._collect_verdicts` keeps its partial
trajectory sorted by `sequence_number`, so `live_verdict` never sees a list whose order
contradicts those numbers. Without the renumber the layer reports breaches on inputs the
watcher cannot construct, and it degrades to a silent no-op for any checker that sorts by
`sequence_number` itself — the shuffle sorts straight back to the authored ordering.
`case.reaches` and polarity honesty are not checked on shuffles because pinning either
makes the layer unsound for exactly the order-sensitive criteria it exists to probe.

## CE037

A helper whose docstring documents a bug the live code still has is worse than none.

The motivating `_rmtree_restrictive` explained, correctly and in detail, why
`rmtree(..., ignore_errors=True)` orphans a mode-000 reference tree, while both live
cleanup sites called exactly that. A reader auditing the cleanup path found a function
asserting the shipped code was broken, and a test that called the helper directly made the
real path read as covered.

The rule turns "helper written, never wired" into a `make lint` failure at the commit that
introduces it, which is the only moment anyone knows where it was meant to be called from.

Scope is narrow so the rule stays a bug detector rather than a style nag: methods are
reached via `self`, which a name search cannot see; public names have out-of-tree callers;
dunders are protocol; a decorator (`@register_criterion`, `@field_validator`,
`@app.command`) is a registration. A whole-tree text search rather than an import graph
means a helper referenced anywhere — called, passed as a callback, aliased — counts as
wired, and a name that merely appears in a docstring is an accepted false negative for a
rule that must never block a legitimate refactor.

## CE038

In the leaking shape the awaiting coroutine receives `CancelledError`, propagates it out
of `__aenter__`, and never reaches the `finally`.

The motivating bug held a reference directory at mode 000 with no matching restore: the
directory stayed unreadable for the rest of the run, and a stale registry entry poisoned the
next window on the same path. The comment above the acquire claimed shielding prevented
exactly that. The rule requires all four conditions so it stays specific.

## CE039

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

Storing the output whole is safe because the harness's own exec-output truncation already
bounds it.

## CE044

Eight fields are byte-identical duplicates across `marketplace.json` and `plugin.json`
(`name`, `displayName`, `description`, `keywords`, `author`, `homepage`, `repository`,
`license`), and nothing else compares them: the only other test that reads `plugin.json`
is `tests/test_action_version_pin.py`, and it reads only `version`. A one-sided edit, such
as retitling the plugin in the marketplace but not in the manifest, ships silently and
shows two different one-liners in the wild.

The allowlist half is the one that has already bitten. The marketplace schema allows both
`keywords` ("Tags for plugin discovery and categorization") and `tags` ("Tags for
searchability and discovery"); the plugin-manifest schema has no `tags` property. Split
discovery strings drop half of them from the installed copy, and leave a future editor
with no rule for which list a new term belongs in. The `MARKETPLACE_ONLY` allowlist keeps
that decision in code, not in tribal knowledge.

## CE045

Probed against the real CLI, from a cwd that is not the skill's own repo (project
discovery would otherwise find the skill regardless of `--plugin-dir`, and the namespace
prefix is the real signal):

    claude --plugin-dir <root>/skills  ->  nothing
    claude --plugin-dir <root>         ->  `root:probe-beta`

The cost is invisible and total: every activation suite the plugin generated reported
recall 0.0, which the bundled template's own comment calls "reads exactly like a broken
skill", and `ci` wrote the same path into users' SCHEDULED workflows, where it renders as a
permanent red indistinguishable from the drift the schedule exists to detect.

Six wrong-value lines across five files shipped at once: `docs/PLUGIN.md`, tutorial 07,
`activation.yaml` (comment and example), `check-skill`, and `ci`. Nothing held them in
agreement, which is why they drifted together.

## CE046

`Agent.get_environment_info` emits the `system_prompt_semantics` marker from the ClassVar of
the same name, so every run, including out-of-tree SPI agents, records which system-prompt
regime built its prompts. Dashboards read an ABSENT marker as "a run from before the marker
existed" and pool it into a legacy bucket, so a bare-dict override does not merely omit a
key: it mis-buckets every run of that agent.

The motivating bug: `OpenCodeAgent.get_environment_info` returned `{"opencode_model": ...,
"opencode_pure": ...}` with no `super()` spread, so no OpenCode run carried the marker and
no test caught it. The base's docstring states the contract ("Overrides should spread
`super().get_environment_info()` rather than returning a bare dict"); the rule makes it
mechanical.

## CE047

OpenCode shipped while four of the seven surfaces still listed three harnesses, and
nothing failed.

The roster is restated in prose on surfaces that nothing ties to the code, so adding a
harness means remembering all seven. A reader, a crawler or an LLM answering "which agents
does Coder Eval support?" is told the missing agent does not exist, and the surface quietly
under-sells the framework. The rule only makes "we forgot this harness exists" impossible to
ship.

Plugin agents are out of scope because a built-in is what these surfaces promise.

## CE048

The failure is silent, which is what makes it worth a rule. `evaluate`'s
`in_place: bool | None = typer.Option(None, "--in-place/--copy")` reads as "no preference"
and selects copy-vs-in-place from the target shape. Called in process, the tests graded in
place and the default they meant to cover was never exercised.

The fix is the `run_pipeline` / `run_evaluation` / `run_plan` split. In each half the
Typer signature is a thin wrapper and the body is a plain function with real Python
defaults. Tests are in scope because that is the only place the defect occurs.

## CE049

The motivating bug: `build_task_event` published
`Score = float(result.weighted_score or 0.0)` on every `CoderEval.Task.End`. Four App
Insights tiles compute `avg(todouble(customDimensions.Score))` with no status filter, so one
`coder-eval execute` night dragged every score tile toward zero, indistinguishable from a
genuinely bad night. `orchestrator.py` documents the same hazard in prose ("every
downstream `score or 0.0` would launder it into a real-looking failure"); the rule makes it
mechanical.

## CE050

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
... getattr(c, "command", None)`).

## CE051

The driver IS the isolation boundary. Rewriting `docker` to `tempdir` behind the caller's
back does not degrade gracefully: it moves execution from a container onto the operator's
own machine, where the task's criteria address paths and toolchains that do not exist.
They score 0.0, the row is written back FAILURE for a trajectory that passed, and the same
commands (`rm -rf /verifier`, `mkdir -p /logs/verifier`) run unsandboxed on the grading
host.

The motivating bug: `regrade.grading_sandbox_config` rewrote the driver unconditionally
on BOTH new grading entry points, which also neutralized the `driver: docker` refusal in
`Sandbox.adopt` — a guard added in the same change specifically to catch this. The
legitimate suppressions are the host-side staging rewrite in `docker_runner._stage_inputs`
and the opt-in host-grading branch, which refuses by default and stamps `graded_on_host` on
the row.

## CE052

`run_task_internal_command` armed its heartbeat watchdog — a daemon thread whose whole
authority is `os._exit(137)` — unconditionally. A test that invoked the command in-process
left the pytest worker holding that thread, which exited the worker 40 s later inside an
unrelated test file, naming a different test on each run and on each platform with no
traceback. The dead worker's lost coverage data then failed the gate as `65.13 < 80.00`,
naming neither the test nor the cause.

`os._exit` also drops buffered IO. Outside the container's own disposable main process,
its outcome is not degraded, it is unattributable.

The test that armed the watchdog invoked `run_task_internal_command` in-process on
purpose: the command must refuse a malformed `context.json`, and asserting that means
calling it. The 40 s is the 20 s grace plus the 20 s stale window. The failure was
invisible at low load: with 14 local workers the file finished and the run ended before
the timer fired, so it reproduced only on CI's 2 workers.

`CODER_EVAL_IN_CONTAINER` is the established in-container predicate
(`Sandbox.enforces_permission_windows`, `orchestration/evaluation.resolve_reference_dir`).
A lexical check is enough because the missing property was a guard written down at the
site.

## CE053

The rule was widened to the four run-log names after the same shape recurred: `docker.log`
was produced in `isolation/` and consumed in `orchestration/` as three unrelated literals,
and because the consumer guards its copy with `is_file()`, a rename would have silently
discarded the only record of why a grading container failed. The original defect:
`TASK_JSON_FILENAME` shipped with a rename-safety rationale while twelve exact literals
stayed unmigrated, including all three `rglob("task.json")` sites the constant's own
comment cites as its reason to exist, so it created the second source of truth it argues
against.

Two half-copies of the same string in different packages is how a rename becomes a silent
no-op on the sites it missed. The unmigrated literals sat in `orchestrator.py`, `batch.py`, `docker_runner.py`,
`reports.py`, `reports_junit.py`, `reports_stats.py` and `report_command.py`, while only
the new modules used the constant. A rationale that only a human remembers is not a rule.

## CE054

The `reference_digest` anti-cheat guard shipped as a read with no writer anywhere:
`.get()` returned `None`, the guard took its early return, and CLAUDE.md plus the user
guide both described it as protection it never provided.

The reader was `verify_reference_unchanged`, which read
`environment_info.get("reference_digest")` to refuse a re-grade whose answer key had
changed. A whole-tree grep found exactly one occurrence of the key — the read itself — and
every automated gate in the repo was green.

## CE055

Two in-tree tasks shipped an absolute criterion `path:`, and the pair is the argument for
a static rule on top of the runtime `CheckerMisuseError`. `byod_smoke_test` IS in a CI
bucket and produced only `Results: 7/8 succeeded` plus a gating 0.0 reading "file does not
exist" for a file that existed. `dockerfile_build_example` is in NO bucket, so nothing ran
it and no runtime guard was ever reached.

The two broken tasks: `tasks/byod_smoke_test.yaml` checked `/opt/byod_marker`, and the
real cause sat in a warning inside a task log;
`tasks/dockerfile_build_example/dockerfile_build_example.yaml` checked `/opt/greeting.txt`
and `/opt/secret_check.txt`.

## CE056

The CE053 shape again: a rename-safety constant shipped beside the literal it replaced,
and the straggler was the single WRITER, so a rename would have disarmed four
security/correctness gates at once with nothing failing.

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

Prose that names the variable is left alone because a docstring or error message names it
on purpose, and the rule must not push authors to obfuscate their own explanations.

## CE057

The failure is silent: the sidecar runs where `coder_eval` and its dependencies are not
installed, so one package import makes every shadowed CLI die with an ImportError the
agent reads as "the tool is broken", costing a whole run to diagnose.

Import-time enforcement, a test that renders and executes a shim, catches the defect only
when some test happens to declare a response rule; the lint rule catches it the moment the
import is written. `STDLIB_ALLOWED` stays small because every entry must exist in whatever
interpreter the sandbox's shebang resolves to.

`from __future__ import ...` is not an import hazard: every interpreter that can run the
shim supports it. Widening the allowlist for it would retire the rule's own guard on
future-import syntax.

## CE058

The `model_copy(update={...})` dict form exists because the Antigravity DONE path writes
through that shape, which a keyword-only rule cannot see. Antigravity constructed EVERY
message with `generation_duration_ms=0.0`, so the task page's Generation cell read `0ms`
and its breakdown rendered `0%` for months with nothing failing. Codex published the SDK's
`0.0` as a measured command duration, so `avg_command_time_ms` divided real milliseconds
by a command count of which 70 of 211 in one nightly had never been timed. The fourth form
is the one no existing rule shape covered, and it is where a live instance was hiding:
`claude_code_agent._finalize_commands` set `0.0` on every command force-closed without a
tool result, in the one harness a timing audit had called healthy.

Every consumer downstream of a timing field (an average, a breakdown percentage, a
timeline cell) treats an invented literal as a measurement. This is the same reasoning as
CE049 on the score side.

`TurnRecord.harness_startup_ms` and `harness_teardown_ms` are the turn's head and tail
buckets, and they are the same invariant one level up. A turn whose stream carried no
assistant message was never timed at either end. A `0.0` there claims the harness started
instantly, and that reading sends a real gap into the evalboard's `Unaccounted` cell while
a named bucket says it was measured at zero. `tool_union_ms` is the third bucket, under
the same None-vs-0.0 contract. A measured `0.0` remains a legitimate answer: a window
subtracted down to nothing by the tool execution inside it, or a clamped inversion where
both ends really were observed.

A sixth form, `cmd.duration_ms = 0.0` as a plain assignment, exists because form 4 passed
the live `_finalize_commands` defect only by coincidence. Form 4 keys on the `if` test
naming a timing attribute, and the shipped bug spelled it `if cmd.duration_ms is None:`.
But the assignment sat inside an outer `if cmd.result_status is None:` block. Setting the
literal under THAT guard instead reads just as naturally, books the identical lie, and is
invisible to forms 1-5. A guard is evidence about the value only when it names the value;
with no such guard there is no evidence at all, which is strictly worse.

Form 6 uses `_zero_literal`, not form 4's broader `_numeric_literal`. Under `if x is None`
the guard proves the value was never measured, so any invented number is a defect. A bare
assignment proves nothing: `cmd.duration_ms = elapsed_ms` is how a measured value is
written, and a literal `1234.0` is a legitimate test factory or replay. Only the
placeholder zero is the tell, the same narrowing form 1 makes for the same reason.

## The CE id space

CE062 IS DELIBERATELY UNUSED and must stay that way — the ids in `runner.py` jump 061 to
063. It was claimed during the turn-timing work and then folded into CE063 rather than
shipped. An id is a permanent documentation anchor: a suppression comment carrying 062 in
an older branch, review or commit message must never start meaning something new. CE023 is
retired the same way, after the rule was deleted with the package it guarded.

Claim 068 next, and note 065 IS TAKEN without being in `ALL_RULES`: doc-surface and
whole-tree rules are `@pytest.mark.lint` classes in `tests/test_custom_lint.py` rather than
`BaseRule`s, so `runner.py`'s uniqueness assert cannot see them. Enumerating them in a
comment is how that note fell behind CE044, so grep instead:
`grep -E '^class Test(CE[0-9]{3})' tests/test_custom_lint.py`. Spell it `[0-9]`, not `\d`
— GNU and BSD `grep -E` read `\d` as a literal `d` and report zero hits, which reads as
"no ids taken". The whole id space is unioned in one place, by
`TestRuffExternalCoversEveryRule._known()`.

### CE058 field families

THREE field families, not two. `tool_union_ms` is the turn's third wall-clock bucket, on
the same model and under the same None-vs-0.0 contract as the `harness_*` pair — and it
matched NO arm of `_TIMING_NAME`, so `TurnRecord(tool_union_ms=0.0)` was invisible although
`TurnRecord` was already in `_TIMING_CONSTRUCTORS`. Naming the field
`tool_union_duration_ms`, to inherit the generic `_duration_ms` arm for free, was
considered and rejected: the two fields beside it needed their own arm for exactly this
reason, and one spelling across the four buckets is worth two lines of regex.

The `_startup_ms` / `_teardown_ms` arms need a leading segment for the same reason the
`_duration_ms` arm does: the shipped fields are `harness_*`, and a bare `startup_ms` is
more likely a budget than a measurement.

## CE059

The Antigravity reducer read `datetime.now()` once and passed it as both bounds, so
`started_at == completed_at` on 368 of 368 sampled messages. It is a separate id from
CE058 because it is a separate invariant — a zero-length window whatever the duration
field says — and one invariant per id is what makes a `# noqa` mean one thing. The
`generation_duration_ms=None` exemption keeps the rule pointed at the misleading case
instead of accumulating four permanent suppressions on the rollout-rebuild and
sub-agent-synthesis sites.

Every consumer that derives a window from the two stamps saw nothing at all on those
messages; a window needs two reads at two moments. The exempt sites are Codex's rollout
rebuild and the sub-agent syntheses on Codex and Claude Code, where the generation arrives
as a tool result and is never streamed, so collapsing both bounds to one `now()` is a
formatting choice, not a false measurement.

The two-names blind spot has a live shape in Codex: `started =
_ms_to_dt(self.open_start_ms)` and `completed = _ms_to_dt(self.open_end_ms if ... is not
None else self.open_start_ms)` collapse to one instant whenever `open_end_ms` is None.

## CE060

An identity invariant, which is why it is its own id rather than a second arm of
CE058/CE059, both of which are about timing. Antigravity omitted the kwarg, so the field
defaulted to `None` on every message it ever recorded, and the evalboard — which groups
assistant emissions by `message_id` and falls back to a `SAME_EMISSION_GAP_MS` wall-clock
gap when either side lacks one — collapsed a whole turn's generations into ONE timeline
row as soon as the harness's generation windows became contiguous (the gap is then exactly
0 ms, always). Nothing failed: the consumer SUMS the group, so the totals and the
reconciliation invariant stayed right, and the golden snapshots had ratified the `null` on
the day they were written — a snapshot is regenerated from whatever the code currently
does, so it catches a later change and never an initial omission. The damage was not
confined to the timeline, which is why "only granularity is lost" was the wrong way to
describe it: a grouped emission is one API call to the evalboard's thinking-cost
simulator, whose prompt-cache cascade is quadratic in that count, so a single-shot
Antigravity run had every cascade coefficient pinned at zero; the `Messages` count and the
10 s slow-generation bar were per-turn too. Deriving the constructor set from each
module's own imports closes exactly the blind spot CE058's clause concedes:
`claude_code_agent.py` binds only `AssistantMessage as AssistantMessageTelemetry`, so a
name list guards that file's two construction sites purely by coincidence, and an
arbitrary `as Msg` is missed outright. Widening CE058/CE059 the same way changes two
shipped rules, needs its own mutation checks, and is recorded in
`.claude/harness-candidates.md`.

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

Pi shipped measuring its window from its own `turn_start` while four sibling reducers
tiled from a mark, so the wall clock between one turn's end and the next turn's start (the
model time that PRODUCED that turn) fell into no bucket at all. Why nothing failed:
timing.md § Why the ms-exact identity contract exists. Pi's own tests passed because they
were written against Pi's own arithmetic. The hazard is therefore not a reducer that
computes the window wrongly but one that computes it AT ALL: a new harness whose author
reimplements the arithmetic inline arrives with a green test suite by construction.
`tests/test_timing_identity_contract.py` is the two-sided check.

A separate id from CE058, CE059 and CE060: those are about the VALUES a message carries —
an unknown duration published as a literal, a window built from one clock read, a missing
identity. This one is about PROVENANCE, where the arithmetic came from, and one invariant
per id is what makes a `# noqa` mean one thing.

Proving the import is the cheap structural half that the arithmetic tests cannot reach — a
sixth harness rolling its own.

Tool subtraction is not part of a window's geometry: a call issued by an earlier emission
can still be running when the next window closes, and folding that into `close_window`
would put a mode flag on a helper whose value is having one shape. The collector already
knows every span, so `timing.subtract_tool_time` removes tool time once for every harness.
claude-code calls the same `close_window` as the other reducers, with no suppression. The
explicit-`None` exemption covers codex's rollout rebuild and claude-code's sub-agent
synthesis.

## CE063

Five reducers each subtracted tool time themselves while the head and tail were computed
centrally at the same seam, and that asymmetry is where every timing defect on this branch
lived (measured cases: agents.md § Per-harness generation marks). A sixth harness reaching
for `busy_ms` rebuilds that, and the double subtraction under-reports generation on one
harness only, which takes a corpus comparison to notice. It is a separate id from CE061
rather than a rebody: CE061 asks where a window's ARITHMETIC came from, and all five
reducers call `close_window`, so its property is live and unsuperseded; this asks whether
a reducer subtracts at all. All five reducers today import `close_window` by name, so a
ban built from `_imports_the_helper` would fire on the first working call site spelled
`timing.close_window(...)`.

In `_imports_the_helper`'s own words, a rule that missed `timing.close_window(...)` "would
tell an author to change a working call site." CE063 therefore keys on the `busy_ms` name
binding plus an `ast.Attribute` match for `timing.busy_ms`.

`tests/test_timing_identity_contract.py` drives every harness off a scripted clock and
asserts that the four buckets tile the turn to the millisecond. CE063 adds only the cheap
structural half that a static check can reach.

## CE064

The turn's OUTER bounds are something no other rule looks at: CE058-CE061 all scope to
`AssistantMessage`, and the bracket is not one. All three clocked harnesses derived their
window bounds from the `TurnClock` and let the bracket fall back to `datetime.now`,
putting a monotonic-derived stamp and a raw wall stamp inside one subtraction — the exact
split `TurnClock` exists to remove, reintroduced at the one seam the clock did not own.
Measured on a live antigravity turn: an `AgentEndEvent` stamped **17 us BEFORE its own
last message finished**, which cannot happen (the event is constructed strictly after the
final flush), and `decompose_turn` clamped that negative and published `0.0` — "measured,
and instant", the CE058 confusion arrived at from the other direction — for a harness
whose real tail is ~0.1 ms. It surfaced on one harness only because the drift is tens of
microseconds and antigravity is the only one that holds its process across turns, so
nothing happens between its last flush and its end event; every other harness books a tail
of 7-543 ms, where the drift is invisible rather than absent — which is why the fix is at
every clocked site rather than at that one. Codex and opencode take their spans from the
CLI's own epoch stamps and deliberately have no `TurnClock`, so a raw bracket is
CONSISTENT with their bounds. `tests/_bracket_clock.py` anchors its stand-in a year from
real time, so a reverted argument fails by a year rather than by the microseconds that
separate the two clocks — the division of labour CE060 states: a rule removes the SILENT
case, a default nobody chose.

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

`tests/_bracket_clock.py::AnchoredClock` advances on the REAL monotonic clock instead of
stepping by hand. That is what lets the same fixture also assert the head and tail: with
the bracket and the window bounds on one basis, `decompose_turn` returns small positive
measurements instead of the clamped `0.0` the cross-basis subtraction produced (the PROBE
above).

The two ends of the bracket fail differently, so `assert_overhead_is_measured` needs one
assertion for each. A defaulted `AgentStartEvent` lands ~365 days before the clock-derived
first window, so the head blows any sane UPPER bound by the whole offset. A defaulted
`AgentEndEvent` lands ~365 days BEFORE its own last message, so `decompose_turn` clamps the
negative to `0.0` — "measured, and instant", which passes an upper bound. Only a strict
`> 0.0` catches it. It holds on all three clocked harnesses because real work separates a
turn's last flush from its end event. The margin is smallest on antigravity, which holds
its process across turns: it measures 0.007-0.035 ms there, 7-35 ticks of the 1 us
resolution that both `datetime` and `time.monotonic()` have on Linux, macOS and Windows.
That is the magnitude the clamped defect hid, which is why `>= 0.0` is not an acceptable
relaxation.

## CE065

`evalboard/lib/pricing.ts` used to carry a hand-copied mirror of the Python rate card.
Keeping a hand-copy honest needed five layers of bookkeeping: a regex parser that re-read
`pricing.py` at test time, a meta-guard against that regex silently narrowing, a
`DELIBERATELY_UNMIRRORED` exemption set, a staleness guard for the exemption set, and a
comment begging the next reader to keep the set honest. It still shipped a real bug —
`claude-sonnet-5`, `gpt-5.6-sol`, `gpt-5.6-terra` and `gpt-5.6-luna` sat in the exemption
set under "the evalboard never runs them" while appearing tens of thousands of times in the
run corpus, so every one of those runs rendered `—` for cost with nothing failing.

If a *test* can read the table, a *generator* can emit it.

The old exemption set encoded TWO different things, and they survive differently. That
three OpenRouter models must stay unpriced, so that `runs.ts`'s apportionment of the
provider's real bill still fires, is a property of the RATE, so it is now data on the rate
itself (`ModelPricing.per_request_billing`) and nothing has to remember it. That four
heavy frontier variants are not priced on the board is a property of the FRONTEND, so
`DELIBERATELY_UNMIRRORED` keeps the stale-membership guard the deleted test carried — an
exemption nobody re-reads is what shipped the bug above.

### Keeping DELIBERATELY_UNMIRRORED honest

Membership silences the mirror for one id indefinitely, so a stale entry hides a live bug
rather than a non-issue. Before adding an id, grep the run corpus for it — absence from run
data is the ONLY justification, and it expires the moment a harness adopts the model.
`_assert_exemptions_are_live` fails the build once an id leaves `pricing.py`.

## CE066

A metric, a statistic and a serializer that core needed were pulled out of `reports*`, which
put `turn_time_buckets` and the run.json serializer in a rendering module. They now live in
`result_metrics.py`, `stats.py` and `run_record.py`.

Core legitimately writes reports: `orchestrator.py` writes the per-task HTML and
`orchestration/batch.py` drives `ReportGenerator`.

The allowlist is purely writers, and an allowlist rather than a denylist is the CE018
rationale. `eval_result_to_task_dict` is deliberately absent, because carrying a
serializer on it would be the rule documenting a wart instead of the wart being removed.

Checking the RELATIVE spelling is not a detail. The relative form is the local idiom —
both surviving edges are `from .reports import write_task_html` and `from ..reports import
ReportGenerator` — and an earlier draft matched only `node.module`, which for a relative
import holds `"reports"` with the dots in `node.level`. It fired on nothing the codebase
actually writes, and its own tests passed because they used the absolute form. An unrun
assertion is documentation, not enforcement.

## TestRuffExternalCoversEveryRule

`[tool.ruff.lint] external` is what stops ruff reporting RUF102 "Invalid rule code" for a
suppression it does not own. It was hand-maintained and had fallen ~14 ids behind —
including CE054 and CE048, whose own docstrings advertise `# noqa: CE054` / `# noqa: CE048`
as the supported escape hatch. The first person to use the documented exemption got a red
`make check` for doing exactly what the rule told them to.

The list then drifted a SECOND time, and this class is why it drifted quietly: it read
`ALL_RULES` alone, so it could not see a rule that is a `@pytest.mark.lint` class rather
than a `BaseRule`. CE044 and CE065 are both such rules, both were missing, and only CE065
was noticed — by a human reading a diff. Nothing was red for want of those two entries (no
`# noqa: CE044` or `# noqa: CE065` exists in the tree), so the fix was pre-emptive.

The reverse direction matters because a declared id for a deleted rule is the
exemption-set rot that the generated pricing table exists to remove.

## TestRunRecordFieldVocabulary

Both run-analysis surfaces shipped six wrong field names at once (`turns`, `total_tokens`,
`assistant_turn_count`, `max_turns`, `criteria_count`, `all_criteria_perfect`), and the
failure is worst exactly where the instruction applies: the >20-task path, where the agent
is told NOT to fall back to reading whole files.

Unioning the run-level and criterion-level models is a weakening that still catches every
one of the six shipped names, since none of them exists on any of those models.

## _layers

A second copy of "where does this file sit in the package" is how a package added to one
regex silently escapes the other. The two rules keep separate exemption sets because they
do not ask the same question — see [CE004](#ce004) for the cycle that inheriting one
opened.

The denylist form left holes twice over. An earlier draft named only `orchestrator.py` as
the top-level core module, which exempted `result_metrics.py` — the very module CE066's
fix message tells a violator to move their metric into — along with `run_record.py`,
`stats.py` and `timing.py`. Its successor listed ten core directories and `isolation/` was
not one of them, so `isolation/docker_runner.py`, the `driver: docker` evaluation path,
could import anything with both rules silent.

The package regex is anchored on `src/` because the unanchored form made a repo-root file
core: this project's own checkout directory is named `coder_eval`, so
`…/coder_eval/conftest.py` matched the package. A clone under a parent directory literally
named `src` still matches, and so does that clone's `tests/` tree. No path substring
separates the package from a checkout laid out like it; closing it properly means
relativising every rule's path against the repo root. It is unreachable today because
CE004 and CE066 are only ever handed paths under the runner's `SRC`, and
`_ALSO_SCAN_TESTS` is `{"CE048"}`, which uses neither predicate.

## _model_ctor

CE060 and CE061 ask the same first question: is this call building an `AssistantMessage`?
CE060 worked out the answer first. Duplicating the resolver into CE061 means a model
rename or a new import spelling needs two fixes in two rules, and the second fix is the
one that gets missed. CE064 asks the identical question about
`coder_eval.streaming.events` and `coder_eval.timing`, which is why `bindings_from` takes
the module as a parameter instead of a third copy.

Taking the class name from the model follows CE056 importing `IN_CONTAINER_ENV` and CE057
deriving its target set from `SIDECAR_MODULES`. Alias resolution removes the *local
binding* spelling, not every rename; taking the name from the model covers the rest.
