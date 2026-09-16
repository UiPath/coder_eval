# Contracts: criteria, datasets, judging

> Conventions and authority order: see [README.md](README.md).

## Datasets and aggregation

- **Dataset fan-out**: `TaskDefinition.dataset` (inline rows or JSONL path) expands a
  single task into N row-tasks with `${row.<field>}` substitution in `initial_prompt`
  and `success_criteria` string fields. Expansion runs in `task_loader.expand_dataset`
  **before** variant resolution, so variants cannot override the dataset. Row sampling:
  CLI `--sample N` (fixed-seed uniform-random N over the whole dataset) overrides
  `--sample-per-stratum N` / `dataset.sample_per_stratum` (stratified random
  N-per-stratum, keyed on `stratify_field`, default `expected_skill` — for
  classification suites like activation). Stratified sampling (whether the N-per-stratum
  count comes from the **CLI** `--sample-per-stratum` flag or **YAML**
  `dataset.sample_per_stratum`) is **nondeterministic** by default — it re-draws each
  run (so the nightly activation suite broadens coverage over time). Set
  `dataset.sample_seed` to pin a reproducible sample; an explicit seed always wins.
  (Only `--sample N` uses a fixed seed, since a smoke test wants the same N rows each
  run.)

- **Per-criterion aggregation**: Each `BaseCriterion` subclass exposes
  `aggregate(criterion, per_row_results) -> CriterionAggregate | None`. Default emits
  `count / mean / median / std / min / max` so every criterion is suite-thresholdable
  for free. Classification-style criteria return `ClassificationCriterionResult`
  (subclass of `CriterionResult`) and layer accuracy / P/R/F1 / confusion via the shared
  `overlay_classification_metrics` utility. `BaseSuccessCriterion.suite_thresholds`
  gates the suite on those metrics; CLI exits non-zero on any gate failure.

### Criterion aggregation

The observed-label sentinels are their own classes in the confusion matrix, so "wrote
nothing" and "wrote something unrecognisable" are visible failure modes rather than rows
that vanish from the rollup.

## The live_verdict contract

`LiveVerdict` is a criterion's verdict from a PARTIAL, mid-run trajectory. Every override
must be deterministic (a pure function of the prefix it is handed) and monotonic (once it
answers pass or fail, it answers the same for every longer prefix); `undecided` is the only
verdict allowed to change on a later call. Both properties are stated at the definition
site in `criteria/base.py`, because they are the contract an author has to satisfy.

`TurnMonitor`'s deferred fail-stop and its pass/fail flip-attribution are correct ONLY
because the two shipped implementations honor them. A non-monotonic or non-deterministic
override compiles, passes CE025, and silently corrupts the stop logic.

### Why replay is the only sound check

CE025 checks the SHAPE — that a `LiveSuccessCriterion` subclass pairs with a `live_verdict`
override — and can check nothing else. Monotonicity over arbitrary Python is undecidable,
so CE036 replays every live criterion against every prefix of recorded trajectories and
asserts both properties directly. That is why adding a live criterion REQUIRES adding
`ContractCase` fixtures in the same change: CE036 fails on a live type with no cases, and
on a polarity an instance claims decidable that no fixture reaches.

The limit is worth naming: replay proves the contract on the trajectories an author
supplied, not in general. Honoring it is still on the author.

`live_decidable_polarities` is typed as the narrower CAPABILITY type rather than a bare
`frozenset[str]`, so an override returning a typo — or `undecided`, which is never a
decidable polarity — is a pyright error rather than a runtime-only lint gap.

### Any-engagement, and why order does not matter

`skill_triggered` scores a row on whether the skill was engaged AT ALL. A positive
criterion passes if its skill was engaged anywhere in the run, so a wrong skill engaged
first does not fail it — that is recall. A distractor criterion fails on ANY engagement of
its skill — that is precision. `live_verdict` latches the instant its own skill is engaged,
which is the same policy read forward.

Engagement is detected agent-agnostically so the score does not depend on the harness.
Claude emits an explicit `Skill` tool call; a harness that names that argument something
else renames it at the AGENT boundary rather than growing an alternative here. Every other
agent engages a skill by reading its files, so both the repo layout and the sandbox symlink
contain `skills/<name>/`, matched in any string parameter. The trailing separator is
required, so `uipath-agents` does not collide with `uipath-agents-foo`. The function returns
the full SET rather than a yes/no, which is what lets a caller detect a competing
engagement.

