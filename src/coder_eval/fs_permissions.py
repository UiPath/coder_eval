"""Temporary filesystem-permission windows for anti-cheat.

The agent under evaluation runs with the same filesystem view as the harness:
in ``driver: tempdir`` it is an ordinary process on the host, and in
``driver: docker`` the orchestrator and the agent share one container. Any
directory the harness can read, the agent can read too -- including the task
directory and the reference solution. An agent that greps for the reference
does not solve the task, it copies the answer.

:func:`set_permissions` closes that window: it chmods the target paths to a
mode (0o000 by default) for the duration of an ``async with`` block and falls
back on exit. The orchestrator wraps every ``agent.communicate`` call in it, so
the reference and task directories are unreadable exactly while the agent is
executing, and readable again by the time criteria and judges run.

Windows **stack**, which is what makes a mid-turn re-grant expressible: code
that runs inside the turn but is not the agent can open a narrower window to
read a shielded path, and the enclosing 000 is restored when it closes::

    async with set_permissions([reference], mode=RESTRICTED_MODE):
        ...                                       # agent turn: 000
        async with set_permissions([reference], mode=READ_ONLY_MODE):
            ...                                   # this code can read: 555
        ...                                       # back to 000

(No in-tree caller needs the inner form today -- the early-stop watcher decides
from the in-memory trajectory, and ``live_verdict`` takes no ``sandbox``
argument by design. The stack exists so that adding one does not require
reworking this module.)

Two further properties:

* **Pre-window mode capture, not a hardcoded restore.** The outermost exit
  restores the mode actually observed, so a repo that ships ``0o750`` task dirs
  stays ``0o750``.
* **Crash-safe.** Unwinds are also registered with :mod:`atexit` and on
  ``SIGINT``/``SIGTERM``, so a killed run does not leave a checked-out
  ``tasks/`` tree at mode 000.

.. warning::
   **This is defense-in-depth, not a boundary.** ``chmod`` is a DAC control.
   Two separate facts limit it against an agent running as root in the same
   container:

   * *Reading* a mode-000 path is bypassed via ``CAP_DAC_OVERRIDE`` /
     ``CAP_DAC_READ_SEARCH``; the docker driver drops both, which does close
     that half (verified: a ``chmod 000`` dir is readable by container root by
     default, and denied once the caps are gone).
   * *Restoring* the mode is gated on **owner-or-CAP_FOWNER**, and container
     root owns the bind-mounted copy. Dropping ``FOWNER``/``CHOWN`` closes this
     only where the mount preserves a non-root owner. Verified on Docker
     Desktop, where the mount reports uid 0: ``chmod 755 /work/references``
     succeeds even with all four caps dropped, and the agent then reads the
     solution.

   So this stops passive reads and accidental leakage; it does not stop an
   agent that deliberately re-opens the path. Full containment needs the agent
   to run as a non-root uid that does not own the reference (follow-up).
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import os
import signal
import threading
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from types import FrameType


logger = logging.getLogger(__name__)


# Mode applied during an agent turn: no read, write, or traverse for anyone.
RESTRICTED_MODE = 0o000

# Read + traverse, no write. The mode to re-grant with when something that runs
# INSIDE the turn window legitimately needs to read a shielded path.
READ_ONLY_MODE = 0o555


class _PermissionStack:
    """Process-wide stack of applied modes, per resolved path.

    A plain stack, not a refcount: the whole point is that windows nest with
    *different* modes, so what an exit has to restore is the mode of the
    enclosing window -- not "the original", and not "is anyone still holding
    it". A refcount cannot express that; it would see a nested re-grant as just
    another holder and silently leave the outer mode in place.

    The stack also subsumes what a refcount did, for free: two windows applying
    the same mode push two identical entries, and the inner pop re-applies the
    outer's (identical) mode instead of restoring the pre-window one.

    Keyed by the *resolved* path so a directory reached by two different
    relative routes is one entry. Guarded by a plain ``threading.Lock`` rather
    than an ``asyncio.Lock`` because the crash-safety handlers (:mod:`atexit`,
    signal handlers) run outside the event loop and must be able to take it.
    """

    def __init__(self) -> None:
        # RLock, not Lock: restore_all() runs from a signal handler, which can be
        # delivered on the main thread while atexit's restore_all() is already
        # mid-flight. A non-reentrant lock deadlocks the interpreter at exit —
        # exactly when restoring matters most.
        self._lock = threading.RLock()
        # resolved path -> (mode before the outermost window, applied-mode stack)
        self._entries: dict[Path, tuple[int, list[int]]] = {}

    def push(self, path: Path, mode: int) -> bool:
        """Apply ``mode`` to ``path`` and record it for the matching :meth:`pop`.

        Returns True when the caller must later pop. Returns False when the mode
        could not be applied at all (missing path, or chmod refused) -- the
        caller then skips the matching pop.
        """
        _install_crash_handlers()
        with self._lock:
            existing = self._entries.get(path)
            original = existing[0] if existing is not None else None
            try:
                if original is None:
                    original = path.stat().st_mode & 0o7777
                os.chmod(path, mode)
            except OSError as e:
                # A missing path is the common, benign case (task has no
                # reference). A genuine chmod refusal (read-only mount, foreign
                # owner) is worth a warning: the window is not in place and the
                # operator should know this run is not protected.
                if isinstance(e, FileNotFoundError):
                    logger.debug("set_permissions: %s does not exist; nothing to do", path)
                else:
                    logger.warning(
                        "set_permissions: could not chmod %s to %#o (%s) -- the agent may be "
                        + "able to read it during this turn",
                        path,
                        mode,
                        e,
                    )
                return False
            if existing is None:
                self._entries[path] = (original, [mode])
            else:
                existing[1].append(mode)
            logger.debug("set_permissions: %s -> %#o (depth %d)", path, mode, len(self._entries[path][1]))
            return True

    def pop(self, path: Path) -> None:
        """Undo the innermost applied mode: fall back to the enclosing one.

        Restores the pre-window mode only when the outermost window closes.
        """
        with self._lock:
            entry = self._entries.get(path)
            if entry is None:
                return
            original, applied = entry
            applied.pop()
            if applied:
                target = applied[-1]
            else:
                target = original
                del self._entries[path]
            # chmod INSIDE the lock. Releasing first would let a concurrent push()
            # observe the path still at the restricted mode and record THAT as its
            # `original` — so its own pop would then leave the path at 000
            # permanently, the exact failure this module exists to prevent.
            try:
                os.chmod(path, target)
                logger.debug("set_permissions: %s <- %#o", path, target)
            except OSError as e:
                logger.error(
                    "set_permissions: FAILED to chmod %s back to %#o (%s) -- the path may need a manual chmod",
                    path,
                    target,
                    e,
                )

    def restore_all(self) -> None:
        """Unwind every outstanding path to its pre-window mode (crash path)."""
        with self._lock:
            outstanding = [(path, entry[0]) for path, entry in self._entries.items()]
            self._entries.clear()
        for path, original in outstanding:
            try:
                os.chmod(path, original)
                logger.warning("set_permissions: emergency-restored %s to %#o", path, original)
            except OSError as e:
                logger.error("set_permissions: emergency restore of %s failed: %s", path, e)


_registry = _PermissionStack()

# Crash safety, installed on the first push(): an interpreter that dies mid-turn
# must not leave the user's checked-out tasks/ tree unreadable. Signal handlers
# chain to the previous handler so we don't swallow an operator's Ctrl-C.
_handlers_installed = False
_handlers_lock = threading.Lock()


def _install_crash_handlers() -> None:
    """Install atexit + signal restores. Idempotent; called on first push().

    Deliberately NOT called at import time: `sandbox.py` imports this module, so
    an import-time call would rewrite SIGINT/SIGTERM disposition for every
    process that merely imports coder_eval — including library embedders and
    host runs, where no window is ever opened and the registry stays empty.
    The whole install runs under the lock so a concurrent caller cannot observe
    a half-installed state.
    """
    global _handlers_installed
    with _handlers_lock:
        if _handlers_installed:
            return
        _install_locked()
        # Set only after a successful install, so a failure part-way through is
        # retried by the next push() rather than latched as "already done".
        _handlers_installed = True


def _install_locked() -> None:
    atexit.register(_registry.restore_all)

    def _make_handler(previous: object) -> object:
        def _handler(sig: int, frame: FrameType | None) -> None:
            _registry.restore_all()
            if callable(previous):
                previous(sig, frame)
            elif previous == signal.SIG_DFL or previous is None:
                # None == handler installed from C and not retrievable from
                # Python. Treating it like SIG_DFL restores default termination;
                # swallowing it would make SIGTERM stop killing the process.
                signal.signal(sig, signal.SIG_DFL)
                os.kill(os.getpid(), sig)

        return _handler

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous = signal.getsignal(signum)
            signal.signal(signum, _make_handler(previous))  # type: ignore[arg-type]
        except (ValueError, OSError):
            # Not on the main thread, or the platform lacks the signal. atexit
            # still covers the ordinary-exit case.
            logger.debug("set_permissions: could not install handler for signal %s", signum)


@contextlib.asynccontextmanager
async def set_permissions(
    paths: Iterable[Path | None],
    *,
    mode: int = RESTRICTED_MODE,
) -> AsyncIterator[None]:
    """Chmod ``paths`` to ``mode`` for the body, then fall back on exit.

    Windows NEST, and an inner window may be *more* permissive than the one
    around it -- that is the point. Exiting restores the enclosing window's
    mode, and only the outermost exit restores the pre-window mode::

        async with set_permissions([reference], mode=RESTRICTED_MODE):
            ...                                        # agent turn: 000
            async with set_permissions([reference], mode=READ_ONLY_MODE):
                ...                                    # something mid-turn reads: 555
            ...                                        # back to 000, not to 755

    ``None`` entries and duplicates are dropped, so callers can pass optional
    paths (``[task_dir, reference_dir]``) without pre-filtering. Paths are
    resolved before use so the stack keys are canonical.

    The unwind runs in a ``finally``, so it happens on the exception path too
    -- an agent crash or turn timeout must not leave the tree unreadable.

    Args:
        paths: Directories (or files) to chmod. ``None`` entries are skipped.
        mode: Permission bits to apply. Defaults to :data:`RESTRICTED_MODE`.
    """
    resolved: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        if raw is None:
            continue
        try:
            candidate = Path(raw).resolve()
        except OSError as e:
            # Same fail-open outcome as a chmod refusal, so it gets the same
            # visibility — this path is NOT shielded during the turn.
            logger.warning("set_permissions: could not resolve %s (%s); it will not be shielded", raw, e)
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        resolved.append(candidate)

    # chmod is a syscall per path; offload so a slow network filesystem doesn't
    # stall the event loop that is about to drive the agent's streaming turn.
    held: list[Path] = []
    if resolved:
        # Shielded like the unwind below. Unshielded, a cancellation landing on
        # this await (task_timeout watchdog, sibling batch failure) propagates out
        # of __aenter__ so the finally never runs — while the worker thread still
        # completes every chmod. The paths would stay at mode 000 with no matching
        # pop, and the run would then grade with an unreadable reference.
        held = await asyncio.shield(asyncio.to_thread(_push_all, resolved, mode))
    try:
        yield
    finally:
        if held:
            # Shielded: the unwind MUST run even when the surrounding task is
            # being cancelled (task_timeout watchdog), or the tree stays at 000.
            await asyncio.shield(asyncio.to_thread(_pop_all, held))


def _push_all(paths: list[Path], mode: int) -> list[Path]:
    """Push every path, returning only those that must later be popped."""
    return [path for path in paths if _registry.push(path, mode)]


def _pop_all(paths: list[Path]) -> None:
    for path in paths:
        _registry.pop(path)
