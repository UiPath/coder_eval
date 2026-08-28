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
the staged reference directory is unreadable exactly while the agent is
executing, and readable again by the time criteria and judges run. The task
directory is shielded by the same window: under docker it is mounted as a
throwaway COPY at a fixed container path, which is what makes it chmod-able
without touching the user's checked-out ``tasks/`` tree. See the call site in
``Orchestrator._communicate_with_retry``.

This shields grading MATERIAL that happens to live in the task directory (a
``reference/`` subdirectory, fixtures), not the task DEFINITION: ``task.yaml``
is separately staged at ``/work/input``, which the agent can still read.

Windows **stack**, which is what makes a mid-turn re-grant expressible: code
that runs inside the turn but is not the agent can open a narrower window to
read a shielded path, and the enclosing 000 is restored when it closes::

    async with set_permissions([reference], mode=RESTRICTED_MODE):
        ...                                       # agent turn: 000
        async with set_permissions([reference], mode=READ_ONLY_MODE):
            ...                                   # this code can read: 555
        ...                                       # back to 000

The inner form exists for ONE intended consumer: **live success criteria**.
Early-stop verdicts are computed while the agent turn is still running -- i.e.
inside the 000 window -- so a live criterion that needs to consult the reference
solution has to be able to read it exactly then, while the agent still cannot.
A flat set/restore cannot express that, and a refcount actively breaks it (it
treats the inner re-grant as just another holder and leaves 000 in place). That
is why this is a stack, and why :data:`READ_ONLY_MODE` is public. It is a
designed seam, not speculative generality.

NOT WIRED UP YET. The remaining work, for whoever picks it up:

* ``EarlyStopWatcher._evaluate_impl`` wraps its verdict loop in a
  ``READ_ONLY_MODE`` window over the reference. One window per round, around
  the loop rather than per criterion -- that is the tightest placement, which
  matters because a chmod is global filesystem state and the agent is running
  CONCURRENTLY: the re-grant is visible to it too, for as long as it is open.
* That loop is a ``StreamCallback`` (plain ``def``), so it needs a synchronous
  twin of :func:`set_permissions` pushing onto this same ``_registry`` -- the
  stack is already thread-safe, so the twin is small.
* ``live_verdict`` gains NO parameter. It reads the reference from a per-task
  accessor instead. That accessor must be a ``ContextVar``, NOT ``os.environ``:
  ``run_batch -j 8`` runs many orchestrators in one process, so a process-global
  would leak one task's reference into a sibling's verdict, silently and only
  under parallelism. (``REFERENCE_DIR`` today is set only in the ``env=`` dict
  handed to ``run_command`` subprocesses, so it is not readable in-process.)

Scope note: only the REFERENCE is shielded, never the sandbox. A live criterion
reading the agent's own output files therefore needs no window change at all --
and should not get one. Reading the static reference mid-turn cannot break the
``LiveVerdict`` monotonicity contract; reading the half-written sandbox can, and
is the "end-state peeking" ``live_verdict``'s own docstring rules out.

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
     root owns the bind-mounted copy, so ``chmod 755 /work/references`` puts it
     back and the agent reads the solution. ``FOWNER``/``CHOWN`` are NOT dropped
     to stop that, because the in-container orchestrator that applies the window
     is the same root process with the same caps: dropping them breaks the
     harness's own chmod wherever the mount preserves a non-root owner (native
     Linux), i.e. on exactly the hosts where the drop would otherwise bite.
     Closing this half needs a different uid, not a smaller capability set.

   A third limit is about *time*, not permissions: the window spans
   ``agent.communicate``, so between turns (and after the last one, while
   criteria run) the path is back at its pre-window mode. Nothing reaps the
   agent's child processes at turn end, so a backgrounded ``while ! cat ...``
   loop started during a turn succeeds the moment the window closes. Reading is
   only half of that — the docker mount is read-WRITE by necessity, so the same
   loop could *overwrite* the reference and drive ``reference_comparison`` to
   1.0. The overwrite half IS closed, by a content hash taken at staging time
   and re-verified before grading
   (``Orchestrator._verify_reference_integrity``); the read half is not.

   So this stops passive reads and accidental leakage; it does not stop an
   agent that deliberately re-opens the path or waits the window out. Full
   containment needs the agent to run as a non-root uid that does not own the
   reference, and the reference to be unreadable for the agent's whole lifetime
   rather than per-turn (follow-up).
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import os
import signal
import threading
from collections.abc import AsyncIterator, Callable, Iterable
from pathlib import Path
from types import FrameType
from typing import Any


