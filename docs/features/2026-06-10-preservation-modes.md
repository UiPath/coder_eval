# Preservation modes (`--preservation-mode`)

**Date:** 2026-06-10

## Summary

Replaces the boolean `--preserve / --no-preserve` flag on `coder-eval run` with an
explicit tri-state enum, `PreservationMode`:

| Mode | Where the sandbox runs | At end of run |
|------|------------------------|---------------|
| `NONE` | `/tmp` tempdir | deleted (no artifacts kept) |
| `MOVE_ON_WRITE` | `/tmp` tempdir | `shutil.move` into `run_dir/artifacts/<task_id>` |
| `DIRECT_WRITE` | directly in `run_dir/artifacts/<task_id>` | nothing — already in place |

## Driver-derived default

When `--preservation-mode` is not passed, the mode is resolved per-task from the
sandbox driver by `resolve_preservation_mode(explicit, driver)`
(`orchestration/config.py`):

- `docker` → `DIRECT_WRITE`
- everything else (`tempdir`) → `MOVE_ON_WRITE`

An explicit `--preservation-mode` always wins over the driver default.

### Why the split

`MOVE_ON_WRITE` is the safe host default. Running the sandbox directly under
`run_dir` on a shared host lets parent-dir `node_modules` contaminate Node tool
resolution — this is the regression that PR #257 (MST-9795) fixed by moving the
runtime sandbox into `/tmp`. Keeping the host default as `MOVE_ON_WRITE`
preserves that protection.

`DIRECT_WRITE` is the docker default because the container is isolated (no stray
ancestor `node_modules`), and writing straight to the bind-mounted artifacts dir
avoids a cross-mount copy: inside the container `/tmp` and `/work/output` are
different filesystems, so `MOVE_ON_WRITE` degrades to copy+remove. `DIRECT_WRITE`
writes once. This revives the pre-#257 direct-write behavior, now scoped to the
isolated docker path where it is safe.

## Resolution happens on the host

The default must be resolved at the batch dispatch seam (`batch.run_single`),
where the *original* driver is still visible. The in-container orchestrator sees
the driver forced to `tempdir` (docker tasks are re-run with `driver=tempdir`
inside the container), so it cannot re-derive the docker default. The host
resolves the mode and forwards it to the container via `context.json`; the
container obeys it verbatim.

## DIRECT_WRITE does not clear the target (except on resume re-run)

`DIRECT_WRITE` uses `mkdir(parents=True, exist_ok=True)` and writes in place; it
deliberately does **not** clear a pre-existing `run_dir/artifacts/<task_id>`
during a normal run (unlike `MOVE_ON_WRITE`, whose `preserve_to` rmtrees the
destination first). The orchestrator logs a warning when the target dir already
exists and is non-empty so the situation is visible at runtime. A setup failure
does not wipe a caller-supplied target either — only a self-created tempdir is
removed on failure.

**Exception — `--resume`:** a resumed run re-executes every non-finalized task
from scratch, so any leftover artifacts (a container killed mid-run leaves
partial DIRECT_WRITE files in the bind-mounted artifacts dir) are stale and must
not survive into the re-run — otherwise a leftover file could satisfy a
file-based criterion the resumed agent never produced, changing the score for
the same output. So on `--resume`, `clear_rerun_artifacts` removes
`run_dir/artifacts/<task_id>` for each re-run task before dispatch. This is
host-side (covers both the docker bind-mount and the tempdir path) and a no-op
for `MOVE_ON_WRITE`/`NONE`, which never create that dir before finalize.

## Permissions

`DIRECT_WRITE` calls `Sandbox.grant_read_access()` (`chmod a+rX`) at finalize,
because the docker container runs as root and writes into the host bind-mount;
without it the host user (a different uid) can't traverse the 0700 tree.
`MOVE_ON_WRITE` already applied the same widening via `preserve_to`.

## Backward compatibility

Per the repo's greenfield / no-backward-compatibility rule, the old
`--preserve / --no-preserve` flags (and their `-p` / `-P` short forms) are
**removed** from `coder-eval run` — there is no alias. Use `--preservation-mode`
(default = on, driver-derived) or `--preservation-mode NONE` to discard.
`coder-eval evaluate` keeps its boolean `--preserve / --no-preserve` (it is
always in-process / host, never docker) and maps it to `MOVE_ON_WRITE` / `NONE`.

**Resume:** the run fingerprint key changed from `preserve_sandbox` (bool) to
`preservation_mode`. `--resume` against a pre-this-change run compares only
overlapping fingerprint keys, so the rename surfaces no drift and the resumed
run silently adopts the new driver-derived default. This matches the existing
warn-don't-refuse resume design.
