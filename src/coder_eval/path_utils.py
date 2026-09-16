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

# A RE-GRADE's log goes here, NOT to task.log: the log handler opens its file
# `mode="w"`, so a detached grade pointed at task.log truncates the agent
# trajectory the run already paid for.
# Rationale: .claude/notes/persistence.md § Run-directory filename constants
GRADE_LOG_FILENAME = "grade.log"

# The per-task result record, and the pre-grade snapshot a detached grade keeps
# beside it.
TASK_JSON_FILENAME = "task.json"
PRE_GRADE_JSON_FILENAME = "task.execute.json"
# The already-executed row a DETACHED GRADE seeds from, staged into the grading
# container's read-only input mount. Never written by a run.
PRIOR_RESULT_FILENAME = "prior.json"

# The container's stdout+stderr transcript, and its fold-back name after a GRADING
# container. Named for the PHASE: on ``run --resume`` ``docker.log`` already holds the
# executed container's log, and reusing it repeats the task.log/grade.log truncation bug.
# Constants, not literals (CE053): the fold-back is guarded by ``is_file()``, so a
# rename on the producing side would silently skip the copy.
# Rationale: .claude/notes/persistence.md § Run-directory filename constants
DOCKER_LOG_FILENAME = "docker.log"
GRADE_DOCKER_LOG_FILENAME = "grade.docker.log"

# The virtualenv directory `setup` creates and `adopt` discovers. Named because
# whether it is on PATH decides which binaries a criterion resolves.
VENV_DIRNAME = ".venv"

# Ignore list for every copy of a reference solution tree. Shared, because the
# host-side docker mount and the per-run staged copy are the SAME operation on
# two mutually exclusive driver paths.
# Rationale: .claude/notes/persistence.md § Run-directory filename constants
REFERENCE_COPY_IGNORE = [".git"]


def write_text_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file + ``os.replace``.

    A plain ``write_text`` truncates first, so a crash mid-write leaves a
    half-file — and a truncated ``task.json`` parses as *malformed*, which
    ``--resume`` reads as "not complete" and pays for the agent again. One
    writer, so every producer has the same crash semantics.

    The temp file is opened ``O_CREAT | O_EXCL | O_NOFOLLOW`` under a name that
    is UNIQUE per call. ``O_NOFOLLOW`` closes a symlink-plant overwrite
    primitive; the unique name keeps ``O_EXCL``'s guarantee while making a
    leftover from a SIGKILLed predecessor inert instead of a permanent refusal
    to write the record. Mode is ``0o644``, the widest that is never group- or
    world-WRITABLE; do not narrow it, because the docker driver reads this file
    back as a different uid. It is a CEILING, not a guarantee — the umask still
    narrows it (0o077 yields 0600).

    Rationale: .claude/notes/persistence.md § write_text_atomic
    """
    # pid + random: unique across concurrent writers AND across a crashed
    # predecessor, so O_EXCL cannot collide with our own leftovers.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o644)
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

    Plain ``rmtree(..., ignore_errors=True)`` silently declines on such a tree,
    and an ``onexc`` handler cannot fix it either — the failing call is the
    ``scandir`` that drives the walk. So traversal is restored on the way DOWN
    first, then the tree is deleted.

    Rationale: .claude/notes/persistence.md § rmtree_restrictive
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


def task_log_path(run_dir: Path, *, regrade: bool = False) -> Path:
    """Per-task log file path inside a task run directory.

    ``regrade=True`` returns the ``grade.log`` sibling instead. The caller is a
    grading pass over a trajectory that already exists on disk, and the log
    handler truncates whatever file it is given — so the two passes must not
    share one.
    """
    return run_dir / (GRADE_LOG_FILENAME if regrade else TASK_LOG_FILENAME)


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

    Shape mirrors ``build_task_run_dir``, so log tags and on-disk paths stay in
    lockstep. Callers MUST use this helper rather than hand-rolling the f-string.
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
    # Relative target, so the symlink resolves from the runs base itself.
    target = Path(run_id)

    try:
        if latest_link.exists() or latest_link.is_symlink():
            latest_link.unlink()
        latest_link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        if platform.system() != "Windows":
            raise
