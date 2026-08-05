"""Container-side permission primitives for the user/permission isolation barrier.

Under ``--driver docker`` the in-container entrypoint runs as root (grading needs
root) and, before the agent turn, (a) locks all grading material root-0700 so the
dropped agent uid gets EACCES, and (b) grants the agent uid ownership of the paths
it legitimately reads/writes (its workspace, the skill-DOCS copy, ``~/.claude``).
(``/tmp`` is left as-is — already 1777 world-writable, so the agent uid can create
its own temp files there without a chown.)

These three functions are the SINGLE chmod/chown choke point for the barrier:
harness paths must route through here, never a bare ``os.chown``/``os.chmod`` in
``docker_runner`` / ``run_task_internal_command`` (a CExxx lint rule guards this;
see the docker-isolation guardrails).

Every function is a guarded thin wrapper: it NO-OPS (debug-log) when the process
is not root or the platform is not Linux, so the module imports cleanly and is
unit-testable on macOS dev machines and non-root CI. The real chown/chmod only
fire inside the Linux container where they matter.

Fail-loud asymmetry: when the barrier IS active, ``lock_harness_root_0700``
RAISES on any chmod/chown failure (a silently-skipped lock leaves grading material
agent-readable — the leak class this barrier exists to close). ``grant_agent_ownership``
stays best-effort (a failed grant only costs the agent a convenience).
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterable
from pathlib import Path

from coder_eval.models import AGENT_GID, AGENT_UID


logger = logging.getLogger(__name__)

_ROOT_ONLY_MODE = 0o700


def _barrier_active() -> bool:
    """True only when we can actually enforce Unix ownership: Linux + effective root.

    Off-Linux or non-root the barrier is inert (host unit tests, macOS dev). The
    caller that *requires* the drop (the entrypoint when a drop is requested) fails
    loud separately; these primitives just no-op so import/unit-test stays safe.
    """
    if sys.platform != "linux":
        return False
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def _chown_chmod_tree(
    root: Path, uid: int, gid: int, *, mode: int | None, errors: list[tuple[Path, OSError]] | None = None
) -> None:
    """chown (and optionally chmod) ``root`` recursively. Symlinks are not followed.

    ``errors``: when a list is supplied, each ``OSError`` is appended to it (and NOT
    logged) so the caller can decide to fail loud; when ``None`` (the grant path),
    the error is best-effort debug-logged and swallowed.
    """

    def _apply(path: Path) -> None:
        try:
            os.chown(path, uid, gid, follow_symlinks=False)  # type: ignore[attr-defined]  # os.chown is POSIX-only; this whole module no-ops off-Linux
            if mode is not None and not path.is_symlink():
                os.chmod(path, mode)
        except OSError as exc:
            if errors is not None:
                errors.append((path, exc))
            else:
                logger.debug("container_perms: could not chown/chmod %s: %s", path, exc)

    _apply(root)
    if root.is_dir() and not root.is_symlink():
        for dirpath, dirnames, filenames in os.walk(root):
            base = Path(dirpath)
            for name in (*dirnames, *filenames):
                _apply(base / name)


def lock_harness_root_0700(paths: Iterable[Path]) -> None:
    """Make each path root-owned and mode-0700 (recursively for dirs).

    Denies the agent uid (and every non-root uid) read/traverse. Applied by
    ``_apply_isolation_barrier`` to the grading material it locks: ``/work/input``
    (criteria + the root-only ``task_full.json``), the per-task-dir mount, and any
    non-skill-DOCS plugin/grader mount. (``/work/output`` is deliberately NOT locked
    — it is a host-shared bind mount carrying the liveness heartbeat.) No-op
    off-Linux / non-root.
    """
    if not _barrier_active():
        logger.debug("lock_harness_root_0700: barrier inactive (non-root/non-linux), skipping")
        return
    # Fail LOUD: a lock that silently no-ops leaves grading material agent-readable
    # (the EROFS-on-:ro-bind-mount class of bug). Collect every chmod/chown failure
    # and raise if any occurred — consistent with the entrypoint's fail-loud non-root
    # check. The grant path stays best-effort (a failed grant only costs the agent a
    # convenience, never a leak).
    errors: list[tuple[Path, OSError]] = []
    for p in paths:
        if not p.exists():
            continue
        _chown_chmod_tree(p, 0, 0, mode=_ROOT_ONLY_MODE, errors=errors)
    if errors:
        detail = "; ".join(f"{path}: {exc}" for path, exc in errors[:5])
        raise OSError(
            f"lock_harness_root_0700 failed to lock {len(errors)} path(s); "
            + f"grading material may be agent-readable (first failures: {detail})"
        )


def grant_agent_ownership(paths: Iterable[Path], *, recursive: bool = True) -> None:
    """chown each path to the agent uid/gid so the dropped agent can read/write it.

    Applied to the agent-legitimate paths: the workspace, the skill-DOCS copy, and
    the ``~/.claude`` copy. Ownership only — the existing mode bits are left intact
    (root still reads everything, ignoring DAC). No-op off-Linux / non-root.
    """
    if not _barrier_active():
        logger.debug("grant_agent_ownership: barrier inactive (non-root/non-linux), skipping")
        return
    for p in paths:
        if not p.exists():
            continue
        if recursive and p.is_dir() and not p.is_symlink():
            _chown_chmod_tree(p, AGENT_UID, AGENT_GID, mode=None)
        else:
            try:
                os.chown(p, AGENT_UID, AGENT_GID, follow_symlinks=False)  # type: ignore[attr-defined]  # POSIX-only
            except OSError as exc:
                logger.debug("grant_agent_ownership: could not chown %s: %s", p, exc)
