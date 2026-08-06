"""Protected mock server: fixture-backed CLI service loaded host-side, queried by a thin client.

Fixtures are read by this process only; the evaluated agent sees a generated
shim that forwards each invocation over a per-run socket. The endpoint is
chosen at bind time: an AF_UNIX socket in the run's scratch directory when a
real bind succeeds, else TCP on 127.0.0.1 with an ephemeral port. Requests must
carry the run's token; peer-credential checks (Unix ancillary credentials) run
only where available AND configured -- the Docker isolation layer will
configure them, the tempdir driver does not (same-user, nothing to verify
beyond the token).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import socketserver
import struct
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .protocol import (
    ENDPOINT_FILE_NAME,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    PROTOCOL_VERSION,
    SOCKET_FILE_NAME,
    TOKEN_FILE_NAME,
    format_tcp_endpoint,
    format_unix_endpoint,
)


@dataclass(frozen=True)
class CommandResponse:
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class ToolState:
    responses: dict[tuple[str, ...], CommandResponse]
    normalized_responses: dict[tuple[str, ...], CommandResponse]
    subset_responses: list[tuple[tuple[str, ...], CommandResponse]]
    default: CommandResponse
    remaining: int
    passthrough_prefixes: tuple[tuple[str, ...], ...]
    passthrough_executable: str | None
    passthrough_cache: dict[tuple[str, ...], CommandResponse]


PASSTHROUGH_TIMEOUT_SECONDS = 60
_NOISE_VALUE_FLAGS = frozenset({"--output"})


def _expand_argv_tokens(argv: list[str]) -> list[str]:
    """Flag-form-agnostic token stream: ``--flag=value`` split, noise flags dropped."""

    expanded: list[str] = []
    for raw in argv:
        if raw.startswith("-") and "=" in raw:
            flag, value = raw.split("=", 1)
            expanded.append(flag)
            if value:
                expanded.append(value)
        else:
            expanded.append(raw)

    cleaned: list[str] = []
    skip_next = False
    for token in expanded:
        if skip_next:
            skip_next = False
            continue
        if token in _NOISE_VALUE_FLAGS:
            skip_next = True
            continue
        cleaned.append(token)
    return cleaned


def _normalized_argv(argv: list[str]) -> tuple[str, ...]:
    """Canonical finite-command key: flag form/order agnostic, never subset matching."""

    return tuple(sorted(_expand_argv_tokens(argv)))


def _response(raw: object, *, context: str) -> CommandResponse:
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be an object")
    exit_code = raw.get("exit_code", 0)
    stdout = raw.get("stdout", "")
    stderr = raw.get("stderr", "")
    if not isinstance(exit_code, int) or not 0 <= exit_code <= 255:
        raise ValueError(f"{context}.exit_code must be an integer from 0 to 255")
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise ValueError(f"{context} stdout/stderr must be strings")
    encoded_size = len(stdout.encode("utf-8")) + len(stderr.encode("utf-8"))
    if encoded_size > MAX_RESPONSE_BYTES // 2:
        raise ValueError(f"{context} response exceeds the configured size limit")
    return CommandResponse(exit_code=exit_code, stdout=stdout, stderr=stderr)


def _load_tool(
    tool: str,
    fixture_path: Path,
    max_requests: int,
    passthrough_prefixes: list[list[str]],
) -> ToolState:
    raw = json.loads(fixture_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != PROTOCOL_VERSION:
        raise ValueError(f"fixture {fixture_path} must declare version {PROTOCOL_VERSION}")
    entries = raw.get("responses")
    if not isinstance(entries, list):
        raise ValueError(f"fixture {fixture_path} responses must be a list")
    responses: dict[tuple[str, ...], CommandResponse] = {}
    normalized_responses: dict[tuple[str, ...], CommandResponse] = {}
    # Ordered on purpose: subset rules are scanned in fixture-file order and the
    # first match wins, so duplicates are legal (an earlier rule shadows a later
    # one) -- the duplicate-key error applies to the finite match modes only.
    subset_responses: list[tuple[tuple[str, ...], CommandResponse]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"fixture {fixture_path} response {index} must be an object")
        argv = entry.get("argv")
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            raise ValueError(f"fixture {fixture_path} response {index}.argv must be a string list")
        match_mode = entry.get("match_mode", "exact")
        if match_mode not in {"exact", "normalized", "subset"}:
            raise ValueError(f"fixture {fixture_path} response {index}.match_mode must be exact, normalized, or subset")
        if match_mode == "subset":
            rule_tokens = tuple(_expand_argv_tokens(argv))
            if not argv or not rule_tokens:
                raise ValueError(f"fixture {fixture_path} response {index}.argv must be non-empty for subset matching")
            subset_responses.append((rule_tokens, _response(entry, context=f"response {index}")))
            continue
        key = tuple(argv)
        destination = responses if match_mode == "exact" else normalized_responses
        command_key = key if match_mode == "exact" else _normalized_argv(argv)
        if command_key in destination:
            raise ValueError(f"fixture {fixture_path} contains duplicate argv {argv!r} for {match_mode} matching")
        destination[command_key] = _response(entry, context=f"response {index}")
    default = _response(
        raw.get(
            "default",
            {"exit_code": 2, "stderr": "protected mock: command is not configured for this scenario\n"},
        ),
        context="default",
    )
    executable = shutil.which(tool) if passthrough_prefixes else None
    if passthrough_prefixes and executable is None:
        raise ValueError(f"protected mock passthrough tool is not installed: {tool}")
    return ToolState(
        responses=responses,
        normalized_responses=normalized_responses,
        subset_responses=subset_responses,
        default=default,
        remaining=max_requests,
        passthrough_prefixes=tuple(tuple(prefix) for prefix in passthrough_prefixes),
        passthrough_executable=executable,
        passthrough_cache={},
    )


def load_config(config_path: Path) -> dict[str, ToolState]:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != PROTOCOL_VERSION:
        raise ValueError(f"mock config must declare version {PROTOCOL_VERSION}")
    tools = raw.get("tools")
    if not isinstance(tools, list) or not tools:
        raise ValueError("mock config tools must be a non-empty list")
    loaded: dict[str, ToolState] = {}
    for entry in tools:
        if not isinstance(entry, dict):
            raise ValueError("mock config tool entries must be objects")
        tool = entry.get("tool")
        fixture = entry.get("fixture")
        max_requests = entry.get("max_requests")
        passthrough_prefixes = entry.get("passthrough_argv_prefixes", [])
        if not isinstance(tool, str) or not tool or tool in loaded:
            raise ValueError("mock config tools must have unique non-empty names")
        if not isinstance(fixture, str) or not isinstance(max_requests, int) or max_requests < 1:
            raise ValueError(f"mock config entry for {tool!r} has invalid fixture or max_requests")
        if not isinstance(passthrough_prefixes, list) or not all(
            isinstance(prefix, list) and prefix and all(isinstance(token, str) and token for token in prefix)
            for prefix in passthrough_prefixes
        ):
            raise ValueError(f"mock config entry for {tool!r} has invalid passthrough prefixes")
        loaded[tool] = _load_tool(tool, Path(fixture), max_requests, passthrough_prefixes)
    return loaded


_UnixStreamServer: Any = getattr(socketserver, "UnixStreamServer", object)


class ProtectedMockServer:
    """Transport-agnostic dispatch core; instantiated via a transport subclass.

    ``token`` gates every request (same-user hygiene). ``allowed_peer_uids``
    additionally gates by Unix socket peer credentials when set -- the Docker
    isolation layer will set it; the tempdir driver leaves it ``None``.
    """

    tools: dict[str, ToolState]
    budget_lock: threading.Lock
    passthrough_lock: threading.Lock
    token: str | None
    allowed_peer_uids: frozenset[int] | None

    def _init_state(self, tools: dict[str, ToolState], token: str | None) -> None:
        self.tools = tools
        self.budget_lock = threading.Lock()
        self.passthrough_lock = threading.Lock()
        self.token = token
        self.allowed_peer_uids = None

    def dispatch(self, tool: str, argv: list[str]) -> CommandResponse:
        state = self.tools.get(tool)
        if state is None:
            return CommandResponse(127, "", "protected mock: unknown tool\n")
        with self.budget_lock:
            if state.remaining <= 0:
                return CommandResponse(75, "", "protected mock: request budget exhausted\n")
            state.remaining -= 1
        response = state.responses.get(tuple(argv))
        if response is None:
            response = state.normalized_responses.get(_normalized_argv(argv))
        if response is None and state.subset_responses:
            # Finite matches take precedence; subset rules scan in fixture-file
            # order and the first whose tokens all appear in the invocation's
            # normalized token set wins.
            invocation_tokens = set(_expand_argv_tokens(argv))
            for rule_tokens, candidate in state.subset_responses:
                if all(token in invocation_tokens for token in rule_tokens):
                    response = candidate
                    break
        if response is not None:
            return response
        if any(tuple(argv[: len(prefix)]) == prefix for prefix in state.passthrough_prefixes):
            return self._passthrough(state, argv)
        return state.default

    def _passthrough(self, state: ToolState, argv: list[str]) -> CommandResponse:
        key = tuple(argv)
        with self.passthrough_lock:
            cached = state.passthrough_cache.get(key)
            if cached is not None:
                return cached
            if state.passthrough_executable is None:
                return CommandResponse(69, "", "protected mock: passthrough is unavailable\n")
            try:
                result = subprocess.run(
                    [state.passthrough_executable, *argv],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    timeout=PASSTHROUGH_TIMEOUT_SECONDS,
                )
                exit_code = result.returncode if 0 <= result.returncode <= 255 else 70
                response = CommandResponse(exit_code, result.stdout, result.stderr)
            except (OSError, subprocess.SubprocessError):
                response = CommandResponse(70, "", "protected mock: passthrough failed\n")
            encoded_size = len(response.stdout.encode("utf-8")) + len(response.stderr.encode("utf-8"))
            if encoded_size > MAX_RESPONSE_BYTES // 2:
                response = CommandResponse(70, "", "protected mock: passthrough response exceeds size limit\n")
            state.passthrough_cache[key] = response
            return response


class _TcpMockServer(ProtectedMockServer, socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, tools: dict[str, ToolState], token: str | None) -> None:
        self._init_state(tools, token)
        super().__init__(("127.0.0.1", 0), ProtectedMockHandler)


class _UnixMockServer(ProtectedMockServer, socketserver.ThreadingMixIn, _UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, tools: dict[str, ToolState], token: str | None) -> None:
        self._init_state(tools, token)
        super().__init__(path, ProtectedMockHandler)  # pyright: ignore[reportCallIssue]


class ProtectedMockHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server = cast(ProtectedMockServer, self.server)
        line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
            self._write(CommandResponse(64, "", "protected mock: invalid request size\n"))
            return
        try:
            request: Any = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict) or request.get("version") != PROTOCOL_VERSION:
                raise ValueError
            tool = request.get("tool")
            argv = request.get("argv")
            if not isinstance(tool, str) or not isinstance(argv, list):
                raise ValueError
            if not all(isinstance(item, str) for item in argv):
                raise ValueError
        except (UnicodeError, ValueError):
            self._write(CommandResponse(64, "", "protected mock: invalid request\n"))
            return
        if server.token is not None and request.get("token") != server.token:
            self._write(CommandResponse(77, "", "protected mock: caller token rejected\n"))
            return
        if server.allowed_peer_uids is not None and self._peer_uid() not in server.allowed_peer_uids:
            self._write(CommandResponse(77, "", "protected mock: caller identity rejected\n"))
            return
        self._write(server.dispatch(tool, argv))

    def _peer_uid(self) -> int:
        peer_cred = getattr(socket, "SO_PEERCRED", None)
        if peer_cred is None:
            raise RuntimeError("SO_PEERCRED is required for protected mock caller validation")
        credentials = self.request.getsockopt(socket.SOL_SOCKET, peer_cred, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", credentials)
        return uid

    def _write(self, response: CommandResponse) -> None:
        payload = (
            json.dumps(
                {
                    "version": PROTOCOL_VERSION,
                    "exit_code": response.exit_code,
                    "stdout": response.stdout,
                    "stderr": response.stderr,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        if len(payload) > MAX_RESPONSE_BYTES:
            payload = (
                json.dumps(
                    {
                        "version": PROTOCOL_VERSION,
                        "exit_code": 70,
                        "stdout": "",
                        "stderr": "protected mock: response exceeds size limit\n",
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
        self.wfile.write(payload)


def create_server(
    tools: dict[str, ToolState],
    token: str | None,
    runtime_dir: Path,
    transport: str = "auto",
) -> tuple[ProtectedMockServer, str]:
    """Bind the service and return ``(server, endpoint string)``.

    ``transport``: ``"unix"`` forces AF_UNIX, ``"tcp"`` forces TCP loopback,
    ``"auto"`` probes with a real AF_UNIX bind in ``runtime_dir`` and falls back
    to TCP when the platform (or the path) does not support it.
    """
    if transport not in {"auto", "unix", "tcp"}:
        raise ValueError(f"unknown protected mock transport: {transport!r}")
    if transport != "tcp" and getattr(socket, "AF_UNIX", None) is not None and _UnixStreamServer is not object:
        socket_path = runtime_dir / SOCKET_FILE_NAME
        socket_path.unlink(missing_ok=True)
        try:
            server: ProtectedMockServer = _UnixMockServer(str(socket_path), tools, token)
            return server, format_unix_endpoint(str(socket_path))
        except (OSError, ValueError):
            if transport == "unix":
                raise
    elif transport == "unix":
        raise RuntimeError("AF_UNIX stream sockets are unavailable on this platform")
    tcp_server = _TcpMockServer(tools, token)
    host, port = tcp_server.server_address[:2]
    return tcp_server, format_tcp_endpoint(str(host), int(port))


def serve(config_path: Path, runtime_dir: Path, transport: str = "auto") -> None:
    """Load fixtures, bind, publish the endpoint file, and serve until terminated.

    The endpoint file is written atomically (temp + rename) once the socket is
    bound, so a reader that sees the file always sees a complete, live endpoint.
    """
    # The runtime always passes an absolute dir (tempfile.mkdtemp); resolve
    # anyway so a hand-launched relative --runtime-dir cannot publish a
    # relative socket path in the endpoint file.
    runtime_dir = runtime_dir.resolve()
    token_file = runtime_dir / TOKEN_FILE_NAME
    token = token_file.read_text(encoding="utf-8").strip() if token_file.is_file() else None
    tools = load_config(config_path)
    server, endpoint = create_server(tools, token, runtime_dir, transport)
    endpoint_file = runtime_dir / ENDPOINT_FILE_NAME
    endpoint_tmp = runtime_dir / (ENDPOINT_FILE_NAME + ".tmp")
    try:
        endpoint_tmp.write_text(endpoint + "\n", encoding="utf-8")
        os.replace(endpoint_tmp, endpoint_file)
        server.serve_forever(poll_interval=0.2)  # pyright: ignore[reportAttributeAccessIssue]
    finally:
        server.server_close()  # pyright: ignore[reportAttributeAccessIssue]
        endpoint_file.unlink(missing_ok=True)
        (runtime_dir / SOCKET_FILE_NAME).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--transport", default="auto", choices=["auto", "unix", "tcp"])
    args = parser.parse_args()
    serve(args.config, args.runtime_dir, args.transport)


if __name__ == "__main__":
    main()
