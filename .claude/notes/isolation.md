# Isolation: docker, sandbox, detached grading

> Conventions and authority order: see [README.md](README.md).

## Detached grading and `Sandbox.adopt`

- **Detached grading (`evaluate` over a run dir) + `Sandbox.adopt`**: `coder-eval
  evaluate` takes two shapes, told apart by a **pure** resolver
  (`cli/evaluate_target.py`) on one probe — a target holding `task.json` is a run
  directory. Run-dir mode rebuilds the task from the run's own `task_config.resolved`,
  **not** by re-loading the YAML: `resolved` is post-merge, so variant overrides / `-D`
  / dataset expansion are already baked in and re-loading the source would silently
  grade a *different* task (fallback to `source_file` only when `resolved` no longer
  validates, and loudly). It seeds the fresh result from the prior one via
  `Orchestrator(prior_result=...)` → `_seed_from_prior_result`, which carries the
  trajectory (every derived figure — tokens, cost, `command_stats`, `model_used` —
  recomputes from `iterations`), `iteration_count`, execution facts, and **`early_stop`
  — load-bearing, because gate selection is FIRED-ONLY**: dropping it re-grades a
  truncated trajectory under the full-run strict-AND gate and can flip the verdict.
  Carrying it is only half the fix — **both** grading paths select the gate through the
  single `Orchestrator._select_gate()`; the evaluate-only branch a detached grade
  actually takes originally called `all_criteria_passed` inline, so the seeded field was
  written and never read. `tests/test_seed_from_prior_result.py` partitions every
  `EvaluationResult` field as CARRIED or RECOMPUTED and fails closed on a new one, and
  asserts the two `_select_gate()` call sites. A prior status that
  `FinalStatus.is_execution_fact` (TIMEOUT / ERROR / BUILD_FAILED / the budget stops) is
  **preserved**, never overwritten: grading may only move `NOT_GRADED` to
  SUCCESS/FAILURE, since it neither repeated nor observed the agent phase.
  **`MAX_TURNS_EXHAUSTED` is deliberately NOT one of them — anywhere**.
  `_EXECUTION_FACT_STATUSES` maps it to `False`, and the table and the chain that reads
  it must agree: it shipped as `True` while `_terminal_status`'s own docstring argued
  the opposite, and the disagreement pinned a re-graded max-turns row at
  MAX_TURNS_EXHAUSTED *while holding `weighted_score` 1.000* and exit 1 — a combination
  `run` can never produce for the same trajectory. Under `execute`: `_terminal_status`
  puts the `grade=False` arm ABOVE it, because on the graded path it is subordinate to
  the verdict — `run` returns SUCCESS for a max-turns trajectory whose criteria pass —
  so it is not knowable without grading. Consuming it first made it terminal AND
  permanent (the `is_execution_fact` arm then pinned it), so identical agent output
  scored SUCCESS/1.0 under `run` and MAX_TURNS_EXHAUSTED under `execute` → `evaluate`.
  The fact survives on `result.max_turns_exhausted`, which `_seed_from_prior_result`
  carries, so the detached grade walks the identical chain. The CLI must also branch on
  WHERE a status came from, not on its value: a preserved TIMEOUT exited 0 under "All
  criteria passed" (a CI wrapper reading the exit code went green on a row run.json
  counts as failed), and a preserved ERROR printed the ORIGINAL run's crash message as
  though grading had crashed, claimed the row was "left ungraded" (false — the restored
  record still read ERROR), and discarded a verdict just computed at 1.000. Grader-host
  `environment_info` is preserved as flat `graded_by_*` scalars rather than overwriting
  the run's (flat, not a nested sub-dict: `environment_info` is rendered as a flat map
  by the HTML report and typed as one by the evalboard, so a nested capture prints as a
  Python dict repr). The route recorder follows the same rule: on a detached grade it
  writes `graded_by_api_routing` / `graded_by_eval_routing` and leaves the run's
  `api_routing` alone — writing in place contradicted the "prior wins" contract and left
  a self-contradictory record (a direct route named beside the run's stale
  `aws_region`/`bedrock_model`). Two other parity fixes: `command_base_path` is now
  persisted into `environment_info` by `_sync_sandbox_command_path_with_agent` and
  restored in the evaluate-only branch (closing the PATH gap that method's docstring
  already named), and `_join_litellm_actual_cost` **skips** when `prior_result` is set
  (its join keys on a per-Orchestrator nonce the prior turns never carried, so it would
  clobber already-correct costs). The verdict is written back into the run's
  `task.json`, with the pre-grade record kept as `task.execute.json` — that in-place
  write is what makes plain `coder-eval aggregate <run_dir>` rebuild a graded `run.json`
  with **zero** new code. **`Sandbox.adopt(workspace)`** is the grade-in-place
  primitive: it reuses `setup`'s adoption half but skips every *materializing* step
  (`_setup_template`, `_generate_cli_recorders`, venv/package installs, the destructive
  `$HOME` remediation), running only non-mutating derivation (mock-dir `+x`, venv
  *discovery*, plugin-tools pin); `_cleanup_on_exit` stays False so an adopted tree is
  never moved or deleted, and `Sandbox.was_adopted` is set — the Orchestrator reads it
  to SKIP the `pre_run` hook (`run()` calls it unconditionally with `cwd = sandbox_dir`,
  and several in-tree tasks stage fixtures there with `cp -a /app/[!.]* "$PWD/"`, which
  would overwrite the agent's deliverables before the criteria read them) and to KEEP
  `sandbox_path` in the `PreservationMode.NONE` cleanup arm (an adopted tree survives
  cleanup, so the path is not stale). `pre_run`'s recorded results are carried from the
  prior run instead. **`post_run` is the opposite case and moved phases**: it is defined
  as running after the verdict and may mutate the workspace the criteria read (`rm -rf
  node_modules` is the archetype), so running it under `execute` inverted its own
  contract and broke round-trip equivalence — the criteria had not read the tree yet, so
  `execute` + `evaluate` graded a workspace `post_run` had already modified and could
  return a different verdict than a single `run` for the identical trajectory (the
  in-tree tasks all escaped it only because their `post_run` touches nothing a criterion
  reads). `execute` now DEFERS it; whichever command grades runs it, exactly once —
  `_skip_post_run` skips on `grade=False`, and skips again when the prior row already
  recorded results, since nothing declares these commands idempotent. That makes it a
  capability of the in-place path, so `embedded_commands` scans it OUTSIDE
  `include_setup_phase` (which is False in place) — minus
  `_operator_baseline_post_run()`, the grading host's own `experiments/default.yaml`
  contribution, which every task carries and the record therefore did not choose;
  without that exemption the refusal fired on 100% of run directories, and a refusal
  that always fires is waved through. In-place is **more correct**, not merely faster:
  `_setup_template` filters the copy through `_should_ignore_template_file`, which drops
  `node_modules` / `dist` / `build` / `.venv` / `.git`, so on the copy path a criterion
  like `test -f dist/bundle.js` fails as a *copying artifact* rather than as a verdict
  (verified: 0.00 "does not exist" on copy vs 1.00 in place). Defaults: in-place for a
  run dir, copy for a bare work dir (criteria can mutate it and it is the user's own
  tree); `--in-place`/`--copy` override. `adopt` hard-errors on `driver: docker` (a
  container workspace is unreachable from the host), and grading a `driver: docker` task
  is DISPATCHED INTO A CONTAINER of the task's own image (`_should_grade_in_container`
  -> `_grade_in_container` -> `DockerRunner(prior_result=, grade_workspace=)`), because
  that is the only place its criteria mean what they meant during the run:
  `tasks/byod_smoke_test.yaml` asserts `test -f /opt/byod_marker`, baked into its image,
  and the IDENTICAL row scores SUCCESS 1.000 in a container and FAILURE 0.000 on the
  host — the host answering a question nobody asked. The grading container gets TWO
  mounts and their separation is the design: the grading pass's own fresh `run_dir` at
  `CONTAINER_OUTPUT_DIR` (whose `task.json` the host then folds back into the row,
  preserving `task.execute.json` exactly as on the host path) and the executed workspace
  at `CONTAINER_GRADE_WORKSPACE`, read-WRITE and NOT a copy, adopted rather than written
  over. The container half reuses the same `regrade_in_place`
  (`run_task_internal_command._grade_recorded_run`, driven by `context.json`'s `regrade`
  flag plus a staged `prior.json`) rather than restating it. A container-graded row
  carries NO `graded_on_host` stamp, so it is indistinguishable from a `run` row, which
  is the parity that makes the split honest. `--allow-host-grading` survives as the
  ESCAPE HATCH (no docker on this machine; criteria known to be host-portable) and still
  stamps. **The dispatch is itself inside the trust gate**: the record names the image,
  and a container of it runs with the default credential allowlist (`ANTHROPIC_API_KEY`,
  `UIPATH_ACCESS_TOKEN`, `AWS_BEARER_TOKEN_BEDROCK` ...) forwarded in and a copy of
  `~/.claude` mounted — a strictly WIDER capability than the `run_command` strings the
  gate already refuses, and it shipped reachable with no flags because
  `embedded_commands` walked only `success_criteria` and `post_run`. That is the same
  blind spot the function's own docstring already described for `--copy` provisioning
  ("a shared run directory whose criteria were all `file_exists` sailed through"), one
  layer up, so `include_container_dispatch` scans it on the in-place path exactly as
  `post_run` is — rendering the whole dispatch as ONE command string (the prompt joins
  with `"; "` and counts `len(commands)`, so an argv fragment appended as its own entry
  reported one `docker build` as four shell commands), and naming every HOST PATH it
  exposes: the task DIRECTORY copied from the recorded `source_file`'s parent (a record
  naming `~/.ssh/config` copies all of `~/.ssh` in), every auto-mounted
  `agent.plugins[].path` / `TemplateDirSource.path` / `system_prompt_file`, and the
  writable `~/.claude` copy. Disclosing only `sandbox.docker.*` asked the operator to
  consent to a strict subset of what happens. Which families the gate discloses follows
  from ONE parameter (see § Detached grading from the CLI). Three further properties are
  load-bearing and were not free: the grading container gets a **scratch** run dir,
  never the caller's — `run --resume` passes the executed row's OWN directory, where
  `_parse_result_or_raise` (which keys on `task.json` existing and discards
  `returncode`) read a dead container's stale pre-grade record back as a successful
  grade, and where `docker.log` was truncated; the recorded `source_file` is the HOST's
  path (`Orchestrator.recorded_task_file`, the path twin of `recorded_task`), because a
  container run recorded `/work/task_dir/task.yaml`, which exists on no host, so the
  dispatch guard's `task_file is None` test passed and `_prepare_task_dir_mount`'s `if
  not source.is_dir(): return` then mounted NOTHING — every `$TASK_DIR` criterion
  silently resolving against the wrong tree; and the dispatch is guarded against an
  image that ignores the `regrade` key (see § The two honored-request guards). The
  grading container is a SECOND, fresh container: only the workspace crosses and
  `pre_run` is not re-run, so a criterion depending on out-of-workspace state
  (`tasks/samples/skillsbench/3d-scan-calc` symlinks `/root/mass_report.json` in
  `pre_run` and its verifier asserts that path) scores 0.000 for a trajectory `run`
  scores 1.000 — warned at dispatch AND stamped onto the row as
  `environment_info.graded_without_pre_run`, since re-running `pre_run` would trade it
  for the deliverable-clobbering bug `_skip_pre_run_for_adopted` exists to prevent. The
  stamp is the load-bearing half: `stamp_host_grading`'s own docstring already says why
  ("a console warning does not travel with `task.json` into `run.json`, the reports or
  the evalboard"), and 3 of the 10 in-tree docker tasks match the pattern, reachable
  with NO flags via `execute` -> `run --resume`. `dockerfile_path` is the second, weaker
  gap and is stamped the same way (`graded_with_rebuilt_image`): `_build_image` re-runs
  `docker build` under the deterministic tag `coder-eval-task-<id>:built`, so the
  grading image REPLACES the run's, and nothing pins image identity on either side — a
  `reference_digest`-style pin is the real fix and needs the RUN path to record it
  first, so for now the row says it happened rather than the guide claiming a control
  that does not exist. The grading container's own logs are folded out of the scratch
  dir in a `finally`, not only on success: `docker.log` (as `grade.docker.log`, since on
  the resume path that name is the executed run's) and `grade.log`, which is a
  documented run-layout artifact holding the per-criterion detail. Folding out only on
  success deleted exactly the evidence, while DockerRunError's own text said `See
  {log_path}` — a path already gone by the time it printed. Both copies refuse a
  symlinked destination, because `shutil.copy2` follows one and the sibling verdict
  write goes through `write_text_atomic` for precisely that reason; and the verdict
  write raises `RegradeError`, never a bare `OSError`, since it sits outside the
  dispatch `try` where `evaluate` (which guards only `RegradeError`) let it escape into
  Typer AFTER a successful grade while `run --resume` caught it and reported a correct
  verdict as a grading failure. A container grade also emits its own
  `CoderEval.Task.End` host-side (`_emit_task_telemetry`), mirroring `batch.py`: the
  grading path had inherited only the silent half of the container-silent invariant (§
  Environment forwarding). The dispatch is gated on `IN_CONTAINER_ENV`, never on the
  driver — the in-container entry point rewrites `docker` -> `tempdir` before building
  its Orchestrator, so a driver-based test would read an already-changed value and a
  grading container would dispatch a grading container. That env var now has ONE
  definition (`models/container_paths.py::IN_CONTAINER_ENV`), and **CE056** keeps it
  that way — the migration converted all four READERS and left the single WRITER
  (`docker_runner`'s `--env CODER_EVAL_IN_CONTAINER=1`) on the literal, which is the one
  site that produces the value the gates consume: a rename would have updated every
  consumer and left the container exporting the old name, disarming the reference
  anti-cheat window, the reference mount, the grading-container recursion guard and the
  watchdog together, all silently. CE052 accepts both spellings — a rule that saw only
  the literal would read a constant-based gate as no gate and tell the author to paste
  the literal back, arguing against the SSOT it exists to reinforce. The earlier
  behavior silently rewrote the driver to `tempdir`, which ran a container task's
  criteria against a host filesystem lacking `/verifier` and the image's toolchain
  (FAILURE for a trajectory `run` scored 1.0, plus `rm -rf /verifier` unsandboxed on the
  grading machine) and neutralized `adopt`'s own docker guard; an opted-in row is
  stamped `graded_on_host` so it is never silently comparable with a container-graded
  one (lint rule CE051). A re-grade refuses on a `reference_digest` mismatch — the
  digest is persisted into `environment_info` at staging time by `_stage_reference` (it
  shipped once as a read with no writer anywhere, so the guard was dead code; then it
  shipped with a writer whose value was **discarded before it reached disk**, because
  `_setup` REBOUND the whole `environment_info` dict from `get_version_info()` a hundred
  lines later, which CE054 cannot see — a write existed in `src/`, it was just dead.
  `_setup` now `update()`s that dict rather than rebinding it, and
  `tests/test_detached_grading_boundaries.py` asserts the key survives a real end-to-end
  run, not just that `_staged_digest` works in isolation), and
  `verify_reference_unchanged` now takes the task file it resolves against and RAISES on
  a vanished or unresolvable reference instead of returning silently. That comparison
  digests a STAGED copy of the source, not the raw tree: the recorded digest is taken
  over the staged copy, which `stage_reference_dir` filters through
  `REFERENCE_COPY_IGNORE` (`.git`), so digesting the source directly compared two
  differently-filtered trees and reported a permanent false mismatch for any reference
  that is a git checkout — exactly the case the ignore list exists for. **The recorded
  config is untrusted input**: `evaluate <run_dir>` rebuilds the task from a shareable
  artifact, so a rebuilt config that carries shell (`run_command` criteria,
  `agent_judge`, `llm_judge`, `uipath_eval`, and — only on the `--copy` path, since the
  in-place path skips them — `pre_run`/`post_run` **and the sandbox's own
  provisioning**) is REFUSED unless `--allow-recorded-commands` is passed. The
  provisioning half was the one the gate originally missed, and the worst:
  `grading_sandbox_config` carries the recorded `sandbox` block through untouched and
  the `--copy` branch calls `Sandbox.setup`, which reaches `uv pip install <recorded
  packages>` / `npm install` / `git clone <recorded url>` — arbitrary code at install
  time — so a shared run dir whose criteria were all `file_exists` sailed through a scan
  that walked only `success_criteria`. `Sandbox.resolve_files` is containment-checked
  for the same reason (see § Criterion paths are contained, quietly). A warning is not a
  control: it prints as the command is already being prepared. Passing the task file
  explicitly (`evaluate <task.yaml> <run_dir>`) also bypasses it, since that config came
  from the operator. The workspace fallback `artifacts / prior.task_id` is
  containment-checked like its `sandbox_path` sibling (`task_id` is an unvalidated
  string, and `"../../.."` joins to a real directory `is_dir()` confirms),
  `_sanitize_restored_path` drops relative entries (they resolve against the grader's
  cwd) and anything inside the run dir rather than only the workspace, and
  `write_text_atomic` opens its temp file `O_EXCL|O_NOFOLLOW` — a pre-planted
  `task.json.tmp` symlink otherwise bypassed the write-back's destination symlink guard
  entirely. The record must also describe the task as AUTHORED, not as executed:
  `run_task_internal_command` rewrites `driver: docker` -> `tempdir` before building the
  in-container Orchestrator (see .claude/notes/orchestration.md § The in-container
  driver rewrite), and recording that rewrite made a docker run's own `task.json` claim
  `driver: tempdir`. Since `grading_sandbox_config` reads the driver back OUT of the
  record, `evaluate <run_dir>` on a container row skipped BOTH the
  `--allow-host-grading` refusal and the `graded_on_host` stamp and graded a container
  task against the host filesystem silently — the exact outcome that gate exists to
  prevent. `Orchestrator(recorded_task=...)` is the seam: what is recorded, as distinct
  from what is run — and `recorded_task_file` is its path twin, which must travel with
  it through EVERY caller. `regrade_in_place` and `_grade_recorded_run` shipped without
  it, so every container-graded row re-recorded `/work/task_dir/task.yaml` as its
  `source_file`, reintroducing the defect one caller down; both seams are now pinned by
  a test that drives the in-container regrade branch end to end, because deleting either
  left the whole suite green. NOTE the Typer command is a thin wrapper over
  `run_evaluation(...)`, which has real Python defaults — calling a Typer command
  function in-process hands unspecified options an `OptionInfo` sentinel, which silently
  made `in_place=None` truthy.

### What a graded row inherits, and what it does not

A task row describes the TASK, so its clock is the agent run's — not the grading pass's.
`started_at`, `completed_at` and `duration_seconds` are all restored from the prior row,
because a 10-minute run re-graded in 2 seconds would otherwise report 2 seconds, and that
figure feeds `average_duration`, the report tables and the evalboard. All three are
restored together, so the row's time fields stay consistent with each other: leaving
`completed_at` at grading wall-clock produces a triple where
`completed_at - started_at != duration_seconds`.

`setup_ms` is restored for the same reason — a detached grade ADOPTS the workspace rather
than building one, so its own setup is a different activity. `grading_ms` goes the other
way and is deliberately NOT carried: the verdict this row now holds came from THIS pass.

`pre_run` belongs to the EXECUTE phase and is not re-run against an adopted workspace, so
its recorded outcomes are carried or they vanish from the graded row. `post_run` is the
opposite — it belongs to the GRADING phase, so on a row that came from `execute` the list
is empty and this grade is about to fill it. Both are copied into fresh lists, so
appending can never mutate the prior result.

`environment_info` is merged, not replaced. The prior capture describes the machine that
RAN the task; ours describes the machine grading it. Prior wins on conflict, and ours
survives as flat `graded_by_*` scalars — flat because `environment_info` is consumed as a
flat map everywhere (the HTML report escapes each value into a table cell, the evalboard
types it as `Record<string, string | number | null | ...>`), so a nested capture renders
as a Python dict repr. Only the facts that identify the grading HOST are kept, and only
when they differ. The same rule covers the grader's API route: writing it into the run's
keys would leave a self-contradictory record — `api_routing: anthropic_direct` beside the
run's stale `aws_region`.

### Why pre_run and post_run each run exactly once

`adopt()` guarantees it materializes nothing into the workspace, but that guarantee is
only as strong as its weakest caller: `run()` invokes the hooks unconditionally, with
`cwd = sandbox_dir`. Several in-tree tasks stage fixtures there (`cp -a /app/[!.]* "$PWD/"`),
so re-running `pre_run` during a detached grade would overwrite the agent's deliverables
BEFORE the criteria read them — silently changing the verdict and destroying preserved
artifacts.

`post_run` runs after the verdict is finalized and is free to mutate the workspace
(`rm -rf node_modules` is the archetype), so it belongs to whichever phase GRADES.
Running it under `execute` inverted its own contract and broke round-trip equivalence:
the criteria had not read the tree yet, so `execute` + `evaluate` graded a workspace
`post_run` had already modified. Its two skips are NOT the same condition — `grade=False`
DEFERS it to the grading pass, while an adopted sandbox whose prior row already recorded
`post_run` results has already run it once, and nothing declares those commands
idempotent.

A run that is never graded therefore never tidies its sandbox — that is the accepted cost
of keeping the verdict honest.

### Detached grading from the CLI

`evaluate` decides its two shapes with a **pure** resolver over one filename probe, so a
plain work directory that merely happens to hold a file called `task.json` reads as a run
directory. The resolver cannot tell a real record from a namesake, so the caller re-reads
the record and falls back to WORK_DIR when it does not parse — without that fallback the
pre-existing `evaluate <task.yaml> <dir>` form aborted on a pydantic wall the user could
escape only by renaming their own file. The two-argument form over a *run* directory is
allowed on purpose: it is "iterate on my criteria against an expensive run I already paid
for", which is most of the reason `execute` and `evaluate` are separate commands.

The grade is dispatched by branching on `prior is not None` directly, and the sandbox is
built inside the branch that uses it, so neither value is `Optional` at its use site.
Building a host `sandbox_config` first called `grading_sandbox_config`, whose whole job is
to REFUSE a `driver: docker` row — so the refusal fired before the branch that no longer
needs it, and no docker row could be graded properly at all. (A bare `assert` plus a
comment asserting an invariant the type checker can hold structurally is the weakest
narrowing available; it is stripped entirely under `-O`.)

The orchestrator-direct branch — fresh work-dir grading, or `--copy` — is the sibling of
the delegating one and needs the same two things stated only once for the delegating path:
a criteria-free task must be refused rather than finalized SUCCESS at `weighted_score`
0.0, and a host-graded row must carry the `graded_on_host` stamp. CLAUDE.md, the user
guide and CE051's own `noqa` all state that stamp as unconditional, so
`evaluate <run_dir> --copy --allow-host-grading` was writing an unstamped host verdict
nothing downstream could tell apart from a container-graded one.

Errors are rendered unwrapped, like the sibling handlers: the delegating branch raises for
a missing or unresolvable task file and for a failed grading container, and both messages
carry the operator's next step, which arrived as the tail of a stack trace instead. The
grading-crash check must run BEFORE the criteria-count guard: `Orchestrator.run()` converts
internal failures into a populated ERROR result with an EMPTY criteria list, so the count
check fired first and the user was told only "Result count mismatch: got 0, expected 2" —
the real error never printed, and the "still re-gradeable" notice unreachable on exactly
the path it was written for. On that path the pre-grade record is restored: leaving ERROR
on disk replaces a re-gradeable NOT_GRADED row with one BOTH commands treat as permanently
complete.

`_write_back` is gated on RUN_DIR mode, not merely on `prior is not None`. `--format
harbor` seeds a SYNTHETIC prior on the WORK_DIR shape from the supplied `--trajectory`;
that target is not a run directory and has no `task.execute.json` sibling to preserve, so
writing there planted a spurious `task.json` into the Harbor-synced workdir and wedged a
later `evaluate` on it into RUN_DIR mode. The write refuses to follow a symlink — a run
directory is a shareable artifact, so its `task.json` is untrusted input and following a
link turns `evaluate <run_dir>` into an arbitrary-file-overwrite primitive on the grader's
host — and it is atomic, matching the orchestrator's own writer, because a torn write makes
the row parse as malformed, which a later `--resume` reads as "not complete" and re-pays
for the agent.

The pre-grade snapshot is taken BEFORE anything grades. Taking it inside `_write_back`
captured an ALREADY-GRADED record whenever `--run-dir` pointed at the target run dir (the
orchestrator writes there first), destroying the evidence the copy exists to preserve.

The recorded-shell gate takes ONE lever — whether the grade happens in place — passed once
and derived through the same function `run_evaluation` uses (`_gate_scope_for_grade`). A
second copy of the rule keeps answering the old question the moment the default moves: the
lever shipped beside an `include_setup_phase` every caller passed as its exact complement,
and a caller setting one and forgetting the other would silently drop half of a SECURITY
gate. In place, the grade may dispatch a
CONTAINER built from the recorded sandbox block, a wider capability than any recorded shell
string; on `--copy` instead, `pre_run` and the sandbox's own installers, neither of which an
adopted workspace reaches. `post_run` is in NEITHER set — it belongs to the grading phase
and runs on both paths, so it is scanned unconditionally.

## Grading a docker row inside a container

A `driver: docker` row is graded IN a container of its own image, which is the only place
its criteria mean what they meant during the run. The dispatch happens before anything
else — the reference check included — so the container performs every step against
container paths rather than having half of it done against the host's.

Host-grading a container task is REFUSED rather than downgraded. Its criteria address
container paths (`/verifier`, `/logs/verifier`) and container toolchains; run on the host
they score 0.0 for a trajectory `run` scored 1.0, and the row is written back FAILURE. The
same commands (`rm -rf /verifier`, `mkdir -p /logs/verifier`) also execute unsandboxed on
the grading machine. A silent rewrite additionally neutralized the `docker` refusal in
`Sandbox.adopt`, which exists to catch exactly this. `--allow-host-grading` is the
operator's explicit acceptance of both, and such rows are STAMPED `graded_on_host` so they
are never silently comparable with rows a container graded.

(The host-grading config's docstring once opened "grading never runs a container: the
docker driver dispatches through DockerRunner, which needs an agent". That premise was
simply wrong — a grading pass needs no agent — and it is why the refusal survived a
release after it stopped being the only answer.)

### Two known equivalence gaps, stamped rather than refused

Both are recorded ON the row, for the reason the host-grading stamp exists: a console
warning does not travel with `task.json` into `run.json`, the reports or the evalboard, so
a row it describes cannot be filtered out of a comparison by anything downstream.

**`graded_without_pre_run`** counts `pre_run` commands that ran in the container which
executed the agent and were NOT re-run. The grading container is a SECOND, fresh one —
only the workspace crosses over — and `pre_run` is not re-run because `Sandbox.adopt` sets
`was_adopted` and the orchestrator skips it (re-running would overwrite the agent's
deliverables before the criteria read them). It is not hypothetical: three of the ten
in-tree `driver: docker` tasks seed state outside the workspace, and `3d-scan-calc`
symlinks `/root/mass_report.json` whose existence is its verifier's first assertion — so
such a row scores 0.000 for a trajectory `run` scores 1.000. Refusing outright was
declined: it would break `run --resume` on rows it grades correctly today whenever
`pre_run` happens to touch only the workspace, which is the common case. A durable,
machine-readable marker lets a consumer decide; a refusal does not.

**`graded_with_rebuilt_image`** marks the other: the pass re-runs `docker build` under the
run's deterministic tag, so the grading image REPLACES the run's under the same name. A
Dockerfile, build context or base image that moved between the phases means the criteria
read a different filesystem than the agent did. Nothing pins or records image identity on
either side yet, so it cannot be detected after the fact — which is exactly why it is said
at dispatch. A `reference_digest`-style pin is the real fix and needs the identity
recorded on the RUN side too.

### Why the grading container gets a private scratch directory

The two callers disagree about what `run_dir` is — `evaluate` passes a freshly prepared
directory, `run --resume` passes the executed row's OWN — and every part of
`DockerRunner`'s result handling assumes an output dir it alone populates:

- `_parse_result_or_raise` decides "did the container produce a result?" on
  `task_json.exists()` and discards the return code. Over the row's own directory the
  pre-grade `task.json` is already there, so a grading container that DIED (OOM, exit 137,
  or any in-container FATAL guard) was read back as a successful grade — returning the
  stale ungraded row as the verdict, with the container's error discarded.
- `run()` opens `run_dir/docker.log` with mode `"w"`, truncating the executed container's
  log — the same loss the task.log/grade.log split exists to prevent.
- `grant_container_access(output_dir, writable=True)` would recursively widen the whole
  preserved artifacts tree.

A private directory makes both callers identical.

Its logs are folded back on BOTH the success and the failure path, and the FAILURE path is
what makes it necessary: the scratch dir is deleted the moment the `with` exits, and
everything explaining a failure lives in it — `docker.log`, a failed build's captured log,
the synthetic `BUILD_FAILED` records. Folding out only on success deleted precisely the
evidence, and the error's own text says "See {log_path} for container output", naming a
path that no longer existed by the time it was printed. `grade.log` is the grading pass's
OWN log, holding the per-criterion detail that is the only durable record of WHY a
criterion scored what it did, and a documented part of the run-directory contract.

The fold-back refuses to write through a SYMLINK: `shutil.copy2` opens the destination for
writing and follows one, which is an arbitrary-file-overwrite primitive in a run directory
the grader did not create. `docker.log` is renamed for the PHASE, because on the resume
path that name is already taken by the executed container's log.

## Capability drops and the anti-cheat window

The container runs as root, and root bypasses ordinary file permissions via
`CAP_DAC_OVERRIDE` / `CAP_DAC_READ_SEARCH`. Without dropping both, the mode-000 window
`fs_permissions.py` puts around every agent turn is a NO-OP on native Linux — verified: a
`chmod 000` directory is still readable by root in a default container, and Permission
denied once the two caps are dropped. It appears to work on macOS Docker Desktop even
without the drop, because virtiofs enforces host-side; that is a platform accident, not
the rule. Nothing in a sandbox legitimately needs to override discretionary access
control, so dropping these costs the task nothing.

**FOWNER and CHOWN are deliberately NOT dropped**, though an earlier revision dropped
them. That decision, the deliberate re-chmod it leaves open, and the measured evidence for
both are owned by
[docs/DOCKER_ISOLATION.md § Architecture](../../docs/DOCKER_ISOLATION.md#architecture);
they are not restated here.

The window is a real control only inside a container, where the filesystem is private to
one task. On the host (`driver: tempdir`) it is a deliberate no-op: parallel tasks in one
batch share the checked-out `tasks/<name>/` tree, so chmod-ing it is a cross-task side
effect on the user's own working copy for no isolation benefit — there is no boundary to
enforce when the agent is just another process with the same uid. The predicate is the
`CODER_EVAL_IN_CONTAINER` env var, NOT `config.driver`, because the in-container entry
point rewrites `driver: docker` to `tempdir` before constructing the orchestrator, so a
driver-keyed predicate would read "tempdir" inside the container and disable the window on
exactly the path that needs it.

### grant_container_access

Dropping DAC_OVERRIDE revokes root's bypass on EVERY framework-owned bind mount, not just
the reference. The container runs as root but OWNS none of them: on native Linux the mount
preserves the uid that ran `coder-eval`, so every access root makes is an "other" access
that only ever succeeded via the capability. Dropping it therefore also revoked the
container's ability to write its own output — the in-container orchestrator died on the
very first `open('/work/output/task.log', 'w')` with EACCES, taking every `driver: docker`
task with it.

Widening the *host* side restores that access through the `other` bits instead of through a
capability, which is what keeps the drop affordable. Semantics match `chmod -R o+rwX`
(`o+rX` when `writable=False`): the `X` form adds execute only to directories and to files
that are already executable, so a copied hook script stays runnable and a data file does
not silently become one. Symlinks are skipped via `lstat` — `chmod` follows them, and for
the `~/.claude` copy the target can be an arbitrary path outside the staging tree.

`writable=False` is not cosmetic. The container only ever *reads* and `chmod`s the
reference copy, so withholding the write bit keeps `_verify_reference_integrity` from being
the sole guard against tampering during the gaps between windows. The task-dir copy is
read-only for the same reason: criteria read fixtures there, nothing legitimately writes
them, and withholding `o+w` keeps an agent from rewriting the expectations it is graded
against. The `~/.claude` copy IS writable — the CLI rewrites settings and state in place,
`copytree` preserves host modes, and `~/.claude` is routinely 0700 with 0600 files, which
without DAC_OVERRIDE is unreadable to the container, so the agent cannot authenticate.

The function returns `(path, original_mode)` for every entry it changed so a caller that
widened a tree it does not own can put it back. The framework-created staging dirs are
disposable and ignore it; the graded workspace is not. That workspace is the one mount
whose files the harness did NOT create, so the owner bits cannot be assumed — it happens to
work when the executing container (as root) wrote the tree, which is what makes the broken
case expensive: an operator-supplied `--workspace`, or artifacts re-created host-side, are
owned by the host uid, and container root without DAC_OVERRIDE reaches them only through
`other`. Criteria then fail EACCES and book a gating 0.0 that reads as an agent failure —
the CE039 shape this feature exists to end. Restoring it matters because the tree survives
the dispatch: an operator-supplied `--workspace` left world-writable forever is a real,
permanent exposure on a shared host.

No-op on Windows, where POSIX mode bits are not the access-control mechanism.

### Why the framework mounts are writable copies

The in-container orchestrator holds the reference and task directories at mode 000 for the
duration of every agent turn, and neither obvious alternative works:

* `:ro` rejects the chmod outright — verified: `chmod: /ro: Read-only file system`. No
  window is expressible at all.
* Read-write *without* a copy chmods the operator's REAL `tasks/` tree. Verified: the host
  directory came back 0600 and even the harness's own cleanup then failed with `Permission
  denied`. A crashed run would strand a checkout at 000.

Shielding the whole task tree, rather than masking just `reference.directory` with a tmpfs
as the old symmetric `-v <host task dir>:<host task dir>:ro` mount required, also closes a
leak the mask could not: a task at `tasks/foo.yaml` has parent `tasks/`, so the old mount
exposed every SIBLING task's directory, reference solutions included.

Symmetry was never load-bearing. The container is told where the task dir is via
`--task-dir`, and that path only seeds `TASK_DIR` — it is never re-read. `TASK_DIR` is
exposed solely to criterion subprocesses, so the agent has no legitimate need for this tree
mid-turn. The reference gets its own dedicated mount at `/work/references` and an empty
tmpfs is layered over its path inside the task-dir mount; docker applies mounts by
target-path depth, so the deeper tmpfs wins regardless of argv order. The original is
deliberately NOT auto-mounted at its host path — that would re-expose it through `$TASK_DIR`,
the exact hole the mask closes.

The staging tree is removed with `rmtree_restrictive`, not `rmtree(ignore_errors=True)`: it
holds the references copy, which stays at mode 000 for the whole of every turn, and a
container killed mid-turn never restores it. `scandir` on a 000 directory raises
PermissionError, which `ignore_errors` swallows — orphaning a tempdir that holds the
reference solution.

The graded workspace is the exception that is NOT copied: criteria legitimately mutate what
they grade (a `run_command` that compiles, a `post_run` that cleans up), and copying is what
the host path proved wrong — for the reason `Sandbox.adopt` exists at all, above.

## What crosses into the container

### The lean ~/.claude copy

The host's `~/.claude` is copied to a throwaway tmp dir and that copy is mounted read-WRITE
at the host's own `~/.claude` path (HOME is forwarded, so the path is symmetric), so the
in-container CLI can write settings, session ephemera and cache without ever touching the
host's real one.

The container needs only auth, settings and plugins; everything else under `~/.claude` is
heavy, transient, or host-local state it never reads. On a real host the skip list is the
difference between a ~300 MB copy and a few MB — `security/` alone is often hundreds of MB,
and `projects/`, `cache/`, `file-history/`, `backups/`, `sessions/`, `telemetry/`,
`downloads/` and `shell-snapshots/` all accumulate without bound. The last group is volatile
per-session churn the *running* CLI rewrites continuously (this harness itself runs inside
Claude Code, so the live host tree mutates while it is copied): dropping it also shrinks the
window for a mid-walk vanish/rewrite race under `--max-parallel`, whose residual is covered
by a bounded retry. Patterns match by basename at every level, so the list is a DENYLIST:
anything not named — `settings.json`, `.credentials.json`, `plugins/` — is copied through.

Symlinks are copied AS symlinks. A plugin marketplace cache can contain a self-referential
link (`plugins/uipath -> ..`) that makes a following walk recurse infinitely and abort the
copy; copying them verbatim is correct and loop-proof.

### Environment forwarding

Forwarding is an explicit allowlist, extendable per task. `--env VAR` (name-only) tells
docker to copy the value from the current environment at run time, so secrets stay out of
the rendered argv that gets logged.

The run's backend rides that same path: `API_BACKEND` is allowlisted and `--backend` syncs
it into `os.environ` at the CLI, so it forwards like any other var. A flag that only mutated
in-process `Settings` would be dropped at the container boundary and the in-container
`Settings` would silently default to DIRECT, downgrading the judge's and the agent's route.

`LITELLM_BASE_URL` points at a proxy on the HOST, which a bridge-network container cannot
reach on loopback, so localhost is rewritten to the docker host alias and that alias is
published with `--add-host` for Linux parity (automatic on Docker Desktop). It is only a
URL, so an explicit `--env VAR=value` is safe to render in the logged argv — unlike the auth
token, which stays name-only. `LITELLM_COST_LOG` is the proxy's per-call cost log, written
on the host and READ by the in-container actual-cost join, so its directory is bind-mounted
at the same host path read-only (the host proxy is the sole writer) and the resolved
ABSOLUTE path is forwarded. Both are skipped when the container has no network.

Telemetry is hard-disabled inside the container with an explicit value, not name-only, so it
overrides any inherited or baked-in setting. The app ships a baked-in default connection
string, so without this the in-container orchestrator emits `CoderEval.Task.End` and the
host re-emits the same event after parsing the result, double-counting every docker task.
The invariant is "container silent, host emits once".

`IN_CONTAINER_ENV` tells in-container agents the harness already provides OS-level
isolation. The Codex agent reads it to fall back to its full-access sandbox: Codex's
Landlock-backed sandboxes cannot initialize inside a container and otherwise fail writes
silently.

### The entrypoint and the image contract

The framework entrypoint is pinned at run time rather than trusted from the image, so the
orchestrator launch survives a task Dockerfile that sets its own ENTRYPOINT/CMD or clears it
with `ENTRYPOINT []`. `--entrypoint` resets the image CMD, which is fine — the run command is
passed explicitly after the image and forwarded to the entrypoint. A task Dockerfile must
start `FROM coder-eval-agent:<version>` so the runtime is present; the built image is
asserted to carry the `org.coder-eval.version` label, because a bare `FROM ubuntu` builds
fine and then dies at `docker run` with a cryptic `exec: ".../coder_eval_entrypoint.sh": no
such file`.

### Extra mounts and reserved destinations

An `extra_mounts` entry is `src:dst[:mode]`, and both halves are task-authored strings. A
leading Windows drive letter is split off first so the colon in `C:\foo` is not misread as
the separator (the container side is always POSIX, so only the source can carry one); a bare
`C:` is deliberately not matched, being malformed. After variable expansion the spec is
re-checked for a `:`, because a variable whose value carries one would add fields to the
rebuilt spec and silently move the destination or widen the mode. The mode defaults to `ro`
when omitted: mounting host paths RW by default is the wrong sandbox stance, and the few RW
cases are better stated than implied by silence.

Destinations that would shadow framework-owned mounts are rejected, in expanded form,
against the same reserved set the workspace-dir validator uses. `/work/…` substrings are
caught too — `/work/foo` lands underneath the staging dir and shadows the input/output tree.

Auto-mounted sources that look like credential directories get a loud warning rather than a
refusal. Task YAMLs typically come from in-house suite authors, but `plugin.path`,
`reference.directory` and `template_sources` are user-controlled strings and a typo, or a
hostile suite, can silently expose `~/.ssh`; legitimate uses exist (a task that really does
want `~/.aws/config`), so the warning surfaces the surprise instead of blocking it.

The container gets a stable, UNIQUE name so cancellation can target it. PID alone collides
under `--max-parallel > 1`, so a uuid suffix and the replicate index disambiguate. Task ids
are sanitized and truncated to 80 characters — dataset row ids like `suite/row` break docker
name validation, and an earlier 30-character cap collided visibly on long shared prefixes.
The same sanitizing applies to `mkdtemp` prefixes (the `/` would need a parent dir that does
not exist) and to the deterministic build tag, which must be lowercase.

### The heartbeat watchdog is armed only inside a container

The host touches a heartbeat file in the output dir every couple of seconds while alive, and
the in-container watchdog exits if it goes stale. It is the only defence against the host
being SIGKILL'd — Claude Code's Escape, say — before the asyncio cleanup that would
`docker kill` the container, which would otherwise keep burning LLM budget orphaned.

The thread's whole authority is `os._exit(137)` on the process it runs in, and the only
process that may be reaped that way is the container's disposable main. Outside a container
it can do nothing but harm, and it did: a test invoked the command in-process (legitimately
— the command must refuse a malformed `context.json`, and proving that means calling it) and
the pytest worker inherited the thread, which found no heartbeat and 40s later exited the
worker mid-way through an unrelated test file. It named a different test on each run and on
each platform, carried no traceback, and took that worker's coverage data with it — so the
gate reported "65.13 < 80.00", naming neither the test nor the cause. It is therefore gated
on `CODER_EVAL_IN_CONTAINER`, not on `driver`, for the same reason the permission window is.

`os._exit` skips atexit and IO flushing, so the error line explaining the suicide would
routinely be lost, making a genuine stale-heartbeat exit indistinguishable from an external
SIGKILL in the archived logs. Flush best-effort first; never let a flush failure stop the
exit.

### The container contract

`context.json` is the host→container boundary, and it is parsed as one `ContainerContext`
rather than read key by key. `json.loads` returns `Any`, so pyright accepts `variant_id: str
= context["variant_id"]` for a value that may be anything at all — the annotation reads like
a guarantee and enforces nothing, and a `"replicate_index": "00"` reached
`build_task_run_dir` typed as `int`. `grade` and `regrade` are `StrictBool` because lax
coercion is itself the defect: a hand-edited `"grade": "false"` is a truthy string that
silently grades a run that asked not to be graded, and the same mistake on `regrade` re-RUNS
the agent against a workspace the operator asked only to grade, destroying the trajectory
being graded. `replicate_index` is `StrictInt` because a bool is an int, and `True` files the
row under `01/`.

Every field is required and unknown top-level keys are refused. A default on any key is reachable only
when host and image DISAGREE — `grade: True` for a host that predates `execute`,
`host_task_file: None` for one that predates the record seam — so a default does not
preserve behaviour, it hides a skew. With `extra="forbid"` and no defaults, that disagreement
is a parse failure naming the field, in both directions: an older host omits a key, a newer
host sends a key the image does not know. `host_task_file` and `workspace_dir` are required
keys with nullable values, because `null` is a real answer (no task file; the standard
workspace) and absence is not. `tests/test_container_context.py` derives its checks from
`model_fields`, so a field added with a default fails there.

No explicit contract-version field exists. It would answer the question the image's
`org.coder-eval.version` label and this parse already answer, with no bump policy: an image
older than the field ignores it, and one newer always agrees.

The host always serialises the POST-override `TaskDefinition` into the staged `task.yaml`,
never `source_yaml`, because the raw on-disk text predates `--model` and `-D` mutations the
container must see. `source_yaml` is forwarded separately so `task.json`'s audit trail
matches the in-process driver's.

## Trusting what the container sends back

### The stdout line limit

asyncio's `StreamReader` caps a single line at 64 KiB by default. The container streams
events as one NDJSON line each, and a single event carrying a large tool input — an agent
writing a whole `.flow` file, say — serialises well past that. The default-limit reader then
raised `ValueError` mid-stream, which tore the container down before it wrote `task.json`:
the entire task was lost and the host recorded a bare ERROR with no per-task report. The
limit is raised to 64 MiB, mirroring the orchestrator's own guard on post-run subprocesses,
and the read loop is an explicit `readline` (not `async for`) so a single over-limit line
degrades to a dropped line instead of a teardown. `readline` drains the offending bytes and
resyncs at the next newline; the dropped line is a host-side render event or a log line, and
`task.json` crosses via the bind mount rather than stdout, so the result is unaffected.

Each line is then a three-way split: a wire-prefixed line that parses goes to the host
callback and is NOT echoed to `docker.log` (the callback is the canonical destination); a
prefixed line that parses badly is a wire bug and is preserved raw so it is not lost; an
unprefixed line is an ordinary log line.

### A container that produced no task.json

The container can die before its orchestrator's `finally` writes the file — torn down after
a host-side stream failure, or killed externally. A synthetic ERROR `task.json` is persisted
so the row stays visible on dashboards and timelines instead of vanishing; the batch layer's
in-memory skeleton never reaches the per-task directory. A file that is present but
unparseable (schema skew from a stale image, a truncated write) degrades the same way rather
than crashing with an uncaught `ValidationError`.

That synthetic record goes through `write_text_atomic` like every other writer of the file.
The hand-rolled tmp+replace it replaced used `Path.write_text`, which FOLLOWS symlinks — so a
pre-planted `task.json.synthetic.tmp` in a run directory (a shareable artifact, bind-mounted
writable into the agent's own container) redirected this harness-privileged write to any path
the grading user could reach. It also falsified the helper's "one writer, so the crash
semantics cannot differ" claim, which is the property future readers rely on.

The build runs before `run_dir/docker.log` and `task.json` exist, so a build failure would
otherwise leave an empty result dir with no trace: the build log is persisted to `docker.log`
and a synthetic BUILD_FAILED record written, then the error re-raised for the batch
dispatcher to record run-level.

Cancellation is handled in a `finally` because `docker run --rm` does NOT propagate a kill
daemon-side: without it, Ctrl-C on the host leaves the container running and burning budget.
Suppression is narrowed to `CancelledError` throughout, so a genuine `KeyboardInterrupt` or
`SystemExit` from a parallel sibling still propagates. A non-zero `docker kill` usually means
the container was already gone (a race with `--rm`) or the daemon refused, so stderr is
surfaced to keep the ambiguity debuggable.

### The two honored-request guards

`grade` and `regrade` cross the boundary only through `context.json`, and an image that
predates either key ignores it and falls through to its old behaviour. The image-version
preflight only warns, so version skew would change what a command MEANS.

For `grade`: a stale image grades anyway, so `execute --driver docker` would silently
produce SUCCESS/FAILURE rows indistinguishable from a normal graded run. For `regrade`: a
stale image ignores the staged `prior.json` and the workspace mount and falls through to the
ordinary orchestrator branch, which **starts an agent** from `initial_prompt` — so the host
would fold a fabricated trajectory back over the recorded row as its "grade", publishing a
verdict for work it never looked at and billing the model for it. Nothing else catches that:
`_assert_grade_honored` early-returns because a grading container is dispatched with
`grade=True`.

Both are keyed on EVIDENCE, not on the label. For `grade`, "did it grade" is
`success_criteria_results` or a non-None `weighted_score`: exempting every execution-fact
status let a stale image return a fully graded MAX_TURNS_EXHAUSTED row — criteria vector,
weighted score and all — unchallenged, because that exemption exists for statuses a *fresh*
image also produces, and a fresh one produces them with neither. For `regrade`, a container
that honored the request seeds from `prior` and never runs the agent, so a DIFFERENT
`started_at` is the tell: `_seed_from_prior_result` restores the agent run's `started_at`
verbatim, so a fresh run is the only way that field can move.

The refusal quarantines the on-disk record before raising. Refusing in memory only left the
graded `task.json` sitting in the bind-mounted host run dir, where a later `execute --resume`
read it back as a completed row (its category is `succeeded`, so the resume partition files
it under prior results) and plain `aggregate` folded it straight into `run.json` — publishing
exactly the row the guard declined to publish. Refusing in memory while leaving contradictory
bytes on disk is not a refusal.

## The sandbox the criteria run in

### Why the venv gets system site packages

`--system-site-packages` is load-bearing. The sandbox venv goes on the criterion PATH, which
governs every `run_command` criterion plus `pre_run`/`post_run`, so inside a task image that
provisions packages globally an ISOLATED venv shadowed the interpreter while providing
nothing: `python` resolved to the empty venv and could not import them, while `pip` — which
`uv venv` does not place in the venv at all — fell through to the image's global pip and
reported them present. Measured in a task image: `import langchain` raised
`ModuleNotFoundError` while `pip list` showed `langchain 1.3.14`. An agent verifying its own
work chased that contradiction for ten turns and ran out of budget.

The venv is NOT on the agent's own PATH — the orchestrator prepends only the resolved mock
dirs there — so the contradiction is a property of criterion and pre/post-run subprocesses.
System site packages fixes it in the direction that keeps both halves: the image's globals
stay importable, `python` and `pip` agree, and installs still land in the venv (`sys.prefix`
remains the sandbox), so a task's `env_packages` cannot leak into the image. The `uv venv`
and stdlib `venv` paths do not produce the same artifact — the latter seeds pip — so which
shape this host got is logged rather than left to be inferred.

`adopt` DISCOVERS an existing venv instead of creating one, so criteria get the same
`VIRTUAL_ENV`/PATH the agent had, and it is gated on `config.python` for the same reason
`setup` is: discovering a venv a task never asked for grades it under a PATH it never ran
under, and would let an agent shadow binaries by writing `.venv/bin/` into its own workspace.

The unit test in `tests/test_sandbox.py` reads `pyvenv.cfg`. That proves the flag is set,
not that the result is correct. Every task image installs packages globally: the
framework image uses `uv pip install --system`, and skillsbench task images use
`RUN pip install ...`. `tests/test_sandbox_venv_live.py` checks the result in the
`coder-eval-agent` base image. It mounts this checkout's `src/` over the image's copy, so
the test runs the code under test and not the version the image was built with. Measured
on that test's own scenario, `python -c "import pydantic"` exits 1 with an isolated venv
and exits 0 with `--system-site-packages`. `pydantic` is a coder_eval runtime dependency,
so the base image already has it globally, and the check needs no build and no network.

### The criterion environment, layer by layer

Each layer is independent — none breaks if another is absent.

1. Inherit the parent environment, so agent tools and credentials remain reachable.
2. If the orchestrator captured the agent's SDK PATH, **prepend** it ahead of the host PATH
   rather than replacing it: the agent's PATH only needs to win the lookup race for its
   bundled toolchain, and system binaries must stay reachable to criteria. Prepending also
   stays symmetric with the venv and node_bin prepends below.
3. Activate the sandbox virtualenv, first-hit-wins. If the agent's PATH already contains the
   venv scripts dir — likely, since it inherits this process's environment — the prepend
   duplicates the entry, which is harmless everywhere and left explicit so the order does not
   depend on what the agent SDK injects.
4. Prepend `<sandbox>/node_modules/.bin`.
5. Pin `NODE_PATH=""` so Node's fallback search cannot pick up contaminated parent-dir
   installs. This does NOT disable parent-walking from cwd — that is hard-wired in Node — but
   it eliminates `NODE_PATH`-mediated leaks.
6. Pin `NPM_CONFIG_PREFIX` to a sandbox-scoped directory, so an `npm install` from inside the
   sandbox writes into the sandbox rather than `$HOME/node_modules`, where concurrent
   sandboxes would shadow each other.
7. Expose `TASK_DIR` for criterion scripts.
8. Expose `REFERENCE_DIR`, the per-run staged copy, when the task declares a reference.
   Safe here because `run_command` criteria execute AFTER the agent's turn, outside the
   mode-000 window; absent for a task with no `reference:` block.

The parent `node_modules` check is detection-only. Concurrent tasks, or anything else running
`npm install --save` in a shared parent, drop packages where Node's parent-walking resolver
finds them before the sandbox-local install. The failure mode is generic to Node module
resolution rather than specific to one npm scope, so the check stays scope-agnostic —
`coder_eval` is a generic framework and should not single out one ecosystem's namespace — and
auto-remediation is avoided because those directories may legitimately belong to the user.

Generated `record_cli` recorders go on PATH FIRST, and refuse to generate a shim whose name a
user mock dir already provides, so the order can never silently shadow a task's own mock — it
only fixes which directory wins for names the harness owns. The clash check covers every name
the feature generates, not just the bare one: on Windows PATHEXT resolves `uip` to the
generated `uip.cmd` ahead of the task's own `mocks/uip.cmd`. The recorder dir is wiped rather
than reused, because DIRECT_WRITE does not clear the target dir and a reused `--run-dir` would
leave a previous run's log to be scored as this one's. The log file is seeded empty:
`cli_called` treats a MISSING log as a harness fault (score 0 even for a negative guard),
which is right when a mock never ran and wrong for a correct run that legitimately called
nothing.

### Criterion paths are contained, quietly

`Path('/tmp/sandbox') / '/etc/passwd'` is `/etc/passwd` — pathlib discards the prefix on an
absolute right operand — so criterion paths, the one consumer that skipped the containment
helper every other task-authored path goes through, were a pass-fail oracle over any file the
grading user could read, and `json_check` could surface parsed values in `details`. That was
defensible while a task YAML was operator-supplied; it stopped being so when
`evaluate <run_dir>` began rebuilding the criteria list from a shareable run directory.

The predicate returns False rather than raising: an out-of-sandbox path is indistinguishable
to the criterion from a file that is not there, which is the same answer the template and
mock-dir paths give, and raising would book a config error as an agent crash (CE039). It is
silent by design — the escape is reported ONCE per criterion, naming the pattern the task
author actually wrote, because logging at the predicate named a resolved absolute path
(uninformative: the author's own string joined onto a tempdir) once per rejected glob match,
so a wide pattern produced a burst of near-identical warnings.

An escaping path that names an EXISTING file is a different case and raises
`CheckerMisuseError`. Returning `[]` booked an eval-CONFIG error as an agent failure: the
criterion scored a gating 0.0 with "file does not exist" for a file that plainly does exist,
with only a WARNING in the task log. `tasks/byod_smoke_test.yaml` was broken exactly that way
— it checks `/opt/byod_marker`, baked into the BYOD image — and the suite reported a 0.0
nobody could explain from the score alone. No agent behaviour can ever satisfy such a path,
so it is not a verdict about the agent, which is precisely the distinction CE039 enforces. A
merely-absent absolute path still resolves to "no match", an ordinary failing verdict, and
the GLOB branch still warns and drops rather than raising, since filtering some matches out of
a search is its normal behaviour.

Resolution prefers the literal: a path naming an existing file resolves to itself **even when
it contains a glob metacharacter**, so a real `report[2024].json` is graded as itself rather
than as a character class that would silently match `report2.json`. Only when the literal does
not exist is it expanded, so a criterion can address a file whose exact location the prompt
does not pin. Glob matches are filtered through the sandbox's ignore patterns, because the
sandbox root holds harness-created content the agent never authored and grading off it is
neither fair nor deterministic; only segments the glob *discovered* are filtered, so a segment
the pattern names literally (`dist/**/*.js`) is an explicit opt-in and survives. Matches are
sorted for determinism and directories dropped so a glob cannot resolve to something
unreadable. The ambiguity error enumerates a bounded number of matches, because the message is
persisted to `task.json` and injected into judge prompts, where an unbounded listing over a
wide pattern is a real payload.

### preserve_to, capture_to, and the capture denylist

`mkdtemp` creates the sandbox root at 0700. Under `driver: docker` the container runs as root,
so the preserved tree lands on the host bind-mount owned by root with that 0700 top dir — the
host user, a different uid, then cannot traverse it, so the blob upload and any `ls` see an
empty dir and silently skip the artifacts. Both paths therefore grant `a+rX` on the tree the
host reads.

`preserve_to` MOVES and repoints `sandbox_dir` so a later `cleanup()` is a no-op; absolute
paths inside the venv are not rewritten, matching the prior copy-based behaviour.
`capture_to` COPIES instead, because the sandbox there is the container's own WORKDIR (`/root`,
say), which is discarded with `--rm` and may contain the orchestrator's own cwd — so a copy is
safe and non-destructive. It does not repoint `sandbox_dir`: the workspace persists
in-container and is reaped with the container. `symlinks=True` plus `ignore_dangling_symlinks`
makes a dangling link a no-op rather than a failure, the exact breakage the old
`cp -a "$PWD/." "/root/"` reconciliation prelude hit.

Because the WORKDIR can BE `$HOME`, capture excludes two classes of entry. The SECURITY
denylist is credential stores that must never leak into artifacts that get uploaded — most
importantly `.claude`, the RW lean copy that carries `.credentials.json`, plus `.aws`, `.ssh`,
`.gnupg`, `.docker`, `.azure`, `.netrc` and `.gitconfig` (which can embed PATs via
`credential.helper`). It is defense-in-depth: the eval images bake no credentials, but a future
image that does should not silently expose them. The NOISE class is sandbox-created bulk and
home-dir infrastructure written by uv, pip, npm and the shell when WORKDIR overlaps HOME; those
are never task deliverables. Both match by basename at every level.

Cleanup on a failed `setup` removes ONLY a temp dir the sandbox created itself. A
caller-supplied `target_dir` (DIRECT_WRITE) may be a pre-existing artifacts dir whose contract
is never to be cleared, and `_cleanup_on_exit` already distinguishes the two.

### Materializing a template into the sandbox

Template copying matches ignore patterns against the template-RELATIVE path, because checking
the absolute path lets an ancestor directory named `dist`, `build`, `env`, `venv` or
`node_modules` filter out the entire template — a repo cloned under `~/build/…`, say. Symlinks
are handled before `is_dir()`/`is_file()`, which follow them: a `tools/node_modules/x -> ../y`
link would look like a directory and produce an empty dir at the destination, breaking npm
workspace resolution. At the destination, `is_symlink()` comes before `exists()` because
`exists()` follows the link and a *broken* symlink is still an overwrite to clear; only a real
directory needs `rmtree`. Link targets are preserved verbatim, relative and absolute alike —
absolute targets remain live links into the host filesystem, which is intended for trusted
template authors, not a defense boundary.

`git clone` is invoked with `--` before the URL: it is argv position 2, so without the
separator a value beginning with `-` is parsed as an OPTION rather than a repository
(`--upload-pack=…` runs a command of the caller's choosing). That URL is task-authored, and
since `evaluate <run_dir>` rebuilds the task from a shareable run directory it is no longer
necessarily the operator's own string.
