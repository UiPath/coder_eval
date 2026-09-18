# Reporting, pricing, harbor, telemetry

> Conventions and authority order: see [README.md](README.md).

## Published rates and run-time caps

- **One formula per published rate**: `pass_rate` / `error_share` are published by THREE
  models (`RunSummary`, `VariantAggregate`, `SuiteRollup`) and all three route through
  the single `models/results.py::nothing_was_measured(not_graded=, measured=)` — see
  [orchestration.md](orchestration.md) § Rates need verdict evidence, not bucket counts
  for why, and why `measured` is counted evidence rather than a bucket count. The
  evalboard mirrors the rule: `TaskTrend.passRate` is `number | null`, and an unmeasured
  task renders "—" and sorts LAST in the worst-first Trends view rather than to the very
  top as the worst offender.

- **Run-time caps (non-criterion enforcement)**: `TaskDefinition.run_limits`
  (`RunLimits` model) is the single namespace for all *task-level* run-time caps —
  `max_turns` / `task_timeout` / `turn_timeout` (structural) and `max_input_tokens` /
  `max_output_tokens` / `max_total_tokens` / `max_usd` (cumulative budget). Token/USD
  breaches abort with `FinalStatus.TOKEN_BUDGET_EXCEEDED` or `COST_BUDGET_EXCEEDED`
  (both `category == "failed"`). Structural caps are set from the CLI via `-D
  run_limits.max_turns=…` / `-D run_limits.task_timeout=…` / `-D
  run_limits.turn_timeout=…` (field-merged into `run_limits`); budget caps via `-D
  run_limits.max_usd=…` etc. or YAML. Layered config uses field-merge — a variant block
  overrides individual keys without replacing the task's block. The one *per-criterion*
  cap, `stop_early.decide_within`, deliberately lives on `LiveSuccessCriterion` instead
  (see [orchestration.md](orchestration.md) § Early stop on criterion) — the watcher
  must attribute a decision-step timeout to a specific criterion, which `RunLimits`
  (task-scoped, criterion-agnostic) cannot express.

## Plugin and GitHub Action layout

### plugins/coder-eval/

The published Claude Code plugin: `.claude-plugin/plugin.json` (its `version` is a
derived pin of pyproject's, bumped by release.yml, guarded by
tests/test_action_version_pin.py), `skills/<name>/SKILL.md` × 6 (`/coder-eval:init`,
`/coder-eval:check-skill`, `/coder-eval:task`, `/coder-eval:lint-tasks`,
`/coder-eval:analyze`, `/coder-eval:ci`), and `reference/` — everything a skill reads
must live here, since an installed plugin is copied to ~/.claude/plugins/cache/ WITHOUT
its parent dirs (address it via `${CLAUDE_PLUGIN_ROOT}`). `reference/criteria.md` is
generated (`make plugin-reference`, CE033); `reference/run-layout.md` is a verbatim
mirror of `.claude/shared/run-layout.md`; `reference/task-rubric.md` is the shared
task-quality rubric that `task` and `lint-tasks` both read (plugin-only — no repo-side
twin); `reference/repo-layout.md` is the eval-tree DISCOVERY policy every skill reads
(`SKILL_NEEDS_EVAL_ROOT_DISCOVERY`, which a new skill must declare a stance in) — glob
for `task_id:` files and `run.json`, never assume `tasks/`/`runs/latest` — as distinct
from `run-layout.md`, which describes what is inside a run directory. Every skill must
appear in all four surfaces in `SKILL_DOC_SURFACES` (derived test), and their combined
frontmatter `description` length is capped (`SKILL_LISTING_BUDGET_CHARS`) because the
skill listing's budget is shared with every skill the user has installed. **Skill naming
is verb-first imperative** — a skill is a command you issue (`/coder-eval:<name>`) and
every one of them takes an action, so name it for the action: a bare verb where that is
unambiguous (`init`, `analyze` — the object comes from the argument), otherwise
`<verb>-<object>` (`lint-tasks`, `check-skill`). Never `<object>-<verb>`: `skill-check`
was renamed to `check-skill` precisely because it read backwards next to `lint-tasks`.
`task` and `ci` predate the rule and stay — renaming a published skill breaks every
user's muscle memory for no functional gain, since activation keys on the `description`,
never the name. Distinct from `.claude/commands/`, which stays repo-local contributor
tooling.

### action.yml

Published composite GitHub Action (coder-eval as a CI gate). release.yml's `release` job
maintains its `version:` default; its `promote` job (gated on publish-pypi) moves the
`v<major>` tag + cuts the Release, so nothing consumer-visible moves before the wheel is
on PyPI. verify-published-action.yml then verifies the published composite
(tag/pin/PyPI/Marketplace parity, plus a real consumer run) after each Release and
nightly. Runbook: CONTRIBUTING.md § Releasing.

