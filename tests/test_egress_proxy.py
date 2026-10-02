"""The stdlib-only egress proxy: real sockets on 127.0.0.1, no docker."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from coder_eval import egress_proxy
from coder_eval.cli.run_task_internal_command import heartbeat_is_alive


PROXY_FILE = Path(egress_proxy.__file__)
ALLOWED_IMPORTS = {"argparse", "asyncio", "os", "sys", "time"}


async def _upstream(handler) -> tuple[asyncio.Server, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(1024):
        writer.write(data)
        await writer.drain()
    writer.close()


@contextlib.asynccontextmanager
async def _proxy(allow: set[str]) -> AsyncIterator[int]:
    server = await egress_proxy.start_proxy(frozenset(allow), "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        server.abort_clients()


async def _request(port: int, head: bytes) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head)
    await writer.drain()
    status = await asyncio.wait_for(reader.readline(), 5)
    return reader, writer, status


async def _status(allow: set[str], head: bytes) -> bytes:
    async with _proxy(allow) as port:
        _reader, writer, status = await _request(port, head)
        writer.close()
    return status.split()[1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_connect_to_allowlisted_target_tunnels_both_ways(capsys):
    echo, echo_port = await _upstream(_echo)
    async with echo, _proxy({f"localhost:{echo_port}"}) as port:
        reader, writer, status = await _request(port, f"CONNECT LocalHost:{echo_port} HTTP/1.1\r\n\r\n".encode())
        assert status.startswith(b"HTTP/1.1 200")
        assert await reader.readline() == b"\r\n"
        writer.write(b"ping")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(4), 5) == b"ping"
        writer.close()
    assert f"ALLOW localhost:{echo_port} CONNECT" in capsys.readouterr().out


async def test_connect_to_denied_target_is_403(capsys):
    assert await _status(set(), b"CONNECT 127.0.0.1:9 HTTP/1.1\r\n\r\n") == b"403"
    assert capsys.readouterr().out.splitlines() == ["DENY 127.0.0.1:9 CONNECT"]


async def test_connect_to_allowlisted_closed_port_is_502(capsys):
    closed = _free_port()
    assert await _status({f"127.0.0.1:{closed}"}, f"CONNECT 127.0.0.1:{closed} HTTP/1.1\r\n\r\n".encode()) == b"502"
    assert f"FAIL 127.0.0.1:{closed}" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("CONNECT 127.0.0.1 HTTP/1.1", b"400"),
        ("CONNECT 127.0.0.1:abc HTTP/1.1", b"400"),
        ("CONNECT 127.0.0.1:70000 HTTP/1.1", b"400"),
        ("CONNECT [::1 HTTP/1.1", b"400"),
        ("CONNECT [::1]:443 HTTP/1.1", b"403"),
        ("GET http://example.com/ HTTP/1.1", b"403"),
        ("GET https://127.0.0.1:443/ HTTP/1.1", b"400"),
        ("GET http://127.0.0.1:443@evil.example/ HTTP/1.1", b"403"),
        ("GET http://127.0.0.1:443:443/ HTTP/1.1", b"403"),
        ("GET http://evil%2eexample/ HTTP/1.1", b"403"),
        ("GET http://evil.example./ HTTP/1.1", b"403"),
        ("GET http://[::1%25eth0]/ HTTP/1.1", b"403"),
    ],
)
async def test_unallowed_or_unparseable_authorities_are_refused(line: str, expected: bytes):
    assert await _status({"127.0.0.1:443"}, f"{line}\r\n\r\n".encode()) == expected


async def test_absolute_form_https_is_400_with_explanation(capsys):
    assert await _status({"example.com:443"}, b"GET https://example.com/ HTTP/1.1\r\n\r\n") == b"400"
    assert "BAD example.com:443 absolute-form https (use CONNECT)" in capsys.readouterr().out


async def test_absolute_form_get_is_rewritten_to_origin_form(capsys):
    received: list[bytes] = []

    async def _http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello")
        await writer.drain()
        writer.close()

    upstream, up_port = await _upstream(_http)
    head = (
        f"GET http://user:pw@127.0.0.1:{up_port}/path?q=1 HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{up_port}\r\nProxy-Connection: keep-alive\r\nProxy-Authorization: x\r\n"
        "Connection: keep-alive\r\nAccept: */*\r\n\r\n"
    ).encode()
    async with upstream, _proxy({f"127.0.0.1:{up_port}"}) as port:
        reader, writer, status = await _request(port, head)
        body = await asyncio.wait_for(reader.read(), 5)
        writer.close()
    assert status.startswith(b"HTTP/1.1 200")
    assert body.endswith(b"hello")
    forwarded = received[0].decode()
    assert forwarded.startswith("GET /path?q=1 HTTP/1.1\r\n")
    assert "Proxy-" not in forwarded
    assert "keep-alive" not in forwarded
    assert "Connection: close\r\n" in forwarded
    assert "Accept: */*" in forwarded
    assert f"ALLOW 127.0.0.1:{up_port} GET" in capsys.readouterr().out


@pytest.mark.parametrize(
    "line",
    [
        b"garbage",
        b"GET /relative HTTP/1.1",
        b"GET http://u:SECRET@h:99999/?token=SECRET HTTP/1.1",
        b"X\nALLOW\tevil.com:443\tCONNECT http://evil.com/ HTTP/1.1",
        b"CONNECT evil.com:443 HTTP/1.1\x00",
        b"GET http://evil.com/\xff HTTP/1.1",
        b"GET http://evil.com/ HTTP/1.1\tX",
    ],
)
async def test_a_bad_request_line_is_one_redacted_log_line(line: bytes, capsys):
    assert await _status(set(), line + b"\r\n\r\n") == b"400"
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 1
    assert out.startswith("BAD ")
    assert "SECRET" not in out


async def test_connections_beyond_the_cap_get_503(monkeypatch, capsys):
    monkeypatch.setattr(egress_proxy, "MAX_CONNECTIONS", 1)
    async with _proxy(set()) as port:
        _held_reader, held_writer = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.sleep(0.05)
        _reader, writer, status = await _request(port, b"CONNECT a.example:443 HTTP/1.1\r\n\r\n")
        writer.close()
        held_writer.close()
    assert status.startswith(b"HTTP/1.1 503")
    assert "BAD too-many-connections" in capsys.readouterr().out


async def test_oversized_head_closes_the_connection(capsys):
    async with _proxy(set()) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET http://x/ HTTP/1.1\r\nX: " + b"a" * (egress_proxy.HEAD_LIMIT_BYTES + 10))
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        assert await asyncio.wait_for(reader.read(), 5) == b""
        writer.close()
    assert "BAD head-too-large" in capsys.readouterr().out


async def test_connect_half_close_still_delivers_the_response():
    async def _reply_after_eof(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"got:" + await reader.read())
        await writer.drain()
        writer.close()

    upstream, up_port = await _upstream(_reply_after_eof)
    async with upstream, _proxy({f"127.0.0.1:{up_port}"}) as port:
        reader, writer, status = await _request(port, f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n\r\n".encode())
        assert status.startswith(b"HTTP/1.1 200")
        assert await reader.readline() == b"\r\n"
        writer.write(b"question")
        writer.write_eof()
        assert await asyncio.wait_for(reader.read(), 5) == b"got:question"
        writer.close()


async def test_upstream_closing_mid_stream_closes_the_client():
    async def _one_shot(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"data: 1\n\n")
        await writer.drain()
        writer.close()

    upstream, up_port = await _upstream(_one_shot)
    async with upstream, _proxy({f"127.0.0.1:{up_port}"}) as port:
        reader, writer, status = await _request(port, f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n\r\n".encode())
        assert status.startswith(b"HTTP/1.1 200")
        rest = await asyncio.wait_for(reader.read(), 5)
        writer.close()
    assert rest == b"\r\ndata: 1\n\n"


async def test_probe_succeeds_only_when_every_target_is_allowed(monkeypatch, capsys):
    echo, echo_port = await _upstream(_echo)
    async with echo, _proxy({f"127.0.0.1:{echo_port}"}) as port:
        ok = await egress_proxy.probe(f"127.0.0.1:{port}", [f"127.0.0.1:{echo_port}"], 5)
        mixed = await egress_proxy.probe(f"127.0.0.1:{port}", [f"127.0.0.1:{echo_port}", "denied.example:443"], 5)
    monkeypatch.setattr(egress_proxy, "PROBE_STARTUP_RETRY_SECONDS", 0.3)
    unreachable = await egress_proxy.probe(f"127.0.0.1:{_free_port()}", ["a.example:443"], 1)
    out = capsys.readouterr().out
    assert (ok, mixed, unreachable) == (0, 1, 1)
    assert f"OK 127.0.0.1:{echo_port}" in out
    assert "FAIL denied.example:443 HTTP/1.1 403 Forbidden" in out
    assert "FAIL a.example:443 proxy unreachable" in out


async def test_watchdog_stops_serve_on_a_stale_or_missing_heartbeat(tmp_path, capsys):
    heartbeat = tmp_path / "hb"
    heartbeat.write_text("1", encoding="utf-8")
    started = time.monotonic()
    await asyncio.wait_for(egress_proxy.serve(frozenset(), "127.0.0.1", 0, str(heartbeat), 1.0), 5)
    assert time.monotonic() - started < 3.5
    await asyncio.wait_for(egress_proxy.watch_heartbeat(str(tmp_path / "absent"), 0.5), 5)
    assert capsys.readouterr().out.count("STALE") == 2


async def test_watchdog_stays_alive_while_the_counter_advances(tmp_path):
    heartbeat = tmp_path / "hb"
    heartbeat.write_text("0", encoding="utf-8")
    watcher = asyncio.create_task(egress_proxy.watch_heartbeat(str(heartbeat), 1.0))
    for counter in range(1, 15):
        await asyncio.to_thread(heartbeat.write_text, str(counter), encoding="utf-8")
        await asyncio.sleep(0.2)
    assert not watcher.done()
    watcher.cancel()
    await asyncio.gather(watcher, return_exceptions=True)


@pytest.mark.parametrize(
    ("current", "last", "mtime", "last_mtime"),
    [
        ("", "", 0.0, 0.0),
        ("", "", 5.0, 0.0),
        ("1", "", 0.0, 0.0),
        ("1", "1", 5.0, 5.0),
        ("2", "1", 5.0, 5.0),
        ("1", "1", 6.0, 5.0),
        ("", "1", 5.0, 5.0),
        ("1", "1", 4.0, 5.0),
    ],
)
def test_liveness_rule_matches_the_container_watchdog(current: str, last: str, mtime: float, last_mtime: float):
    assert egress_proxy.heartbeat_alive(current, last, mtime, last_mtime) == heartbeat_is_alive(
        current, last, mtime, last_mtime
    )


def test_module_imports_only_the_five_stdlib_modules():
    roots: set[str] = set()
    for node in ast.walk(ast.parse(PROXY_FILE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "egress_proxy.py must not use a relative import"
            assert node.module is not None
            roots.add(node.module.split(".")[0])
    assert roots <= ALLOWED_IMPORTS


def test_runs_standalone_in_isolated_mode():
    result = subprocess.run(
        [sys.executable, "-I", str(PROXY_FILE), "--help"], capture_output=True, text=True, encoding="utf-8", timeout=20
    )
    assert result.returncode == 0
    assert "serve" in result.stdout
    assert "probe" in result.stdout


@pytest.mark.parametrize(
    ("extra", "named"),
    [
        (["--listen", "nonsense"], "BAD --listen"),
        *(
            (["--allow", entry], "BAD --allow")
            for entry in ["example.com", "[::1]:443", "a.example:443:1", "a.example:0"]
        ),
        (["--heartbeat", "hb"], "--stale"),
        *((["--heartbeat", "hb", "--stale", stale], "--stale") for stale in ["0", "nan", "inf"]),
    ],
)
def test_main_refuses_invalid_arguments(extra: list[str], named: str, capsys):
    assert egress_proxy.main(["serve", "--listen", f"127.0.0.1:{_free_port()}", *extra]) == 2
    assert named in capsys.readouterr().out


def test_main_serves_until_the_heartbeat_is_stale(tmp_path, capsys):
    heartbeat = tmp_path / "hb"
    heartbeat.write_text("1", encoding="utf-8")
    argv = ["serve", "--listen", f"127.0.0.1:{_free_port()}", "--heartbeat", str(heartbeat), "--stale", "0.5"]
    assert egress_proxy.main([*argv, "--allow", "A.example:443"]) == 0
    out = capsys.readouterr().out
    assert "allow=a.example:443" in out
    assert "STALE" in out
