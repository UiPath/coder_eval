# Persistence

> Conventions and authority order: see [README.md](README.md).

## write_text_atomic

A plain `write_text` truncates first, so a SIGKILL or a full disk mid-write leaves a
half-file. For `task.json` that is worse than no file: a truncated record parses as
*malformed*, which the recovery paths treat as "not complete" — so a later `--resume`
re-executes the task and pays for the agent again, and the row vanishes from `run.json`.
One writer, so the orchestrator and the detached grade's write-back cannot have different
crash semantics for the same file.

`O_NOFOLLOW` is not tidiness: without it, a pre-planted `task.json.tmp` *symlink* in a
shared run directory makes this an arbitrary-file-overwrite primitive — and one that
bypasses the destination symlink refusal in `evaluate`'s write-back, since that guard
checks the destination while the truncation happens through the temp name.

### Why the temp name must be unique rather than fixed

`os.replace` is the only step that can be interrupted without trace, and this function
exists precisely because the process may be SIGKILLed (the docker host-heartbeat watchdog
does exactly that) — so a crash between `open` and `replace` WILL sometimes leave the temp
file behind. Under a fixed name, `O_EXCL` then turned that leftover into a permanent
refusal to write the record at all: the row reported ERROR, `--resume` saw no `task.json`,
re-ran the task into the same run dir, and hit the same stale file — an unbounded loop
that re-pays for the agent every time. A unique name (pid + random) keeps `O_EXCL`'s
guarantee while making a leftover inert. It can litter a dead `.tmp` beside the record
after a hard kill; that is strictly better than wedging finalization, and the litter is
recognisable by its embedded pid.

### Why the mode is 0o644

The same mode a plain `write_text` produced, and the widest one that is never group- or
world-*writable* whatever the umask. Creating it 0600 broke the docker driver on Linux:
the in-container orchestrator writes `task.json` as root straight into the bind-mounted
host run dir, and the host then reads it back as the invoking uid — an unguarded read that
raises `PermissionError` for every task. A result record is not a secret, and the symlink
hazard is closed by `O_NOFOLLOW` and the unpredictable name rather than by the mode.

## rmtree_restrictive

Plain `rmtree(..., ignore_errors=True)` silently declines on a tree left at mode 000 by a
killed run: `scandir` on a 000 directory raises `PermissionError`, the `rmdir`s then fail
with ENOTEMPTY, and every one of those is swallowed — leaving an orphaned tempdir holding
the reference solution, with no log line.

An `onexc` handler cannot fix it either: the failing call is the directory
`open`/`scandir` that drives the walk, which the handler has no way to resume. So
traversal is restored on the way DOWN first, then the tree is deleted.

## Run-directory filename constants

The per-task filenames are module-level constants rather than literals because ~12 sites
name them — including three that `rglob` for the first — and two half-copies of the same
string in different packages is how a rename becomes a silent no-op on the sites it
missed. `REFERENCE_COPY_IGNORE` is shared for the same reason: the host-side docker mount
and the per-run staged copy are the SAME operation on two mutually exclusive driver paths,
so a literal at each site would make `$REFERENCE_DIR` contents driver-dependent the moment
one of them grew an entry.

`prior.json` is never written by a run: it is only ever an input to
`coder-eval evaluate` / `run --resume` over a `driver: docker` row, staged into the
grading container's read-only input mount.

A regrade writes to `grade.log`, not `task.log`, because the log handler opens its file
`mode="w"` — pointing a detached or resumed grade at `task.log` truncated the agent
trajectory log the run had already paid for. `grade.docker.log` exists for the same reason
one layer down: on the `run --resume` path `docker.log` is already the executed
container's log.

## The run layout as two directory templates

`logging_dir_template` and `artifacts_dir_template` (`BatchRunConfig`, resolved by
`path_utils.resolve_dir_template`) describe where a task's bookkeeping and its artifacts
go. They are resolved LATE — per task, which is the only point where `${variant}`,
`${task}` and `${repeat}` exist at all — and they are INDEPENDENT: artifacts nesting under
the logging dir is a default, not a law.

That independence is the point. Harbor puts logs in its own agent logs dir and artifacts at
the container's WORKDIR, two unrelated parts of the filesystem, and because artifacts were
never a child of the logging dir there is nothing to copy and nothing lands twice. The
previous design reached the same end state with a `--workspace-dir`/`--artifacts-dir` pair
that had to be passed the SAME path so an equality check could infer "don't copy"; a
coincidence standing in for an intention.

An override needs no special-casing anywhere, because substituting a template that contains
no placeholders is the identity function: `/work/output` in, `/work/output` out, down the
same code path as the default. The defaults spell out the historical layout, so an
unspecified run writes byte-identical paths — asserted in `test_path_utils.py` against the
literal old layout rather than against `build_task_run_dir`, which now calls the resolver
and would make the test vacuous.

Two implementation constraints, both load-bearing:

- **`string.Template`, never `re.sub`.** `re.sub` interprets backslashes in the
  REPLACEMENT, so a Windows `run_dir` of `C:\runs\2026` comes out mangled.
- **`${task}` may contain a separator.** A dataset-expanded `task_id` is
  `"<task>/<row>"` (`expand_dataset`, whose row ids are validated precisely because they
  become directories), so it nests — on Windows too, where `pathlib` splits on both
  separators.

`${task}` appears twice in the artifacts default (`.../${task}/${repeat}/artifacts/${task}`)
because that IS the layout on disk: the per-task run dir carries it, and
`preserve_to`/`capture_to`/DIRECT_WRITE each appended it again. Kept for compatibility, and
now one string to change rather than five call sites. The `*_as` variants
(`preserve_as`/`capture_as`) exist so a caller-supplied artifacts dir is used as the FINAL
path instead of having `task_id` appended to it a second time; `preserve_to`/`capture_to`
remain as the parent-relative wrappers.