### Why the action argv tests run the shipped script

`tests/test_action_inputs.py` runs the real `run:` bodies from `action.yml` and does not reimplement them. The two bash steps build a `uv tool install` and a `coder-eval run` command line from string inputs, and every failure there is silent. If an extra drops out of the requirement string, the install gives a working CLI that has no agent. If word splitting or pathname expansion changes a value, the CLI gets a different value than the workflow wrote. In both cases the run measures something else and still exits 0. A test that keeps its own copy of the script tells you nothing about what consumers get.

`args` is one argv entry per line, appended verbatim, so a `-D` value like `key=[A,B,C]` survives. That value is a bash character class. If the input were split on whitespace, bash would silently replace the value with a single name whenever a file in the working directory matched.

The argv-recording stub is a bash script, not a Python one. On a Windows runner, `shell: bash` is Git Bash. Git Bash rewrites arguments that look like absolute POSIX paths when it passes them to a native Windows binary, so a Python-shebang stub receives `/action-checkout` as `C:/Program Files/Git/action-checkout`. Turning that conversion off does not fix it, because then the shebang launcher cannot pass Python its own script path. A bash stub never crosses into a native binary, so argv arrives byte for byte on every platform. The stub writes argv NUL-delimited rather than as JSON, so a value with a quote, a backslash or a space needs no escaping when it leaves bash. `CE_PROBE` is how the env-passthrough test sees what the child received. The passthrough is collected into an array and handed to `env -- … coder-eval`, never exported into the step's own shell, so only a process the script starts can report the value.

Collecting rather than exporting matters because the loop is line-based, so an `env` value carrying a newline splits into a second `NAME=VALUE` entry. Exported, that entry could overwrite `CE_ARGS`, `CE_RUN_DIR` or `GITHUB_OUTPUT`, which the step reads AFTER the loop — an argv rewrite. Collected, it reaches the child as data only. No hostile author is needed: any interpolated value or a rotated multi-line secret does it. `test_a_newline_in_a_value_cannot_rewrite_the_step` pins it.

### The claude-pr-review hardening invariants

`claude-pr-review.yml` runs with privileges over attacker-controlled PR content. `tests/test_pr_review_workflow.py` pins its hardening, so an edit that weakens it fails the build instead of passing silently.

`include_comments_by_actor` is a hand-maintained copy of the CODEOWNERS `*` owners. If the two drift, a maintainer's review guidance silently disappears from Claude's context. The tool allowlist bans tools that give secret, re-ingest or network reach. For example, a shell `cat` can read a token persisted in `.git/config`, and `gh pr view` reads every comment verbatim, which bypasses the actor allowlist. `persist-credentials: false` removes the on-disk token that the action's own `git fetch` needs, so an env-only credential helper restores auth. The test checks both halves. If the helper is dropped, fetch fails with "could not read Username". If a literal `secrets.*` goes into the helper instead of an env reference, the token is written to disk again.

### Couplings of verify-published-action.yml

You cannot run `verify-published-action.yml` before merge, so every link it has to another file is a place where a rename passes `make verify` and the gate silently stops working in production. `tests/test_verify_published_workflow.py` gives each link a test that runs before merge.

GitHub does not report an error when `workflow_run: workflows: ["Release"]` names a workflow that does not exist. The trigger never fires, and the gate falls back to the nightly schedule with no signal. The workflow derives the Marketplace slug with a shell pipeline. That is a second slugger next to `marketplace_slug`, the one CE026 uses for doc links. The two agree only because `action.yml`'s `name:` is `coder_eval`, the one input that both leave unchanged. The `# <-- kept in sync` pin anchor has three readers with different whitespace tolerances, so after a reformat one reader can report "parity OK" for a pin that another reader did not bump. The inline consumer task YAML is a full `TaskDefinition` document. CE029 checks that shape in Markdown, but nothing checks it in the workflow, so a field rename or an `extra="forbid"` violation would show up only as an unclear failure in the paid nightly run.

### Why the derived version pins are tested on every commit

`pyproject.toml` is the only source of the version. Two files hold a pin derived from it, and one `release.yml` step bumps both inside the release commit. The first is the `version:` default in `action.yml`. The composite action installs `coder-eval==<that default>`, so a consumer who pins `UiPath/coder_eval@vX.Y.Z` (or the moving `@v0`) must get X.Y.Z. The second is `version` in `plugin.json`. `claude plugin validate --strict` rejects a manifest with no version, and Claude Code keys plugin updates off this value, so a stale pin leaves users on a cached copy.