logger = logging.getLogger(__name__)

# What ``signal.getsignal`` can hand back: a Python callable, one of the
# SIG_DFL/SIG_IGN sentinels, or None for a handler installed from C. Spelled out
# so the chained call into it is argument-checked instead of hidden behind
# ``object`` + a blanket ``# type: ignore``.
_SignalDisposition = Callable[[int, FrameType | None], Any] | int | signal.Handlers | None


# Mode applied during an agent turn: no read, write, or traverse for anyone.
RESTRICTED_MODE = 0o000

# Read + traverse, no write. The mode to re-grant with when something that runs
# INSIDE the turn window legitimately needs to read a shielded path.
READ_ONLY_MODE = 0o555


class PermissionWindowError(RuntimeError):
    """A permission window that MUST hold could not be applied.

    Raised only under ``strict=True`` — i.e. from ``Sandbox.set_permissions``
    inside a container, where the window is the anti-cheat control rather than a
    best-effort nicety.
    """


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
        # Whether the crash handlers are installed. An instance attribute rather
        # than a module-level global: the state belongs to the registry whose
        # entries the handlers restore, and a mutable module global read only by
        # its own writer reads as dead to static analysis.
        self._handlers_installed = False
        # RLock, not Lock: restore_all() runs from a signal handler, which can be
        # delivered on the main thread while atexit's restore_all() is already
        # mid-flight. A non-reentrant lock deadlocks the interpreter at exit —
        # exactly when restoring matters most.
        self._lock = threading.RLock()
        # resolved path -> (mode before the outermost window, applied-mode stack)
        self._entries: dict[Path, tuple[int, list[int]]] = {}

    def push(self, path: Path, mode: int, *, strict: bool = False) -> bool:
        """Apply ``mode`` to ``path`` and record it for the matching :meth:`pop`.

        Returns True when the caller must later pop. Returns False when the mode
        could not be applied at all (missing path, or chmod refused) -- the
        caller then skips the matching pop.

        Does NOT install the crash handlers: ``push`` runs on a worker thread
        (``asyncio.to_thread``), where ``signal.signal`` raises ``ValueError``.
        :func:`set_permissions` installs them from the event-loop thread before
        the offload.
        """
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
                    return False
                message = (
                    f"set_permissions: could not chmod {path} to {mode:#o} ({e}) -- "
                    + "the agent would be able to read it during this turn"
                )
                if strict:
                    # Fail closed. An unprotected run that reports a normal
                    # pass/fail is worse than no run: nothing downstream can tell
                    # it apart from a protected one.
                    raise PermissionWindowError(message) from e
                logger.warning("%s", message)
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
            target = applied[-1] if applied else original
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
                # Keep the entry: restore_all() (atexit / signal) is the last
                # chance to put this path back, and it can only do that while it
                # still holds the pre-window mode. Dropping the entry here would
                # strip the crash path of the only record of `original`.
                return
            if not applied:
                del self._entries[path]

    def ensure_crash_handlers(self) -> None:
        """Install atexit + signal restores once, before the first window opens.

        MUST be called from the main thread: ``signal.signal`` raises
        ``ValueError`` anywhere else. :func:`set_permissions` calls it on the
        event-loop thread before offloading the chmods, which is what makes the
        signal half actually take effect — installing from inside the
        ``to_thread`` worker (as an earlier revision did) silently failed and
        left SIGTERM with no restore at all.

        Deliberately NOT done at import time: ``sandbox.py`` imports this module,
        so an import-time install would rewrite SIGINT/SIGTERM disposition for
        every process that merely imports coder_eval — including library
        embedders and host runs, where no window is ever opened and this registry
        stays empty. The whole install runs under the lock so a concurrent caller
        cannot observe a half-installed state, and the flag latches only when the
        signal handlers really went in, so a call from a worker thread is retried
        from the main thread later rather than latching a no-op as done.
        """
        with self._lock:
            if self._handlers_installed:
                return
            self._handlers_installed = _install_crash_handlers(self)

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


