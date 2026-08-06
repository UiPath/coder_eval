"""Root-harness lifecycle wrapper for the protected mockd subprocess."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

from .protocol import SERVER_LAUNCHER, SOCKET_PATH


@contextlib.contextmanager
def running_mock_server(config_path: Path | None) -> Iterator[None]:
    if config_path is None:
        yield
        return

    process = subprocess.Popen(
        [SERVER_LAUNCHER, sys.executable, "-m", "coder_eval.protected_mock.server", "--config", str(config_path)],
        stdin=subprocess.DEVNULL,
    )
    socket_path = Path(SOCKET_PATH)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"protected mockd exited during startup with code {process.returncode}")
            if socket_path.exists():
                break
            time.sleep(0.02)
        else:
            raise RuntimeError("protected mockd did not create its socket within 5 seconds")
        yield
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        with contextlib.suppress(OSError):
            os.unlink(socket_path)