The seds run only on the release path. A hand edit, or a release that skipped the amend step, drifts with no signal. That is how `action.yml` once shipped pinned to 0.8.6 while main was at 0.8.9. `tests/test_action_version_pin.py` makes the check run on every commit. It also checks the line shape each sed matches: the `# <-- kept in sync` trailing comment in `action.yml`, and a `"version"` line with a trailing comma in `plugin.json`. If a reformat moves `version` to the last key or puts the JSON on one line, the bump becomes a no-op. A `grep -q` guard in `release.yml` catches this, but only after the tag exists.

## The Agent ABC contract

`agent.py` is the plugin SPI: everything a third-party agent author must satisfy. The
authoring walkthrough is [docs/EXTENDING.md](../../docs/EXTENDING.md) and the
numbered lifecycle requirements are in CLAUDE.md § Adding a New Agent; what follows is
why the seams are shaped the way they are.

The turn-lifecycle bookkeeping lives on the BASE class as class-level defaults, so a
subclass gets the behaviour without re-declaring it. `_iteration_was_incremented` is set
right after the counter bump at the top of `communicate()` and consumed by
`discard_pending_turn()`, which rolls the counter back exactly once per failed turn — even
when partial-record assembly leaves `pending_turn` at None. That is why rollback is the
caller's move, not the agent's: only the caller knows a turn failed.

Capability flags are declared rather than probed. `supports_cooperative_stop` gates
arming early-stop, so arming it on an agent that ignores `should_stop` is rejected at
resolution rather than silently never firing. `supports_cost_log_tags` and
`system_prompt_semantics` are declared for reasons of their own — see
[agents.md](agents.md) § Why the constructors declare every kwarg and § The
system_prompt_semantics marker.

The shared mid-turn failure kernels are byte-identical fragments that recur across and
within the agent turn-loops. Each agent keeps its OWN outer try/except/finally bracket,
because the brackets genuinely differ (flat versus nested, `finally` or not), and calls
these from inside its existing branches. They take the agent's own per-turn `finalize`
callable, so the helper never needs to know how each agent assembles its end event.

## Telemetry emission

Telemetry is a self-contained, opt-out usage side-channel and is **never** part of the eval
data path.

The Azure Monitor exporter routes an OpenTelemetry log record to the `customEvents` table
instead of the default `traces` table if and only if the record carries a particular
attribute; the event name is that attribute's value and every other attribute becomes a
custom dimension. That attribute is reachable through plain stdlib logging — an OTel
handler attached to a dedicated logger, and `track_event` calling `logger.info(name,
extra={...})`. So every OTel and Azure import lives inside `init_telemetry`, and
`track_event` is pure stdlib and a cheap no-op when telemetry is off. The attribute name is
hard-coded to match the exporter's internal constant for the same reason.

### On by default, and what that obliges

An ingestion-only connection string is baked into the app, so a fresh install reports usage
to the shared resource; an explicitly-set one takes precedence, and `TELEMETRY_ENABLED` is
the single canonical disable gate. It is INGESTION-ONLY — it can write telemetry to the
resource, never read, query or manage it — which is the same class of value embedded in
every distributed telemetry client, and it was approved for embedding. It is base64-wrapped
only to avoid tripping naive secret scanners and to mark it as an intentional, reviewed
default; base64 is trivially reversible and this is not secrecy. The residual risk is
telemetry spoofing and ingestion-cost abuse, bounded by the resource being dedicated to
coder-eval usage telemetry.

No prompts, file contents or repo paths are ever captured — only enums, counts, durations,
an anonymous per-install id (a random UUID in the user config file: it identifies an
install, not a person) and non-PII platform identity. Because it is default-on, the first
run that initializes it prints a one-time stderr notice disclosing what is collected and
how to disable it.

### Non-fatal, and what that costs

Every public function wraps its body in `try/except Exception` and logs a warning rather
than raising: telemetry must never break a run. CE019 enforces it. Persisting the install
id is best-effort too — a missing HOME or an unwritable directory degrades to no
`InstallId`, never to disabled telemetry.

Two guards are narrower than they look. The exporter is constructed in its own `try`,
because it parses the connection string — a credential — and a parse error can echo it
back, so the failure is logged WITHOUT interpolating the exception. And the command
decorator catches `BaseException` as well as `Exception`, because `KeyboardInterrupt` and
`SystemExit` derive from the former: without that branch a Ctrl-C would skip both handlers
and the `finally` would record the aborted command as "Succeeded".

The events logger is a process-wide singleton that outlives a shutdown, so the handler is
tracked and detached on shutdown and any stale one is dropped before attaching — otherwise
a re-init double-emits. `SchemaVersion` is stamped on every event so the dashboard's
queries, a cross-system contract, can detect a schema change instead of silently breaking.

