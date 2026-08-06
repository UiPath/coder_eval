"""Thin, agent-visible client for the protected exact-command mock service."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

from .protocol import (
    CLIENT_TIMEOUT_SECONDS,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    PROTOCOL_VERSION,
    SOCKET_PATH,
)


def _record(tool: str, argv: list[str], exit_code: int) -> None:
    """Best-effort compatibility with the existing cli_called JSONL schema."""

    raw_path = os.environ.get("CODER_EVAL_MOCK_CALL_LOG")
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


def invoke(tool: str, argv: list[str]) -> int:
    request = json.dumps(
        {"version": PROTOCOL_VERSION, "tool": tool, "argv": argv},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(request) > MAX_REQUEST_BYTES:
        sys.stderr.write("protected mock client: request exceeds size limit\n")
        _record(tool, argv, 125)
        return 125

    try:
        af_unix = getattr(socket, "AF_UNIX", None)
        if af_unix is None:
            raise RuntimeError("Unix-domain sockets are unavailable")
        with socket.socket(af_unix, socket.SOCK_STREAM) as connection:
            connection.settimeout(CLIENT_TIMEOUT_SECONDS)
            connection.connect(SOCKET_PATH)
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