def _make_signal_handler(
    registry: _PermissionStack,
    previous: _SignalDisposition,
) -> Callable[[int, FrameType | None], None]:
    """Build a handler that restores ``registry``, then chains to ``previous``.

    Chaining matters twice over: an operator's Ctrl-C must not be swallowed, and
    SIGTERM must still terminate. ``SIG_IGN`` is the one disposition that is
    neither callable nor ``SIG_DFL`` — it means "the process chose to ignore
    this", so restoring and returning is the correct chain.
    """

    def _handler(sig: int, frame: FrameType | None) -> None:
        registry.restore_all()
        if callable(previous):
            previous(sig, frame)
        elif previous == signal.SIG_IGN:
            return
        else:
            # SIG_DFL, or None == handler installed from C and not retrievable
            # from Python. Treating both as SIG_DFL restores default
            # termination; swallowing it would make SIGTERM stop killing us.
            signal.signal(sig, signal.SIG_DFL)
            os.kill(os.getpid(), sig)

    return _handler


def _install_crash_handlers(registry: _PermissionStack) -> bool:
    """Register the atexit + signal restores for ``registry``.

    Returns True only when every signal handler was installed, so the caller can
    decline to latch a partial (or entirely failed) install. Called once, under
    the registry's lock, from the main thread.
    """
    atexit.register(registry.restore_all)

    installed_all = True
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous = signal.getsignal(signum)
            signal.signal(signum, _make_signal_handler(registry, previous))
        except (ValueError, OSError) as e:
            # Not on the main thread, or the platform lacks the signal. atexit
            # still covers the ordinary-exit case, but SIGTERM does NOT run
            # atexit — so a killed run can strand the tree at mode 000. That is
            # worth more than a debug line.
            installed_all = False
            logger.warning(
                "set_permissions: could not install a restore handler for signal %s (%s); "
                + "a kill -TERM during a turn may leave the reference at mode 000",
                signum,
                e,
            )
    return installed_all


@contextlib.asynccontextmanager
async def set_permissions(
    paths: Iterable[Path | None],
    *,
    mode: int = RESTRICTED_MODE,
    strict: bool = False,
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
        strict: Raise :class:`PermissionWindowError` when an existing path
            cannot be chmod'd, instead of warning and continuing unprotected.
            Set by ``Sandbox.set_permissions`` whenever the window is actually
            enforced (in-container), so a broken anti-cheat control fails the
            run rather than producing a normal-looking score.

    Raises:
        PermissionWindowError: under ``strict``, when a path exists but the
            chmod was refused.
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

    # Install the crash restores HERE, on the event-loop (main) thread, and not
    # inside _push_all: signal.signal() raises ValueError off the main thread, so
    # installing from the to_thread worker below silently installed nothing.
    if resolved:
        _registry.ensure_crash_handlers()

    # chmod is a syscall per path; offload so a slow network filesystem doesn't
    # stall the event loop that is about to drive the agent's streaming turn.
    held: list[Path] = []
    push_task: asyncio.Future[list[Path]] | None = None
    try:
        if resolved:
            # The push sits INSIDE the try, so the finally below ALWAYS runs.
            # asyncio.shield protects the inner task, not this await: a
            # cancellation landing here (task_timeout watchdog, sibling batch
            # failure) still raises CancelledError out of the await while the
            # worker thread goes on to complete every chmod. With the push above
            # the try -- as an earlier revision had it -- that left the paths at
            # mode 000 with no matching pop: unreadable for the rest of the run,
            # and a stale registry entry that poisoned the next window on the
            # same path.
            push_task = asyncio.ensure_future(asyncio.to_thread(_push_all, resolved, mode, strict))
            held = await asyncio.shield(push_task)
        yield
    finally:
        if push_task is not None and not held:
            # We were cancelled mid-push. The shielded worker is still running
            # and still chmod'ing; join it so `held` names exactly what landed
            # and the unwind below cannot race it.
            held = await asyncio.shield(push_task)
        if held:
            # Shielded: the unwind MUST run even when the surrounding task is
            # being cancelled (task_timeout watchdog), or the tree stays at 000.
            await asyncio.shield(asyncio.to_thread(_pop_all, held))


def _push_all(paths: list[Path], mode: int, strict: bool) -> list[Path]:
    """Push every path, returning only those that must later be popped.

    Under ``strict`` a refused chmod raises, and the paths pushed before it are
    left applied: the context manager's ``finally`` cannot see a return value
    that never came. That is deliberate -- ``_registry.restore_all`` (atexit /
    signal) still holds their pre-window modes, and the alternative (unwinding
    here) would swallow the failure that must abort the run.
    """
    return [path for path in paths if _registry.push(path, mode, strict=strict)]


def _pop_all(paths: list[Path]) -> None:
    for path in paths:
        _registry.pop(path)