## Cost joining

For the open-weight backend the Claude binary's transport drops the provider's real cost
and per-call cache before Python can see it, so a proxy-side callback writes one JSONL
record per call and this module joins them back on at the TURN level: the turn's total cost
is overridden with the SUM of its calls' real cost, and the per-call breakdown is attached
as a deterministic audit record.

Token buckets are LEFT UNTOUCHED, so the `EventCollector` remains the single writer of the
token-bucket invariant ([agents.md](agents.md) § Token accounting and the reconciliation
message). There is deliberately NO per-generation distribution:
matching a proxy call to a transcript generation has no deterministic key — only positional
or output-token heuristics — so that view lives in the per-call table rather than being
guessed onto the message stream.

### Coverage, and why a gap keeps the estimate

A turn's cost is overridden only when every call that reported usage is priced. A
degenerate call that reports NO usage at all — no cost and no tokens, seen occasionally on
some providers — is ignored, so one of them cannot revert a whole turn to the static
estimate. A call that reports usage but no cost is a genuine gap: the turn keeps its static
estimate, because overriding would bill it at $0, no breakdown is attached, and a warning
names the unpriced ids. A turn with no matching record keeps its estimate too.

### Retry safety and the two phases

Several turns can share an `iteration` — a crashed attempt and its retry — and both
attempts' proxy calls carry that tag. An iteration's calls are credited to a single
survivor (the last turn with that iteration THAT HAS GENERATIONS) and earlier siblings are
zeroed. The credit and the zero are decided TOGETHER: a sibling is zeroed only when the
survivor is actually credited, so if the survivor falls back to static the sibling keeps its
estimate and the iteration's spend is never dropped.

The join is transactional. The whole plan is computed before any turn is mutated, so a
malformed record — which raises while building the per-call breakdown — aborts the join
with the run untouched, matching the caller's "keeping static pricing" contract. Spend
tagged with an iteration no turn has is surfaced rather than silently dropped.

## The reports package

`reports/` is a **leaf**: it may import from anywhere in `coder_eval`, and the core layers
may import only its public *writer* entry points. That asymmetry is the whole point, and
**CE066** enforces it. The invariant is not "core must not import reports" — core
legitimately *writes* reports (`orchestrator.py` writes the per-task HTML,
`orchestration/batch.py` drives `ReportGenerator`). It is that a **metric, a statistic, a
serializer or a formatter** must never be reached out of the rendering layer.

That was the actual shape of the code before the split. `reports_stats.py` was three
unrelated modules sharing a file, and the orchestrator imported `turn_time_buckets` and
`visible_turn_count` from it *during a run* — a number the evaluation loop needs, living in
a reporting module. The three pieces now sit where their consumers are:

- **`stats.py`** — distribution-free statistics, **dependency-free by contract**: stdlib
  only, no `coder_eval` import, direct or relative. A unit test parses its AST and asserts
  that, rather than leaving it to convention, because being reasonable-about-in-isolation is
  the only reason it is a separate module. Display formatters (`fmt_mean_sd`, `fmt_p`) stay
  in `reports/helpers.py`: they return `"N/A"`, `"—"` and `"<0.001"`, which is presentation.
- **`result_metrics.py`** — metrics derived from a finished `EvaluationResult`, consumed by
  the orchestrator mid-run as well as by the reporters. Deliberately **not** folded into
  `timing.py`, which has no `EvaluationResult` dependency and is imported by every agent
  adapter; adding one would widen that surface for everyone.
- **`run_record.py`** — the `run.json` task-row serializer. It is a run-record serializer,
  not a report, and its old home inside the experiment reporter was the *only* reason
  `orchestration/batch.py` reached into the reports layer at all. Moving it is what lets
  CE066's allowlist be purely writers; carrying a serializer on that list would be the rule
  documenting a wart instead of the wart being removed.

