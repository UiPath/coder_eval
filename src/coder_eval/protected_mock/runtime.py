"""Root-harness lifecycle wrapper for the protected mockd subprocess."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from .protocol import SERVER_LAUNCHER, SOCKET_PATH


# Generous on purpose: normal startup is well under a second, but a loaded box
# -- parallel workers, cold caches -- can stall interpreter startup and bind
# well past a tight deadline. The common case is unaffected: the poll returns as
# soon as the socket appears.
STARTUP_TIMEOUT_SECONDS = 30.0


def _server_stderr_suffix(stderr_path: Path) -> str:
    """Tail of the child's captured stderr, formatted for an error message."""
    try:
        text = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if not text:
        return ""
    return f"; server stderr (tail): {text[-2000:]}"


@contextlib.contextmanager
def running_mock_server(config_path: Path | None) -> Iterator[None]:
    if config_path is None:
        yield
        return

    # Captured to a file rather than a pipe: nothing drains a pipe here, and a
    # full one would deadlock the child. The temp file is created 0600 and owned
    # by the spawning process (root inside the container), so the agent uid
    # cannot read it. It deliberately lives outside the socket directory, which
    # mockd creates for itself.
    with tempfile.NamedTemporaryFile(prefix="coder-eval-mockd-", suffix=".stderr", delete=False) as stderr_sink:
        stderr_path = Path(stderr_sink.name)
        try:
            process = subprocess.Popen(
                [
                    SERVER_LAUNCHER,
                    sys.executable,
                    "-m",
                    "coder_eval.protected_mock.server",
                    "--config",
                    str(config_path),
                ],
                stdin=subprocess.DEVNULL,
                stderr=stderr_sink,
            )
        except OSError:
            stderr_path.unlink(missing_ok=True)
            raise

    socket_path = Path(SOCKET_PATH)
    try:
        started = time.monotonic()
        deadline = started + STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"protected mockd exited during startup with code {process.returncode}"
                    + _server_stderr_suffix(stderr_path)
                )
            if socket_path.exists():
                break
            time.sleep(0.02)
        else:
            waited = time.monotonic() - started
            deadline_note = f"within {waited:.1f}s (deadline {STARTUP_TIMEOUT_SECONDS}s)"
            raise RuntimeError(
                f"protected mockd did not create its socket {deadline_note}" + _server_stderr_suffix(stderr_path)
            )
        yield
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        with contextlib.suppress(OSError):
            os.unlink(socket_path)
        with contextlib.suppress(OSError):
            os.unlink(stderr_path)