### What run_dir still owns

`${run_dir}` is only a placeholder VALUE. But run-LEVEL files — `run.json`, `run.md`,
`experiment.*`, `resume_fingerprint.json` — still follow `--run-dir`, and its default is
CWD-RELATIVE (`runs/<timestamp>`, `config.py`). Omitting `--run-dir` therefore does not
leave those files harmlessly uncollected; when cwd is the agent's WORKDIR it writes them
INSIDE the workspace, polluting the very directory `artifacts_dir_template` names.
Confirmed live: `artifacts/work/runs/<timestamp>/run.json` in a collected Harbor trial.
Harbor consequently passes `--run-dir /tmp/coder-eval-run`, a throwaway path outside the
workspace.

Anything that used to DISCOVER per-task files by walking `run_dir` had to be given the
resolved logging dirs instead, because they need not live under `run_dir` at all and the
walk silently finds nothing: `atif_emit.emit_trajectories_for_run` (which would emit no
trajectory, leaving Harbor's token/cost totals empty) and
`logging_config.aggregate_task_logs` (which would write an empty `experiment.log`).

### A static template is single-task only

A placeholder-free template is the identity function, so every task in a multi-task
`run`/`execute` would resolve `--logging-dir`/`--artifacts-dir` to the SAME directory and
overwrite each other's `task.json`/artifacts. `run_batch` refuses this loud
(`ValueError`) when `len(resolved_tasks) > 1` and either template has no `${...}`
placeholders (`path_utils.dir_template_is_static`), mirroring the pre-existing
`--workspace-dir` + multi-task rejection. Static paths exist for exactly the single-task
case below.

### A flat run_dir needs no special case in run_batch

Harbor's single-task, static-template mode writes `task.json`/`task.html`/`task.log`/
artifacts flat at the top-level `run_dir` instead of the usual
`<variant>/<task_id>/<NN>` nesting -- the multi-task guard above already guarantees
exactly one resolved task here, so that nesting only exists to disambiguate siblings that
can't occur. A flat `run_dir` also means `trajectory.json` (`emit_trajectories_for_run`'s
sibling write) lands at a fixed, predictable path instead of requiring a recursive glob.
`run_batch`'s `run_single` needs no `workspace_dir` special case for this: `rt.run_dir` IS
the resolved `logging_dir_template`, so "flat" is simply what a static template resolves
to, not a mode this seam has to detect.

### CoderEvalAgent passes both templates as static paths

`harbor/agent.py`'s `CoderEvalAgent.run()` passes `--logging-dir`/`--artifacts-dir` as two
STATIC paths (Harbor's own agent logs dir, and the container's WORKDIR via `$(pwd)`) rather
than templates with placeholders — the case the identity-function property above exists for.
Because artifacts are no longer a child of the logging dir, nothing lands twice; because the
artifacts destination IS the workspace, `capture_as`'s self-referential guard makes the copy
a no-op. `--run-dir` still points at `_THROWAWAY_RUN_DIR` (`/tmp/coder-eval-run`), never
substituted into either template, for the reason in "What run_dir still owns" above.

## Judge persistence

A judge transcript — tool calls, raw verdict, rendered prompt, system prompt — runs 10-100
KB. Inlining it into every `task.json` inflates the row record for the consumers that never
need it (suite rollups, report renderers), so it spills to a sibling file and the row keeps
only a path. The inline value is left in place in memory so the orchestrator's own HTML
render still sees it; the JSON dump excludes it.

The sibling is YAML rather than JSON because the transcript carries multi-line text, which
YAML's literal block scalar renders as readable paragraphs instead of one line full of `\n`
escapes. Its consumers are humans. The reader accepts `.json` too, so previously-spilled runs
keep rendering, and a row with no path at all is a no-op, so old inline records keep working.

FILE ORDER IS LOAD-BEARING. Each filename is keyed off the criterion's position in its result
list, and the reader resolves the stored path, so each list must retain its order through
persistence. Fields inside the file lead with the human-readable summary and put the bulkiest
last.

### transcript_path is untrusted input

`task.json` travels across trust boundaries — CI artifacts, shared eval bundles — so a path
read back out of one is attacker-controlled. The writer only ever emits a generated
basename, so the reader ALLOWLISTS that shape directly rather than joining first and hoping
`is_relative_to` catches the result: `/etc/passwd` and `../../secrets` are refused at the
door.

The shape is checked under BOTH POSIX and Windows path semantics. `subdir\judge-0.yaml`
passes a POSIX check on Linux, where a backslash is an ordinary character, and resolves to a
nested file on Windows; rejecting under either interpretation enforces the policy regardless
of which platform the record travels to next. Windows reserved device basenames are rejected
for the same reason: `CON.yaml`, `NUL` and `COM1` open the console, the null device or a
serial port wherever they sit in the tree, the extension is ignored by Win32, and the check
runs platform-independently so a record minted on Linux is refused before it travels.
Containment is then re-verified after resolution, because a symlink inside the directory
could still redirect outside it.

A scalar, list or `None` payload is rejected early: it would land on the result and crash the
renderer with an `AttributeError` on its first `.get()`. The typed model is preferred so
isinstance checks see the same shape they get during the original run, with a fallback to the
raw dict so an older sibling or a forward-compatible key does not break re-render. The
assignment bypasses pydantic's setter, since a loaded subclass's config might validate or
reject it, and the renderer accepts both shapes.