**CE066 checks both the absolute and the relative import spelling.** Its first draft matched
only `node.module`, which for `from ..reports import X` holds `"reports"` with the dots in
`node.level` — so it fired on neither of the two real edges in the tree, and its own tests
passed because they used the absolute form. The layer predicates live in
`tests/lint/rules/_layers.py` so CE004 and CE066 cannot drift about where the package or its
`cli/` boundary is. Each rule's scope is an allowlist of what is *exempt*, so a new subpackage
is in scope by default: CE066's core is *everything under `src/coder_eval/` except `cli/` and
`reports/`*, and CE004's scope is *everything except `cli/`*. The two sets differ on purpose —
the reports package runs without the CLI, so it must not import `cli`, but it may reach into
itself. CE004 first borrowed CE066's predicate whole and so inherited the `reports/`
exemption. Both denylist forms before that also leaked: naming only `orchestrator.py` left
`result_metrics.py` exempt (the module CE066's own fix message points at), and its
ten-directory successor never named `isolation/`, leaving the `driver: docker` evaluation path
invisible to both rules.

**`format_ms` lives in `durations.py`, not `formatting.py`.** `formatting.py` imports
`claude_agent_sdk` for the payload formatters, and the reports package should not reach
through an SDK-shaped module for a 14-line duration formatter. This does *not* make the
package SDK-free — `models/agent_config.py` imports `ClaudeAgentOptions` and every report
module needs `models` — so the tests assert what is true: `durations.py` is SDK-free, and
`reports` no longer imports `coder_eval.formatting`.

### Rejected: a shared section-data layer

The markdown and HTML reporters render four "duplicated" sections. All four pairs were read
in full before deciding, and **only one shares an input shape** (command statistics, both
taking `CommandStatistics`); the others take a `list[dict]` row, a `TokenUsage`, an
`EvaluationResult` and a `list[EvaluationResult]` across three different scopes. The
remaining differences are legitimate per-surface presentation, not drift: `:.1f%` vs `:.0f%`,
and an unmeasured average **hidden** in markdown versus **dashed** in HTML — two valid
renderings of the same `None`. Building the adapter would mean normalizing dict-row and
live-model inputs across three scopes, touching the `run.json` contract, to remove about
twenty lines. Rejected on KISS/YAGNI. The two things in those pairs that *were* real — a
literal `50` beside its own `SLOW_PARAMS_PREVIEW_CHARS`, and a hand-rolled
`TokenUsage.total_tokens` — were simply fixed.

`analysis.py` and `formatting.py` stay top-level on purpose: they are not report modules,
and moving them in would give the package an SDK dependency and force CE066 to exempt the
orchestrator's `analysis` import.

## Ungraded rows in a rollup

Only the SCORE is dropped from an ungraded row, never the row itself. Duration, tokens and
assistant turns are facts about the run that grading has nothing to do with, and `execute`'s
stated contract is that only the verdict is withheld. Skipping the row whole made an
all-ungraded experiment render `Avg Duration | N/A | N/A` with the Tokens and Assistant
Turns rows absent entirely.

An earlier note in `reports/helpers.py` claimed an experiment is either entirely graded or
entirely ungraded, because `grade` is run-level. It is not: `run --resume` grades rows
independently and folds a failed one back ungraded, so mixed experiments are real. That is
why the series are consumed independently rather than index-aligned.

## Report rollups and the HTML twin

`reports/html.py` is the evalboard's STATIC TWIN: the two render the same run and must
agree, so a rule implemented on one side belongs on the other. The arithmetic itself lives
in `result_metrics.py` and `stats.py`; the renderers only format it.

### An unmeasured value is never zero

This is the rule the reporting surfaces exist to hold, and every violation of it has been
the same bug: a value that was never measured rendered as a confident zero.

`analysis.py` returns `None` when nothing was timed, so a `0` there published "measured,
and instant" for a turn whose only tool call was force-closed and never timed. The renderer
must therefore test `is not None` rather than truthiness — a genuine measured `0.0` average
is a real measurement and must survive to the surface. The same reasoning governs the four
wall-clock buckets (an unmeasured one renders as an em dash, never `0ms` — CE058), the
ungraded score placeholder (deliberately not `0.000`), and a suite that graded nothing.

`duration_seconds` is the awkward case: it is a non-optional float defaulting to `0.0`, so
there is no `None` arm to write — but a `0.0` duration is a run that was never timed, and
subtracting real buckets from it renders a fabricated negative residual. The evalboard
keeps that null; so does the report.

### Read the stored value, do not re-derive it

The four wall-clock buckets are computed ONCE by `turn_time_buckets` and carried on the row.
The `iterations` projection is deliberately 6-key, with no messages, no commands and no
harness timings, so a renderer CANNOT re-derive them from it — a second implementation of
that summation is exactly what the shared function exists to prevent.

`_turn_tool_union_ms` shows the shape. It PREFERS the stored value, because the collector
writes it from the single span set it measures all four buckets against, so the two surfaces
are guaranteed to agree rather than merely observed to. The derivation is the LEGACY path
for a record written before the field existed, which must stay renderable. The two paths
cannot be told apart by value — both return `None` for a turn with no bounded span — which
is why the stored one is checked with `is not None`: a stored `0.0` is a measurement and
must not fall through to a re-derivation. The span SELECTION is the shared rule, not a copy
of it, for the same reason.

### The ungraded row in every surface

An ungraded run gets the explanatory line; an ordinary EMPTY run keeps its "n/a (0/0)"
rendering, because the two are different facts. The test is `pass_rate is None`, not
`not tasks_graded` — an execute night with a crashed row has `tasks_graded > 0` while still
having measured nothing, and that rendered `0.0%` beside `Error Share: 100.0%`, exactly the
total-failure reading the guard exists to prevent.

Only the SCORE is dropped when there is none, never the row: duration, tokens and assistant
turns are facts about the run that grading has nothing to do with, and `execute`'s contract
withholds only the verdict. Skipping the row whole made an all-ungraded experiment render
every statistic as N/A. An experiment can be MIXED — `run --resume` grades rows
independently and folds a failed one back ungraded — so the series are consumed
independently and need not be index-aligned; pairing across variants is by task id.

A fourth `Not Graded` row is rendered conditionally in every table that breaks down the
count, because without it Tasks Run / Succeeded / Failed / Errors stop summing to
`tasks_run` with nothing on the page to say where the rest went. The same reasoning drives
the JUnit `<skipped>` element: it is JUnit's only "no verdict" shape, and reporting an
ungraded row as a failure would turn a healthy run red in CI while reporting it as a pass
would invent a verdict. An ungraded row is also excluded from the failure-reason list,
which is documented as failed and errored rows.

### Per-instance aggregation, and what it buys

Per-row results are sliced per criterion INSTANCE by position, because the checker appends
one result per criterion in declared order. Aggregating per-instance rather than pooling by
type is what lets a task stack many criteria of the SAME type — an activation suite's
per-skill `skill_triggered` criteria — and get a distinct aggregate for each, instead of
one type-pooled number repeated once per instance. The aggregate carries the criterion's
description so the stacked instances stay distinguishable downstream.

The agent's own spend is broken out from the total only when there is overhead to
distinguish it from: judge spend is a property of the suite's criteria and identical across
harnesses, so comparing harnesses means comparing the agent line. **Total Cost** always
means the whole bill.

### The claims the reports do NOT make

An early-stopped row does not advertise "N turns avoided". That derived from
`max_turns - sdk_turn_index`, and on harnesses where one `communicate()` is a single SDK
turn it advertised dozens of avoided turns when all that was cut was a tool-call tail. The
upper bound is still persisted, labelled as the bound it is.

Missing spend is worded cause-agnostically, because an unpriced turn and a hard kill reach
the same conclusion and the report cannot always tell which applied.

## Harbor export

Harbor is an outer harness with its own runtime contract: a fixed reward-file convention,
a fixed log layout, a trajectory format. `coder_eval.harbor` is deliberately narrow — it
translates coder-eval's artifacts into that contract and does not know how a task is
DEFINED. It is a core layer like `orchestration/`, so it must not import the CLI (CE004);
it raises plain exceptions and lets the CLI wrap them.

Both directions exist. The packager exports a coder-eval task to run under Harbor with
coder-eval as the grader; `CoderEvalAgent` is the mirror image, making coder-eval Harbor's
AGENT against a fixed-path agent-phase task.yaml the packager bakes in.

### Write the reward file, or do not

Harbor's verifier reads a reward file and does NOT inspect the verifier script's exit code
— only whether the file exists, is non-empty and parses. A missing or malformed one raises
inside Harbor's own verify step, which its trial runner catches, records, and leaves at its
default rather than coalescing to a zero-reward object. That is Harbor's own
infra-versus-policy split, already built.

So the whole job is: write the file, or do not. The "do not" case is the load-bearing one.
An unmeasured row must not become `reward=0.0` — that would train "the agent's behaviour
was bad" from a measurement that never happened. Not writing lets Harbor's own
missing-reward path mask the trial instead. It is CE049's principle (never coalesce a
possibly-unmeasured score to a numeric literal) one level up, at the artifact-writing
boundary rather than the in-process one.

