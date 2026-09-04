"""Path utilities for run directory management."""

import contextlib
import hashlib
import logging
import os
import platform
import secrets
import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path


logger = logging.getLogger(__name__)

TASK_LOG_FILENAME = "task.log"

# The per-task result record, and the pre-grade snapshot a detached grade keeps
# beside it. Module-level because ~12 sites name them — including three that
# `rglob` for the first — and two half-copies of the same string in different
# packages is how a rename becomes a silent no-op on the sites it missed.
TASK_JSON_FILENAME = "task.json"
PRE_GRADE_JSON_FILENAME = "task.execute.json"

# The virtualenv directory `setup` creates and `adopt` discovers. Named because
# whether it is on PATH decides which binaries a criterion resolves.
VENV_DIRNAME = ".venv"

# Ignore list for every copy of a reference solution tree. A module-level
# constant, not an inline literal at each call site: the host-side docker mount
# (`DockerRunner._prepare_reference_mount`) and the per-run staged copy
# (`orchestration.evaluation.stage_reference_dir`) are the SAME operation on two
# mutually exclusive driver paths, so a literal at each site would make
# ``$REFERENCE_DIR`` contents driver-dependent the moment one of them grew an
# entry.
REFERENCE_COPY_IGNORE = [".git"]


def write_text_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file + ``os.replace``.

    A plain ``write_text`` truncates first, so a SIGKILL or a full disk mid-write
    leaves a half-file. For ``task.json`` that is worse than no file: a truncated
    record parses as *malformed*, which the recovery paths treat as "not
    complete" — so a later ``--resume`` re-executes the task and pays for the
    agent again, and the row vanishes from ``run.json``. One writer, so the
    orchestrator and the detached grade's write-back cannot have different crash
    semantics for the same file.

    The temp file is opened ``O_CREAT | O_EXCL | O_NOFOLLOW`` under a name that
    is UNIQUE per call. Without ``O_NOFOLLOW`` a pre-planted ``task.json.tmp``
    *symlink* in a shared run directory makes this an arbitrary-file-overwrite
    primitive — and one that bypasses the destination symlink refusal in
    ``evaluate``'s write-back, since the guard checks the destination while the
    truncation happens through the temp name.

    The name must be unique, not fixed, and that is a correctness requirement
    rather than tidiness. ``os.replace`` is the only step that can be interrupted
    without trace, and this function exists precisely because the process may be
    SIGKILLed (the docker host-heartbeat watchdog does exactly that) — so a
    crash between ``open`` and ``replace`` WILL sometimes leave the temp file
    behind. Under a fixed name, ``O_EXCL`` then turned that leftover into a
    permanent refusal to write the record at all: the row reported ERROR, and
    ``--resume`` saw no ``task.json``, re-ran the task into the same run dir, and
    hit the same stale file — an unbounded loop that re-pays for the agent every
    time. A unique name keeps ``O_EXCL``'s guarantee while making a leftover
    inert. It can litter a dead ``.tmp`` beside the record after a hard kill;
    that is strictly better than wedging finalization, and the litter is
    recognisable by its embedded pid.

    Mode is ``0o666`` so the umask applies, giving the same 0644 a plain
    ``write_text`` produced. Creating it 0600 broke the docker driver on Linux:
    the in-container orchestrator writes ``task.json`` as root straight into the
    bind-mounted host run dir, and the host then reads it back as the invoking
    uid — an unguarded read that raises ``PermissionError`` for every task. A
    result record is not a secret, and the symlink hazard is closed by
    ``O_NOFOLLOW`` and the unpredictable name rather than by the mode.
    """
    # pid + random: unique across concurrent writers AND across a crashed
    # predecessor, so O_EXCL can never collide with our own leftovers.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def digest_tree(root: Path) -> str:
    """Content hash of every file under ``root``, stable across runs.

    Paths are hashed alongside contents (so a rename is a change) in sorted
    order (so ``os.walk`` ordering can't make the digest nondeterministic).
    Unreadable entries are folded in as a sentinel rather than skipped: a file
    that becomes unreadable between two calls IS a change worth catching.
    """
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError as e:
            digest.update(f"<unreadable: {e.errno}>".encode())
        digest.update(b"\0")
    return digest.hexdigest()


def rmtree_restrictive(root: Path) -> None:
    """``rmtree`` a tree that may have been left at mode 000 by a killed run.

    Plain ``rmtree(..., ignore_errors=True)`` silently declines here: ``scandir``
    on a 000 directory raises ``PermissionError``, the ``rmdir``s then fail with
    ENOTEMPTY, and every one of those is swallowed — leaving an orphaned tempdir
    holding the reference solution, with no log line.

    An ``onexc`` handler cannot fix it either: the failing call is the directory
    ``open``/``scandir`` that drives the walk, which the handler has no way to
    resume. So restore traversal on the way DOWN first, then delete.
    """
    for dirpath, dirnames, _filenames in os.walk(root, topdown=True, onerror=lambda _e: None):
        for name in (dirpath, *(os.path.join(dirpath, d) for d in dirnames)):
            with contextlib.suppress(OSError):
                os.chmod(name, 0o700)
    shutil.rmtree(root, ignore_errors=True)
    if root.exists():
        logger.warning("Directory %s could not be fully removed", root)


def ignore_patterns_and_symlinks(patterns: list[str]) -> Callable[[str, list[str]], set[str]]:
    """``copytree`` ``ignore`` callable that drops pattern matches AND every symlink.

    Symlinks in a copied tree — whether malicious or accidental — are rejected
    rather than dereferenced into the destination, which would leak host files
    (e.g. a ``creds -> /root/.aws/credentials`` plant) into a judge workspace or
    a staged reference directory.

    Shared by ``evaluation.sub_agent`` (sandbox → judge workspace copies) and
    ``orchestration.evaluation`` (reference → per-run staged copy) so the
    no-symlinks rule cannot drift between the two.
    """
    pattern_ignore = shutil.ignore_patterns(*patterns)

    def _ignore(src: str, names: list[str]) -> set[str]:
        ignored = set(pattern_ignore(src, names))
        src_path = Path(src)
        for name in names:
            if name in ignored:
                continue
            if (src_path / name).is_symlink():
                ignored.add(name)
        return ignored

    return _ignore


def task_log_path(run_dir: Path) -> Path:
    """Per-task log file path inside a task run directory."""
    return run_dir / TASK_LOG_FILENAME


def generate_run_id() -> str:
    """Generate filesystem-safe timestamp: YYYY-MM-DD_HH-MM-SS."""
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def replicate_subdir_name(replicate_index: int) -> str:
    """Two-digit, zero-padded directory name for a replicate (``'00'``, ``'01'``, ...).

    Two-digit padding caps unique replicate names at 100 (indices 0-99); if a
    follow-up PR ever needs >=100 replicates, widen the padding here.
    """
    return f"{replicate_index:02d}"


def build_task_run_dir(
    run_dir: Path,
    variant_id: str,
    task_id: str,
    replicate_index: int = 0,
) -> Path:
    """Build the per-task run dir: ``<run_dir>/<variant_id>/<task_id>/<NN>/``."""
    return run_dir / variant_id / task_id / replicate_subdir_name(replicate_index)


def format_task_log_id(variant_id: str, task_id: str, replicate_index: int = 0) -> str:
    """Canonical ``<variant_id>/<task_id>/<NN>`` identifier used by:
    - Orchestrator ``_log_task_id`` (console/file log tag, streaming events)
    - Batch ``stream_label``
    - CLI tqdm progress-bar postfix

    Shape mirrors ``build_task_run_dir`` (same three segments, same NN padding
    via ``replicate_subdir_name``) so log tags and on-disk paths stay in
    lockstep. Callers MUST use this helper rather than hand-rolling the
    f-string so future format changes (e.g., NN → NNN) touch exactly one
    place.
    """
    return f"{variant_id}/{task_id}/{replicate_subdir_name(replicate_index)}"


def create_latest_symlink(runs_base: Path, run_id: str) -> None:
    """Create/update 'latest' symlink to current run.

    Gracefully handles Windows where symlinks may fail.

    Args:
        runs_base: Base directory containing all runs (e.g., "runs/")
        run_id: ID of the current run (e.g., "2025-10-09_15-30-45")
    """
    latest_link = runs_base / "latest"
    # Use relative path for symlink target (just the run_id directory name)
    # This ensures the symlink works correctly when both are in the same directory
    target = Path(run_id)

    try:
        # Remove existing symlink/file
        if latest_link.exists() or latest_link.is_symlink():
            latest_link.unlink()

        # Create symlink with relative path
        latest_link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Windows may not support symlinks, skip gracefully
        if platform.system() != "Windows":
            raise
