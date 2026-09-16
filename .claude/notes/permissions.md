# Permissions and the reference anti-cheat

> Conventions and authority order: see [README.md](README.md).

## Reference solutions and the anti-cheat window

`task.reference` is a single required `directory:`, relative to the task YAML. The inline
`code:` and single-file `file:` forms are gone because a directory is the only shape that
can be permission-gated as a UNIT; a `model_validator(mode="before")` gives the removed
forms a migration error.

The orchestrator stages a **per-run private copy** (`orchestration/evaluation.py::stage_reference_dir`,
symlinks stripped) into a tempdir and never preserves it into `run_dir/artifacts`. Cleanup
goes through `rmtree_restrictive`, keyed on a root recorded BEFORE the copy so a failed copy
still cleans up — `rmtree(ignore_errors=True)` silently declines on a tree left at 000.

That copy is held at mode `000` for the whole of every `agent.communicate` call, via
`Sandbox.set_permissions`. Whether the window is a real control at all, and why it keys on
`CODER_EVAL_IN_CONTAINER` rather than `sandbox.driver`, is
[isolation.md](isolation.md) § Capability drops and the anti-cheat window;
`resolve_reference_dir` gates its `/work/references` branch on the same var for the same
reason. How the window stacks is § The stacked chmod window below, and why an
unappliable window is a hard error is § strict=True and the hard-fail path.

The task directory is shielded ALONGSIDE the reference, which matters because it holds
grading material beyond it: `run_command` fixtures, expected outputs, and — for a task laid
out flat — every SIBLING task's reference. That is possible only because the task dir is a
read-write throwaway copy; when it was bind-mounted `:ro` the chmod returned EROFS. There is
no tmpfs mask and has not been one since: see
[isolation.md](isolation.md) § Why the framework mounts are writable copies.

What the window does NOT hide is the task DEFINITION. `task.yaml` is also staged at
`/work/input` for the in-container orchestrator and that mount is untouched, so hiding the
criteria from the agent remains a separate, unsolved problem.

Criteria address reference files with the `$REFERENCE_DIR` token (same resolver as
`$TASK_DIR`) and the `REFERENCE_DIR` env var for `run_command`; `reference_comparison` names
one file via `reference_file`.

