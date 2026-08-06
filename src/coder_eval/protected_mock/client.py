"""Thin, agent-visible client for the protected fixture-backed CLI service.

Connection details (endpoint, token, call-log path) come from environment
variables the generated shim bakes in and sets itself -- the client never
depends on the agent process environment carrying them.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

from .protocol import (
    CALL_LOG_ENV,
    CLIENT_TIMEOUT_SECONDS,
    ENDPOINT_ENV,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    PROTOCOL_VERSION,
    TOKEN_ENV,
    parse_endpoint,
)


def _record(tool: str, argv: list[str], exit_code: int) -> None:
    """Best-effort compatibility with the existing cli_called JSONL schema."""

    raw_path = os.environ.get(CALL_LOG_ENV)
    if not raw_path:
        return
    entry = {"ts": round(time.time(), 3), "tool": tool, "argv": argv, "exit": exit_code}
    try:
        with Path(raw_path).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(entry, ensure_ascii=True, separators=(",", ":")) + "\n")
    except OSError as exc:
        sys.stderr.write(f"protected mock client: invocation log failed: {exc!r}\n")


def _receive_line(connection: socket.socket) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = connection.recv(min(65536, MAX_RESPONSE_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise RuntimeError("response exceeded size limit")
        if b"\n" in chunk:
            break
    return b"".join(chunks).split(b"\n", 1)[0]


def _connect(endpoint: str) -> socket.socket:
    parsed = parse_endpoint(endpoint)
    if parsed[0] == "unix":
        af_unix = getattr(socket, "AF_UNIX", None)
        if af_unix is None:
            raise RuntimeError("Unix-domain sockets are unavailable")
        connection = socket.socket(af_unix, socket.SOCK_STREAM)
        try:
            connection.settimeout(CLIENT_TIMEOUT_SECONDS)
            connection.connect(parsed[1])
        except OSError:
            connection.close()
            raise
        return connection
    host, port = parsed[1]
    return socket.create_connection((host, port), timeout=CLIENT_TIMEOUT_SECONDS)


def invoke(tool: str, argv: list[str]) -> int:
    endpoint = os.environ.get(ENDPOINT_ENV, "")
    token = os.environ.get(TOKEN_ENV, "")
    request = (
        json.dumps(
            {"version": PROTOCOL_VERSION, "token": token, "tool": tool, "argv": argv},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    if len(request) > MAX_REQUEST_BYTES:
        sys.stderr.write("protected mock client: request exceeds size limit\n")
        _record(tool, argv, 125)
        return 125

    try:
        if not endpoint:
            raise RuntimeError(f"{ENDPOINT_ENV} is not set")
        with _connect(endpoint) as connection:
            connection.sendall(request)
            raw_response = _receive_line(connection)
        response = json.loads(raw_response.decode("utf-8"))
        if not isinstance(response, dict) or response.get("version") != PROTOCOL_VERSION:
            raise RuntimeError("invalid response envelope")
        exit_code = response.get("exit_code")
        stdout = response.get("stdout")
        stderr = response.get("stderr")
        if not isinstance(exit_code, int) or not 0 <= exit_code <= 255:
            raise RuntimeError("invalid response exit_code")
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            raise RuntimeError("invalid response streams")
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        sys.stderr.write(f"protected mock client: service unavailable or invalid response: {exc}\n")
        _record(tool, argv, 125)
        return 125

    if stdout:
        sys.stdout.write(stdout)
    if stderr:
        sys.stderr.write(stderr)
    _record(tool, argv, exit_code)
    return exit_code


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("protected mock client: missing tool name\n")
        return 64
    return invoke(sys.argv[1], sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