### Normalizing a shell command before matching it

`command_pattern` regexes are written against the logical command, but telemetry records
the raw `bash -lc "..."` wrapper — so whichever way the agent happened to quote an argument
leaks into the pattern. Authors then hand-model that escaping and get it subtly wrong,
silently under-counting correct calls. Unwrapping the wrapper and resolving quoting with
`shlex` lets a pattern match argv semantics instead. Shell operators survive as their own
tokens, so patterns that reference them keep working.

Adding the normalized haystack is NOT purely additive. A `command_pattern` can only gain
matches, but the same haystacks feed `exclude_pattern` and the `max_count` gate, so a
normalized form can newly satisfy an exclusion or trip a cap — a command that counted on
the raw text alone can stop counting.

It is memoized because the `TurnMonitor` re-scans the whole accumulated trajectory on
every tool-call event, normalizing the same command many times per run.

The regex search window is capped to bound ReDoS on a large command string, and
normalization runs over that same truncated window, so `shlex` never sees more than the cap
and needs no separate guard.

## Recording a CLI invocation

### Evidence, not attestation

The invocation log is an ordinary file in the sandbox the agent writes to, so an agent that
wants to can append a record for a call it never made, or delete one it did. `cli_called` is
built to keep an HONEST run honest — a missing or unreadable log FAILS rather than passing a
`max_count: 0` guard vacuously — not to withstand an adversary. An anti-cheat control needs
what `docs/DOCKER_ISOLATION.md` describes, not this.

A flat log line cannot express "verb X was called AND flag Y had value Z" without stacked
lookaheads, cannot tell a quoted argument containing spaces from two arguments, and cannot
stop a match running across a shell operator. That is why the criterion matches
field-by-field over a structured record rather than regexing a flattened string.

### The five refuse-to-score paths are uniform at a gating 0.0

A missing log, a write sentinel, a sidecar import error, unusable records and a rule error
all score 0.0 and none of them raises. Every one is agent-REACHABLE, because the whole
recorder directory lives inside the sandbox the agent writes to: an escalation would be a
`FinalStatus.ERROR`, normally read as "harness broken, discard this data point", which is a
strictly better outcome for a failing agent than FAILED. An earlier revision raised
`CheckerMisuseError` on the rule error believing only a task author could cause it;
appending one line to the log disproved that.

The legitimate concern behind that escalation — a task author's unevaluable response spec
must not be booked as an agent failure — is handled where the agent cannot reach it instead.
`RecordedCli` proves every response rule is evaluable at LOAD time, so an authoring mistake
is a validation error before the sandbox exists. For the same reason the pattern is compiled
at validation rather than in a checker: a response rule evaluates its pattern inside the
sandbox, where a `PatternError` is swallowed and the tool serves its fallback — a log line
indistinguishable from a legitimate no-match.

Both fault checks are scoped to the records the criterion is about. One log serves every
shadowed tool, so a `uip` shim that could not import its matcher must not fail a
`tool: curl` guard that has nothing to do with response dispatch.

A verb is compared against the NON-FLAG arguments, so a flag written into one can never
match — silently: the criterion scores 0 against a log holding the very call it describes.
The check mirrors the splitter's own number rule, so it cannot forbid a token the matcher
would in fact have seen.

### What the shim is, and is not

The shim records the invocation, writes the configured output, and exits. Nothing is
executed, so there is no network, no auth and no side effect. It stubs a tool; it does not
proxy one — a test that needs a REAL executable's behaviour recorded on the way through
supplies its own wrapper under `mock_path_dirs`, which depends on the tool being installed,
on PATH order, and usually on live credentials.

The recorder directory is deliberately NOT dot-prefixed: CI artifact upload skips hidden
files, and the log is primary evidence for every `cli_called` criterion. Names are
case-folded when checked against the reserved set, because on a case-insensitive filesystem
`CALLS.JSONL` is the seeded log and `ARGV_MATCH.PY` is the sidecar. Shadowing an interpreter
breaks the harness rather than the tool under test: the shim is a script run by an
interpreter and its directory goes FIRST on a PATH the orchestrator also reuses for
`run_command`, so `tool: python3` made the shim re-resolve its own interpreter to itself —
an exec loop that spins to the task timeout.

