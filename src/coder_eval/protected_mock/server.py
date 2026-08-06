"""mockd: exact-command fixture service running as the private mock UID."""

from __future__ import annotations

import argparse
import json
import os
import socket
import socketserver
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from coder_eval.models import AGENT_UID, MOCK_RPC_GID

from .protocol import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, PROTOCOL_VERSION, SOCKET_PATH


@dataclass(frozen=True)
class CommandResponse:
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class ToolState:
    responses: dict[tuple[str, ...], CommandResponse]
    default: CommandResponse
    remaining: int


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


def _load_tool(fixture_path: Path, max_requests: int) -> ToolState:
    raw = json.loads(fixture_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != PROTOCOL_VERSION:
        raise ValueError(f"fixture {fixture_path} must declare version {PROTOCOL_VERSION}")
    entries = raw.get("responses")
    if not isinstance(entries, list):
        raise ValueError(f"fixture {fixture_path} responses must be a list")
    responses: dict[tuple[str, ...], CommandResponse] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"fixture {fixture_path} response {index} must be an object")
        argv = entry.get("argv")
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            raise ValueError(f"fixture {fixture_path} response {index}.argv must be a string list")
        key = tuple(argv)
        if key in responses:
            raise ValueError(f"fixture {fixture_path} contains duplicate argv {argv!r}")
        responses[key] = _response(entry, context=f"response {index}")
    default = _response(
        raw.get(
            "default",
            {"exit_code": 2, "stderr": "protected mock: command is not configured for this scenario\n"},
        ),
        context="default",
    )
    return ToolState(responses=responses, default=default, remaining=max_requests)


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
        if not isinstance(tool, str) or not tool or tool in loaded:
            raise ValueError("mock config tools must have unique non-empty names")
        if not isinstance(fixture, str) or not isinstance(max_requests, int) or max_requests < 1:
            raise ValueError(f"mock config entry for {tool!r} has invalid fixture or max_requests")
        loaded[tool] = _load_tool(Path(fixture), max_requests)
    return loaded


_UnixStreamServer: Any = getattr(socketserver, "UnixStreamServer", object)


class ProtectedMockServer(socketserver.ThreadingMixIn, _UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, tools: dict[str, ToolState]) -> None:
        self.tools = tools
        self.budget_lock = threading.Lock()
        super().__init__(path, ProtectedMockHandler)  # pyright: ignore[reportCallIssue]

    def dispatch(self, tool: str, argv: list[str]) -> CommandResponse:
        state = self.tools.get(tool)
        if state is None:
            return CommandResponse(127, "", "protected mock: unknown tool\n")
        with self.budget_lock:
            if state.remaining <= 0:
                return CommandResponse(75, "", "protected mock: request budget exhausted\n")
            state.remaining -= 1
        return state.responses.get(tuple(argv), state.default)


class ProtectedMockHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        if self._peer_uid() not in {0, AGENT_UID}:
            self._write(CommandResponse(77, "", "protected mock: caller identity rejected\n"))
            return
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
        server = cast(ProtectedMockServer, self.server)
        self._write(server.dispatch(tool, argv))

    def _peer_uid(self) -> int:
        peer_cred = getattr(socket, "SO_PEERCRED", None)
        if peer_cred is None:
            raise RuntimeError("SO_PEERCRED is required for protected mock caller validation")
        credentials = self.request.getsockopt(socket.SOL_SOCKET, peer_cred, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", credentials)
        return uid

    def _write(self, response: CommandResponse) -> None:
        payload = json.dumps(
            {
                "version": PROTOCOL_VERSION,
                "exit_code": response.exit_code,
                "stdout": response.stdout,
                "stderr": response.stderr,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
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


def serve(config_path: Path) -> None:
    socket_path = Path(SOCKET_PATH)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    tools = load_config(config_path)
    server = ProtectedMockServer(str(socket_path), tools)
    try:
        chown = getattr(os, "chown", None)
        geteuid = getattr(os, "geteuid", None)
        if chown is None or geteuid is None:
            raise RuntimeError("mockd requires Linux chown/geteuid support")
        chown(socket_path, geteuid(), MOCK_RPC_GID)
        socket_path.chmod(0o660)
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        socket_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    serve(args.config)


if __name__ == "__main__":
    main()