A grading-time INFRASTRUCTURE failure is the same case in disguise. The weighted-score
calculation short-circuits an empty criteria list to a hard `0.0` rather than `None`, so a
checker raising an escalating exception finalizes the row as ERROR with a score of `0.0`,
not `None`. That is not a measurement either, so it gets the same treatment via the row's
category.

### Not every criterion can grade inside someone else's container

A portability audit classifies each criterion type so the packager can refuse an
unsupported task AT EXPORT TIME, where the operator sees why, rather than at verify time,
where it is an unexplained low reward with no obvious cause.

Filesystem and exit-code checks are portable — nothing about the verifier container changes
what they need. `reference_comparison` needs the reference tree, which the export always
places verifier-side when the task declares one, so that class never actually blocks.
Trajectory-reading criteria once could not work in the export direction at all, but no
longer block: the packager's `CoderEvalAgent` always runs `coder-eval execute --format
harbor`, which writes `/logs/agent/trajectory.json` (ATIF) alongside `task.json`, and the
generated `tests/test.sh` grades `/logs/agent` as a run directory — so the trajectory is
always there by the time the verifier runs. `cli_called` reads a log written by a recorder
shim coder-eval's own sandbox installs, which the exported Dockerfile does not provision —
this is the one class that still hard-blocks. Credential-needing criteria need a model
reachable from inside the verifier container and a judge that does not follow the agent's
route; the export no longer gates them behind a flag — it exports them unconditionally on
the assumption that an operator exporting one has already provisioned that access
themselves (C2 does not do it for them).

Coverage is registry-derived, so a new criterion type added to the union without a
classification fails CLOSED rather than silently exporting as if it were portable.

### The non-obvious constraint in the emitted task.yaml

The verifier-side `tests/task.yaml` must NOT set a `none` agent type, even though the
verifier phase is conceptually exactly that. The `check_none_agent` validator rejects any
criterion with `requires_agent=True` — which includes `reference_comparison`, a criterion
the export treats as portable — the moment the type is literally `none`, regardless of
whether an agent actually runs. The bare-task path never instantiates an agent whatever the
type says, so a placeholder real type plus a placeholder prompt satisfies the validators
without changing behaviour. Verified directly rather than inferred.

`--workspace-dir "$(pwd)"` on the agent side is the real fix, not a workaround: without it
the tempdir sandbox writes the agent's workspace to a throwaway directory elsewhere in the
container, never where the verifier looks. Confirmed live — the agent's output was real and
every criterion scored 0 as "file does not exist". `$(pwd)` is resolved by the container's
shell at exec time and equals the WORKDIR because the exec is given no explicit cwd.

### The artifacts default is CONTAINER_WORK_DIR, not a required field

Harbor's own artifact collection runs after the agent phase but before the verifier and
container teardown, snapshotting `task.toml`'s `artifacts` source to
`<trial>/artifacts/<source stripped of its leading slash>/` on the host.
`CoderEvalAgent`'s `--workspace-dir "$(pwd)"` runs the agent in-place at the container's
WORKDIR and skips coder-eval's own copy-out, so without an `artifacts` entry nothing the
agent produced would ever be visible on the host.

Defaults to `CONTAINER_WORK_DIR` (`/work`, coder-eval's own image WORKDIR) rather than
requiring every task/experiment to restate it — a package exported by coder-eval needs
coder-eval installed in the image, which in practice means derived from
`coder-eval-agent` (a mismatch already warns; see `_MISSING_CODER_EVAL_WARNING`). Unlike
`[environment].workdir` above — deliberately left unset so the container's own WORKDIR
decides `docker exec -w` — guessing wrong here is not fatal: a nonexistent source is a
best-effort collection miss recorded in the artifact manifest, not an exit 127. Declared
as a plain string, not an `ArtifactConfig` table, because Harbor normalizes
`artifacts = ["/x"]` to `ArtifactConfig(source="/x")` itself.

### What the export carries, and what it refuses to carry

No Dockerfile is written unless the task sets `sandbox.docker.dockerfile_path` — only
real build steps (`RUN`) need one. Harbor's own `should_use_prebuilt_docker_image`
(`harbor/environments/definition.py`) pulls `task.toml`'s `[environment].docker_image`
and skips the build, confirmed against a real `harbor` install. So the Dockerfile-less
shape is a real choice rather than a null-vs-set distinction: `docker_cfg.image` always
has a value (`default_factory=get_default_docker_image_tag`).

`[environment].workdir` is what Harbor passes as `-w` at `docker exec` time, but the
packager only sets it for an EXPLICIT override (`sandbox.docker.working_dir`, or a
Dockerfile's own `WORKDIR` line) — never a guess. `docker exec` (unlike `docker run`)
hard-fails with exit 127 if `-w`'s path doesn't already exist in the image, so guessing
one at export time (originally via `docker image inspect`, falling back to a hardcoded
`/app` when that failed) meant an export-time snapshot could go stale against whatever
image the trial actually ran under, on a different machine or after the image changed —
confirmed live: a wrong guess baked into `task.toml` broke every trial with the same exit
127 well before the agent ever ran. Leaving `workdir` unset when the task names no
override means `docker exec` runs with no `-w` at all, so the container's OWN current
`WORKDIR` decides — always correct, nothing to go stale. `tests/test.sh` never needs the
value in advance either; see below.

A bind mount also removes a hazard the `COPY` approach had: it never walks the export's
own `-o` directory, so a plugin source that contains it cannot self-nest.

Nothing is `COPY`'d into the image any more. `environment/task.yaml`, each `type: local`
plugin, each `TemplateDirSource` and each `extra_mounts` entry are bind-mounted at their
own host path, mirroring `docker_runner.py`'s auto-mount — which is why the export warns
that it is NOT portable to a machine without those paths.

The agentless case was found live: `coder-eval`'s schema forbids a `type: none` agent
from setting `initial_prompt`, and a `harbor run` against a real install surfaced that
`TaskDefinition` validation error before the guard existed.

Run limits and the judge-route override are carried through, because grading has no agent
loop to cap but the task timeout still bounds the verifier invocation, and dropping the
route silently replaces a pinned backend with the verifier environment's default.

`HOME` is in the default passthrough ONLY because the docker driver also bind-mounts the
host's `~/.claude` into the container at that path, so the value still resolves to a real
directory there. Harbor builds its own container with no such mount, so forwarding the
host's literal `HOME` would point the container at a directory that does not exist in it — a
real regression, not a no-op.

A missing template is a hard failure, not a warning: the agent-phase task.yaml still
references it, so a silently-skipped copy ships an export whose agent has no starter code,
and every criterion then reads "file does not exist" indistinguishably from a real agent
failure — the CE039 anti-pattern one layer up, at the export boundary. A raw dataset-backed
task is refused for a similar reason: fan-out happens later in the pipeline, so exporting
one would emit a single Harbor task whose prompt and criteria still contain literal
placeholders — never expressible, but scored anyway.

Symlinks are DROPPED rather than dereferenced in every task-authored tree the export
copies, because dereferencing writes a symlink target's content into a distributable
artifact. The reference copy wraps its failure in the export's own error type rather than
letting a bare `OSError` escape, because the CLI catches only the export errors and an
unreadable tree would otherwise abort a whole experiment export the docstring promises it
will not abort.

The generated shell script no longer interpolates a workdir value, or guesses a cwd, at
all — it is a fixed literal in `_TEST_SH_TEMPLATE` that calls `coder-eval evaluate
/tests/task.yaml /logs/agent --in-place --run-dir /logs/verifier`, passing `/logs/agent`
as an explicit RUN DIRECTORY rather than a workdir guess. `coder-eval execute --run-dir
/logs/agent ...` (the agent phase) always finishes with `/logs/agent/task.json` and
`/logs/agent/artifacts/<task_id>/`, so `coder-eval evaluate` locates the workspace from
that `task.json`'s own recorded `sandbox_path` instead of a live `$(pwd)`. Passing the
task file explicitly also keeps this off the untrusted-recorded-config path (which exists
for a shared run directory whose config is not to be trusted without
`--allow-recorded-commands`) — an explicit, operator-supplied task file always overrides
the run's recorded config. Nothing task-controlled is interpolated into the template, so
there is no injection surface to `shlex.quote` against.

## The ATIF trajectory bridge

ATIF models are VENDORED so coder-eval can emit and parse trajectories with zero runtime
dependency on the harbor package; fidelity is guarded by a frozen fixture validated once
against the real models. Three deviations are deliberate: the schema version is
pattern-validated rather than a closed literal, so a trajectory written by a FUTURE minor
version still parses (major bumps are still rejected) where harbor itself would refuse it;
harbor's `Agent` is renamed to avoid clashing with coder-eval's own; and an image payload is
an untyped dict, because coder-eval emits text only and merely needs to TOLERATE one on
read. These live outside `coder_eval.models` on purpose — they are interchange models for
Harbor interop, not evaluation models.

### Emitting

The converter is a PURE function of the models: no I/O, no agent-type branching, no mutation
of its input. Sub-agent generations are NESTED into embedded sub-trajectories rather than
flattened into the main thread, because flattening corrupts SFT data derived from the
trajectory. Reconciliation entries never become steps — their residuals are recorded in an
extra field and are already inside the authoritative totals. A turn with no message stream
degrades to one synthetic user step plus one agent step carrying all the turn's commands;
note that a stream with user or reconciliation entries but no generations takes the NORMAL
path, so those entries survive.

Every user message is a genuine user utterance today. If a tool-result variant ever gains a
producing code path, the converter must learn to SKIP those — tool results already live in
step observations.

### Hydrating

The reverse direction reconstructs only what the trajectory-shaped criteria actually read:
the commands and the message stream. It is deliberately NOT a lossless round trip.
Per-generation token buckets are not recovered, so cost and token reporting for a hydrated
result is incomplete. Sub-agent nesting is flattened. Turn boundaries are recovered by
splitting on user steps, mirroring the emit side's convention, because ATIF carries no
explicit iteration marker.

The one asymmetry that bites: a tool call's status rides on the call's own extra field and
its duration on the matching observation's, so both must be read back — otherwise
`command_executed` with `require_success` silently scores a successful command 0.0 on every
hydrated trajectory, because the status defaults to None rather than "success".