## The checker base class

### Exactly one of _check_impl or _check_impl_async

Async is the strictly more general shape — it covers a CPU-bound check and a genuine I/O
one — so the PRIMARY surface is async and a checker implements whichever form is natural.
The base derives the other: a sync `_check_impl` is offloaded with `asyncio.to_thread`, and
a native-async checker's sync entry point runs the coroutine to completion.

`__init_subclass__` enforces EXACTLY one at class-definition time, so the check cannot be
escaped by registering through the registry directly instead of the decorator. Overriding
neither would recurse forever between the two defaults the first time either is called, and
overriding both would let two live implementations drift into different scores for identical
agent output depending on which entry point ran — the class of bug the derivation exists to
eliminate. `abstract=True` opts a shared base out; its own subclasses are still checked.

### What escalates instead of scoring 0.0

A judge-infrastructure outage and a checker-contract misuse are not agent failures, so they
propagate to `FinalStatus.ERROR` rather than being captured into a scored 0.0. This is the
CE039 distinction, and it is why several sites raise rather than assert: the call runs inside
a wrapper that catches plain `Exception` (`AssertionError` included) and downgrades it, which
is the opposite of the intended behaviour — and an `assert` is stripped under `-O` anyway.

`reference_comparison` is the worked example on the task-definition side. A typo in
`reference_file` scored as 0.0 counts against the agent's pass rate, and on a dataset-fanned
suite it silently zeroes every row and drags down the aggregate mean, the `suite_thresholds`
gate, the JUnit report and the evalboard alike. A missing AGENT file is the opposite case and
is genuinely a gating 0.0. `reference_file` is confined to the reference directory, unlike a
judge's author-written `files:` entry, because it names one file of the solution being
compared against and traversal out of the staged copy is always a mistake.

`skill_triggered` escalates the same way when its `skill_name` is not among the skills
`agent.plugins` offered (`CheckContext.skills_offered`). Resolution refuses the same task
first (`validate_plugins`), so this gate fires only on a detached grade of a recorded run. The agent was never offered the
skill, so the positive control cannot run. Scored as 0.0, every positive row of an
activation suite would read as a skill that never triggers. The gate applies only when the
task sets plugins: with `skills_offered` `None` the criterion scores as before, so a skill
the harness finds by other means still counts.

Grading time is accumulated at the checker, not at the four orchestrator call sites, so a
fifth site cannot be added without it — the same reason the tool subtraction lives at one
collector seam. It is monotonic, booked in a `finally` so a grade that raises still records
what it spent, and `None` until something is checked so an ungraded row reports "never
measured" rather than an instant 0.0 (CE058).

## Route resolution

The agent's route and the evaluation side's route are resolved separately.
`resolve_evaluation_route` decides the `llm_judge` / `agent_judge` transport. The
override travels one way: `checker_context.api_route.model` is the task-authored INPUT,
and it lands on the RESOLVED route's `model` — set only when a real override was given,
never from the agent's own model. `criterion.model` is `None` rather than a materialized
default when unset, so the precedence (explicit per-criterion model, then the route's, then
the default judge model) survives a `model_dump(mode="json")` and reload, which a
`model_fields_set` check would not.

An EXPLICIT backend override that cannot be honored RAISES, naming the missing env var,
rather than falling back to a different backend. That is the load-bearing rule of
`_resolve_backend_route`: the override exists because a task author named a backend, and
quietly grading on another one publishes a verdict from a route nobody asked for. The
tempting edit is a fallback (`or settings.bedrock_model`); it is the regression this rule
forbids.

It raises rather than asserts for a second, independent reason: the path is reached on the
evaluate-only flow with no preceding key validation, so the check must survive `-O`. The
exhaustive final arm of each match is unreachable but present, so every path returns
explicitly.

Under `DirectRoute` the judge transport is resolved at startup: `anthropic` when a key is
present, `None` otherwise, in which case an enabled `llm_judge` fails at dispatch. The
Bedrock backend routes the judge through the run's own backend and never reaches that
selection. The transport-unconfigured arm short-circuits BEFORE backend dispatch.

