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