**KNOWN GAP — defense-in-depth, not a boundary.** Two holes, both documented in
[docs/DOCKER_ISOLATION.md](../../docs/DOCKER_ISOLATION.md#architecture) rather than restated
here: a deliberate re-chmod by the root agent, and waiting the window out (it spans
`agent.communicate` only, and nothing reaps agent child processes at turn end).

The **write** half of the second is closed: `path_utils.digest_tree` hashes the tree at
staging and `Orchestrator._verify_reference_integrity` re-checks before grading, raising
`ReferenceTamperedError` on a mismatch — so an agent cannot overwrite the reference to drive
`reference_comparison` to 1.0. Passive reads are blocked; an adversarial agent is not. Full
containment needs the agent running as a non-root uid AND the window held for its whole
lifetime. `tasks/anti_cheat_reference` probes the passive-read half.

## The stacked chmod window

The agent under evaluation runs with the same filesystem view as the harness: in
`driver: tempdir` it is an ordinary process on the host, and in `driver: docker` the
orchestrator and the agent share one container. Any directory the harness can read, the
agent can read too — including the task directory and the reference solution. An agent
that greps for the reference does not solve the task, it copies the answer.

`set_permissions` closes that window for the duration of an `async with` block. The
orchestrator wraps every `agent.communicate` call in it, so the staged reference directory
is unreadable exactly while the agent is executing, and readable again by the time
criteria and judges run. The task directory is shielded by the same window: under docker
it is mounted as a throwaway COPY at a fixed container path, which is what makes it
chmod-able without touching the user's checked-out `tasks/` tree.

It shields grading MATERIAL that happens to live in the task directory (a `reference/`
subdirectory, fixtures), not the task DEFINITION: `task.yaml` is separately staged at
`/work/input`, which the agent can still read.

### Why a stack and not a refcount

Windows nest with *different* modes, so what an exit has to restore is the mode of the
enclosing window — not "the original", and not "is anyone still holding it". A refcount
cannot express that: it would see a nested re-grant as just another holder and silently
leave the outer mode in place. The stack also subsumes what a refcount did, for free —
two windows applying the same mode push two identical entries, and the inner pop
re-applies the outer's identical mode instead of restoring the pre-window one.

The inner, more permissive form exists for one intended consumer: **live success
criteria**. Early-stop verdicts are computed while the agent turn is still running — i.e.
inside the 000 window — so a live criterion that needs to consult the reference solution
has to be able to read it exactly then, while the agent still cannot. That is why
`READ_ONLY_MODE` is public. It is a designed seam, not speculative generality.

Only the REFERENCE is shielded, never the sandbox. A live criterion reading the agent's
own output files needs no window change at all — and should not get one. Reading the
static reference mid-turn cannot break the `LiveVerdict` monotonicity contract
(contracts.md § The live_verdict contract); reading the half-written sandbox can, and is
the "end-state peeking" `live_verdict` rules out.

## Why the chmod tests are Linux-only

`tests/test_reference_permissions.py` drives `chmod` against the HOST filesystem, and
Windows `chmod` honours only the read-only bit, so mode 000 never takes and every
assertion reads back 0o555/0o777.

This is NOT a coverage gap for Windows users. The window is enforced only when
`CODER_EVAL_IN_CONTAINER=1`, which only `DockerRunner` sets — and Docker Desktop on
Windows runs LINUX containers (WSL2), so the in-container orchestrator that performs the
chmod is on Linux and behaves exactly as these tests assert. A Windows host only ever sees
the window under `driver: tempdir`, where it is a deliberate no-op regardless of platform.
The real behaviour is covered by the Linux CI jobs and by `tasks/anti_cheat_reference`,
which runs in the container.

## Locking and crash safety

The registry is keyed by the *resolved* path so a directory reached by two different
relative routes is one entry, and guarded by a plain `threading` lock rather than an
`asyncio` one because the crash-safety handlers run outside the event loop and must be
able to take it. It is an `RLock`, not a `Lock`: `restore_all()` runs from a signal
handler, which can be delivered on the main thread while atexit's `restore_all()` is
already mid-flight, and a non-reentrant lock deadlocks the interpreter at exit — exactly
when restoring matters most.

`pop` chmods INSIDE the lock. Releasing first would let a concurrent `push()` observe the
path still at the restricted mode and record THAT as its `original` — so its own pop would
then leave the path at 000 permanently, the exact failure this module exists to prevent.
A failed restore keeps the entry rather than dropping it: `restore_all()` is the last
chance to put the path back, and it can only do that while it still holds the pre-window
mode.

Crash handlers are installed from the event-loop (main) thread, before the chmods are
offloaded. `signal.signal` raises `ValueError` anywhere else, so installing from inside
the `to_thread` worker — as an earlier revision did — silently failed and left SIGTERM
with no restore at all. The flag latches only when the signal handlers really went in, so
a call from a worker thread is retried later rather than latching a no-op as done.

Installation is deliberately not done at import time: `sandbox.py` imports this module, so
an import-time install would rewrite SIGINT/SIGTERM disposition for every process that
merely imports `coder_eval` — including library embedders and host runs, where no window
is ever opened.

The signal handler chains to the previous disposition. An operator's Ctrl-C must not be
swallowed, and SIGTERM must still terminate. `SIG_IGN` is the one disposition that is
neither callable nor `SIG_DFL` — it means "the process chose to ignore this", so restoring
and returning is the correct chain. `None` means a handler installed from C and not
retrievable from Python; treating it as `SIG_DFL` restores default termination.

The push sits INSIDE the `try`, so the `finally` always runs, and it is shielded: a
cancellation landing on the await (task-timeout watchdog, sibling batch failure) still
raises `CancelledError` while the worker thread goes on to complete every chmod. With the
push above the `try` — as an earlier revision had it — that left the paths at mode 000
with no matching pop: unreadable for the rest of the run, and a stale registry entry that
poisoned the next window on the same path.

## strict=True and the hard-fail path

Under `strict`, a chmod refusal on an existing path raises `PermissionWindowError` instead
of warning. An unprotected run that reports a normal pass/fail is worse than no run:
nothing downstream can tell it apart from a protected one. `Sandbox.set_permissions` sets
it whenever the window is actually enforced (in-container).

A missing path is the common, benign case (the task has no reference) and only debug-logs.
A genuine refusal — read-only mount, foreign owner — warns even when not strict, because
the operator should know the run is not protected.

`_push_all` under `strict` leaves the paths pushed before the failure applied: the context
manager's `finally` cannot see a return value that never came. That is deliberate —
`restore_all` (atexit / signal) still holds their pre-window modes, and unwinding there
would swallow the failure that must abort the run.

Pre-window modes are captured rather than hardcoded, so a repo that ships `0o750` task
dirs stays `0o750` on restore.