Small-model fallback matters more than it looks: Claude Code routes page-summarization and
other small, fast steps through the small-fast model env var, which on Bedrock is exported
only when `small_model` is set. Leaving it unset made every WebFetch fail with "model
issues", so the main model is the fallback.

### LiteLLM params and env_params

Neither route object carries a base URL or a credential. They flow through orchestrator
state — `environment_info` recording, logging — that has no business handling config which
should be read live from the environment, so the agent path reads settings itself at the
point of use.

The CHECKER side is not sourced from settings AT ALL. A gateway-routed judge model rarely
reuses the proxy or credential the agent's own backend points at, so there is no implicit
fallback: the task author owns the call shape. `params` is passed to `litellm.acompletion`
verbatim as extra kwargs, and `env_params` maps a kwarg name to the ENV VAR NAME to resolve
it from at call time — so an arbitrary provider's config, secrets included, is representable
without a secret landing in the task YAML. `env_params` values are env var names, never
secrets, so recording them verbatim is safe; `params` is not, because an author could put a
raw secret in one.

Calling through the library rather than assuming one wire protocol lets `model` carry its own
provider hint and get that provider's real request and response shape handled, including
per-provider quirks. `drop_params` covers parameters the library's own static cost map knows a
model rejects; a custom or gateway-routed model id usually is not in that map, so it alone
does not protect a `params`-supplied kwarg the target model live-rejects.

## Judge context and untrusted text

A judge reads agent output and tool-call summaries, which are UNTRUSTED text. Everything
below exists so that what the judge was shown, and what is persisted afterwards, stay
under the harness's control rather than the agent's.

`$TASK_DIR` and `$REFERENCE_DIR` in a judge's `files:` are resolved against host directories
and read from the host filesystem, mirroring the same-named env vars `run_command` exposes so
judges and shell criteria address the same places by the same name. `$REFERENCE_DIR` is how a
task attaches SPECIFIC grading assets instead of the whole tree, and it is readable here only
because judges run outside the agent's turn — the directory sits at mode 000 for the whole of
`agent.communicate`.

### Scrub before truncate

`scrub_reference` redacts by exact substring, so ordering is load-bearing everywhere it is
used. Clip first and a multi-KB reference cut by a per-field budget leaves a partial fragment
that no longer matches the full secret: `replace` finds nothing and the prefix is persisted
unsanitized. Scrubbing first replaces the secret with a short marker before any clipping, so
on-disk fields can never carry partial reference content. The same rule is why a truncating
renderer must pass its per-file cap into the key collection — a key built only from
untruncated text would never match what the judge was actually shown.

The secret list is `list[str]`, not `str | Iterable[str]` and not `Sequence[str]`: `str`
satisfies both of those, so a caller passing a bare string type-checked clean and then had its
CHARACTERS iterated as individual secrets, each under the length floor, silently redacting
nothing. Secrets shorter than the floor are skipped, because redacting a tiny common substring
would produce gibberish and is not a realistic leak vector — with the caveat that in directory
mode every file becomes its own entry, so a one-line `__init__.py` is left unscrubbed. The
empty input is a no-op, guarding the `"".replace("", ...)` pathology that ballooned strings.

### What counts as reference-derived

The scrub gate keys on the recorded reference-derived text being non-empty, NOT on
`include_reference`. A `$REFERENCE_DIR/...` entry in `files:` attaches reference bytes with
`include_reference=false`, which is the documented way to show a judge one rubric without
inlining the tree — keying on the flag left exactly that combination persisting the solution
verbatim into the archived transcript.

Scrub keys are the per-FILE contents, not the single rendered block: a model is far more
likely to echo one file back than to reproduce the whole concatenation verbatim, and a
whole-block key would never match. `agent_judge` has TWO routes reference bytes take to the
judge — inlined `files:` entries, and the `_reference/` mount it browses directly — so both
are collected, and both the truncated and untruncated forms are emitted.

### The reference walk's budget and symlink rules

One walk backs both consumers, so the budget and symlink rules cannot diverge between what
the judge is shown and what is redacted from its output — a divergence there leaks reference
content into a persisted transcript.

