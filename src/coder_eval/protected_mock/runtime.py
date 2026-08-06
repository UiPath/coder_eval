"""Host-side lifecycle for the protected mock service, plus the sandbox shim template.

``ProtectedMockRuntime`` owns one per-run service: it resolves fixture paths
against the task directory, writes the server config and token into a fresh
scratch directory, spawns ``sys.executable -m coder_eval.protected_mock.server``,
waits for the endpoint file the server publishes once bound, and tears the
process down (terminate, then kill) on exit. Fixture bytes never enter the
agent workspace; only the generated shim does.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from coder_eval.models import ProtectedMockConfig

from .protocol import (
    CALL_LOG_ENV,
    ENDPOINT_ENV,
    ENDPOINT_FILE_NAME,
    PROTOCOL_VERSION,
    TOKEN_ENV,
    TOKEN_FILE_NAME,
)


STARTUP_TIMEOUT_SECONDS = 10.0

# Host-side per-task invocation log, written next to task.json in the run dir
# (never inside the sandbox). Diagnostic surface: the `cli_called` criterion
# resolves its `log` field sandbox-relative, so it cannot read this file today.
CALL_LOG_NAME = "protected_mock_calls.jsonl"


def resolve_fixture_path(fixture: str, task_dir: Path | None) -> Path:
    """Resolve a fixture path against the task YAML's directory (host-side).

    Mirrors how ``uipath_eval.eval_set`` resolves: relative paths join the task
    directory; absolute paths are used as-is. The fixture must exist.
    """
    path = Path(fixture)
    if not path.is_absolute() and task_dir is not None:
        path = task_dir / path
    path = path.resolve()
    if not path.is_file():
        raise RuntimeError(f"protected mock fixture not found: {path}")
    return path


def fixture_digest(fixture_paths: list[Path]) -> str:
    """SHA-256 over the fixture contents in config order, for the audit record."""
    digest = hashlib.sha256()
    for path in fixture_paths:
        digest.update(path.read_bytes())
    return digest.hexdigest()


class ProtectedMockRuntime:
    """Context-manager lifecycle for one per-run protected mock server process."""

    def __init__(
        self,
        mocks: list[ProtectedMockConfig],
        *,
        task_dir: Path | None,
        transport: str = "auto",
    ) -> None:
        if not mocks:
            raise ValueError("ProtectedMockRuntime requires at least one protected mock")
        self._mocks = mocks
        self._task_dir = task_dir
        self._transport = transport
        self._runtime_dir: Path | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self.endpoint: str = ""
        self.token: str = ""
        self.fixture_digest: str = ""

    @property
    def endpoint_kind(self) -> str:
        """``"unix"`` or ``"tcp"`` (empty before start)."""
        return self.endpoint.split(":", 1)[0] if self.endpoint else ""

    def start(self) -> None:
        """Resolve fixtures, spawn the server, and wait for its endpoint file."""
        # Short prefix on purpose: the AF_UNIX socket lives in this directory and
        # sun_path has a tight length limit on some platforms.
        self._runtime_dir = Path(tempfile.mkdtemp(prefix="cepm-"))
        try:
            fixture_paths = [resolve_fixture_path(mock.fixture, self._task_dir) for mock in self._mocks]
            self.fixture_digest = fixture_digest(fixture_paths)
            config_path = self._runtime_dir / "mock-config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "version": PROTOCOL_VERSION,
                        "tools": [
                            {
                                "tool": mock.tool,
                                "fixture": str(path),
                                "max_requests": mock.max_requests,
                                "passthrough_argv_prefixes": mock.passthrough_argv_prefixes,
                            }
                            for mock, path in zip(self._mocks, fixture_paths, strict=True)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self.token = secrets.token_hex(16)
            (self._runtime_dir / TOKEN_FILE_NAME).write_text(self.token + "\n", encoding="utf-8")
            self._process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "coder_eval.protected_mock.server",
                    "--config",
                    str(config_path),
                    "--runtime-dir",
                    str(self._runtime_dir),
                    "--transport",
                    self._transport,
                ],
                stdin=subprocess.DEVNULL,
            )
            self.endpoint = self._await_endpoint()
        except Exception:
            self.stop()
            raise

    def _await_endpoint(self) -> str:
        assert self._runtime_dir is not None and self._process is not None
        endpoint_file = self._runtime_dir / ENDPOINT_FILE_NAME
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError(f"protected mock server exited during startup with code {self._process.returncode}")
            if endpoint_file.is_file():
                endpoint = endpoint_file.read_text(encoding="utf-8").strip()
                if endpoint:
                    return endpoint
            time.sleep(0.02)
        raise RuntimeError(f"protected mock server did not publish its endpoint within {STARTUP_TIMEOUT_SECONDS}s")

    def stop(self) -> None:
        """Terminate the server (kill on a slow exit) and remove the scratch dir."""
        if self._process is not None:
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        self._process.wait(timeout=3)
            self._process = None
        if self._runtime_dir is not None:
            shutil.rmtree(self._runtime_dir, ignore_errors=True)
            self._runtime_dir = None

    def __enter__(self) -> ProtectedMockRuntime:
        self.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()


_SHIM_TEMPLATE = '''\
#!{interpreter}
"""Protected mock shim for `{tool}` - generated by coder_eval SandboxConfig.protected_mocks.

Forwards the invocation to the host-side fixture service. No fixture data is
stored in this script or anywhere in the workspace. Do not edit: regenerated on
every sandbox setup.
"""

import os
import subprocess
import sys

TOOL = {tool!r}
INTERPRETER = {interpreter!r}

# Baked in on purpose: the client must not depend on this process's inherited
# environment for its connection details.
SERVICE_ENV = {{
    {endpoint_env!r}: {endpoint!r},
    {token_env!r}: {token!r},
    {call_log_env!r}: {call_log!r},
}}

if __name__ == "__main__":
    env = dict(os.environ)
    env.update(SERVICE_ENV)
    raise SystemExit(
        subprocess.call(
            [INTERPRETER, "-m", "coder_eval.protected_mock.client", TOOL, *sys.argv[1:]],
            env=env,
        )
    )
'''


def render_shim(tool: str, *, interpreter: str, endpoint: str, token: str, call_log: str) -> str:
    """Render the sandbox-visible shim source for one ``protected_mocks`` entry.

    The interpreter is the harness's own Python (where ``coder_eval`` is
    importable); the shebang uses its absolute path so PATH order inside the
    sandbox cannot change what runs.
    """
    return _SHIM_TEMPLATE.format(
        interpreter=interpreter,
        tool=tool,
        endpoint=endpoint,
        token=token,
        call_log=call_log,
        endpoint_env=ENDPOINT_ENV,
        token_env=TOKEN_ENV,
        call_log_env=CALL_LOG_ENV,
    )