Symlinks are NOT followed: a reference bundle shipping `secrets -> /etc/passwd` would
otherwise read a host file into the scrub-key list, and a symlinked subdir back to the root
would loop forever. Binary and unreadable files are skipped silently. File count and total
bytes are capped, sized well above any realistic reference skeleton, and the size is
pre-checked with `stat` BEFORE reading, so a single huge file is not pulled into memory in
full before the per-iteration check fires. A directory at mode 000 — meaning this was called
during an agent turn, which should never happen — yields nothing rather than raising.

Rendering drops TRAILING files rather than truncating mid-block, and says so explicitly, so
the judge does not read the omission as "the reference doesn't implement that". The same
shape applies to a dialog transcript, where the per-message cap is applied first so one huge
message cannot crowd out later turns. Remaining transcript budget is split by importance
rather than evenly — an even split clipped the verdict, the most important field, for tasks
with long rubrics.

When both routes are in play the judge is told what the `$REFERENCE_DIR/x` label maps to: its
shell has no such variable and its workspace exposes the tree at `_reference/`, so a judge
asked to re-Read a file it was shown could not otherwise resolve the path it was given.

## Sub-agent judging

### The judge's identity is its system prompt

A sub-agent's system prompt is its ENTIRE identity — judge instructions, simulator persona —
so the coding-agent preset must never prefix it. The judge must not carry an engineering
persona ahead of its grading role, and its verdicts must not shift when the preset does.
That takes both halves: an omitted prompt gets the bare preset, which is the same failure,
so neither is accepted. The runner RAISES on a misconfigured caller rather than mutating the
config, because the field is part of the type contract and callers own theirs; not `assert`,
so the check survives `-O`.

When YAML supplies a partial `agent:` block, pydantic constructs a fresh config from those
keys and the judge defaults never apply — so user-set fields are overlaid on top of a fresh
judge default instead, keeping the hardened defaults for every key the user did not set.
`sdk_options` gets a deep merge to match the experiment layer, which is a no-op today and
prevents a foot-gun when judge defaults grow. `system_prompt_file` must be cleared in the
SAME update as `system_prompt`: they are mutually exclusive and assignment is validated, so
a sequential assignment would raise on the intermediate state.

### The security floor

The judge runs with the evaluator's credentials and can execute arbitrary Bash by default,
against four surfaces: artifacts it executes, prompt injection from included agent output,
credential exfiltration through any network-capable tool, and hooks or MCP servers the main
agent planted. `setting_sources=[]` is forced regardless of user YAML so the SDK does not
load settings or MCP config from the judge's cwd — those can install pre-LLM lifecycle hooks
or MCP subprocesses that run with the evaluator's credentials BEFORE the allowed-tools gate.
The ignore-patterns floor is set-unioned in so it is present even when the user supplied
their own list, and the verdict tool is forced into `allowed_tools` because the judge must be
able to report. Author convenience does not override the floor. `llm_judge` is the answer for
adversarial generation.

The sandbox is copied into an isolated temp dir, so the sub-agent never touches the original
and later criteria are unaffected. Symlinks are SKIPPED rather than preserved, so a planted
`creds -> /root/.aws/credentials` cannot leak host files to a Bash-enabled sub-agent. The
reference mount uses a SEPARATE ignore list defaulting to empty, because the sandbox-side list
contains `_reference` as defense-in-depth against an agent-planted collision and reusing it
would silently drop a user's own nested `_reference/` subdir. The mount point is rmtree'd
first and the copy deliberately does NOT pass `dirs_exist_ok`: if any file survives, a loud
failure beats silently merging the reference into agent-planted content under the same path.

### Cancellation safety

`run_async` is awaited on the orchestrator's own loop, so it is reachable by cancellation at
any await — including mid-copy. `asyncio.to_thread` is NOT itself cancellable: the worker
keeps running after the awaiting coroutine raises. Every such call is therefore shielded and
tracked, and the `finally` awaits any still-in-flight one BEFORE the cleanup, so an orphan
thread can never recreate files after cleanup already ran. The temp directory itself is bound
with a plain synchronous call, because offloading a single fast syscall only widens the
cancellation window. The cleanup itself is deliberately synchronous: a bare `await` inside
`finally` is cancellable, and cancelling as that line is reached would skip cleanup and leak
the copy with no reaper.
