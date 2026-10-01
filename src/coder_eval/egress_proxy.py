"""Stdlib-only egress proxy for ``network: llm_only``; it runs inside the egress sidecar.

``serve`` accepts HTTP ``CONNECT host:port`` and absolute-form plain HTTP
(``GET http://host/...``) and forwards only to an exact allowlisted ``host:port``.
It never originates TLS: an absolute-form ``https://`` request gets ``400``. Every
decision is one stdout line: ``ALLOW``, ``DENY``, ``FAIL``, ``BAD`` or ``STALE``,
after one ``READY`` line. A request line with a control or non-ASCII byte is refused,
so a client cannot forge a log line. With ``--heartbeat`` it stops by itself when the
host heartbeat file stops changing for ``--stale`` seconds.

``probe`` sends one ``CONNECT`` per target through a running proxy, prints
``OK target`` or ``FAIL target <reason>``, and exits 0 only when every target is OK.

The host bind-mounts this file into the framework image and runs it with
``python3 -I``, so it may import only ``argparse``, ``asyncio``, ``os``, ``sys`` and
``time``. Importing it starts nothing.
"""

import argparse
import asyncio
import os
import sys
import time


HEAD_LIMIT_BYTES = 64 * 1024
HEAD_TIMEOUT_SECONDS = 30.0
DIAL_TIMEOUT_SECONDS = 10.0
PIPE_CHUNK_BYTES = 64 * 1024
MAX_HEARTBEAT_POLL_SECONDS = 2.0
PROBE_STARTUP_RETRY_SECONDS = 5.0

_BAD_REQUEST = b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_FORBIDDEN = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_BAD_GATEWAY = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"


def _log(line: str) -> None:
    print(line, flush=True)


def _is_visible_ascii(data: bytes) -> bool:
    return all(0x20 <= byte < 0x7F for byte in data)


def _parse_port(text: str) -> int | None:
    if not (text.isascii() and text.isdigit()):
        return None
    port = int(text)
    return port if 1 <= port <= 65535 else None


def split_host_port(authority: str, default_port: int | None) -> tuple[str, int] | None:
    """Split ``host[:port]`` (or ``[v6]:port``) into a lowercased host and a port.

    Returns None when the host is empty, the port is invalid, or a port is
    required (``default_port is None``) and absent.
    """
    if authority.startswith("["):
        end = authority.find("]")
        if end < 0:
            return None
        host, rest = authority[1:end], authority[end + 1 :]
        if rest and not rest.startswith(":"):
            return None
        port_text = rest[1:] if rest else ""
    else:
        host, sep, port_text = authority.rpartition(":")
        if not sep:
            host, port_text = authority, ""
    if not host:
        return None
    if not port_text:
        if default_port is None:
            return None
        return host.lower(), default_port
    port = _parse_port(port_text)
    return None if port is None else (host.lower(), port)


def heartbeat_alive(current: str, last_counter: str, current_mtime: float, last_mtime: float) -> bool:
    """True when the heartbeat counter text changed or its mtime advanced."""
    return bool(current and current != last_counter) or current_mtime > last_mtime


def _read_heartbeat(path: str) -> tuple[str, float]:
    try:
        with open(path, encoding="utf-8") as handle:
            current = handle.read()
    except (OSError, UnicodeDecodeError):
        current = ""
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        mtime = 0.0
    return current, mtime


async def watch_heartbeat(path: str, stale_seconds: float) -> None:
    """Return once the heartbeat at ``path`` has not changed for ``stale_seconds``."""
    poll_seconds = min(MAX_HEARTBEAT_POLL_SECONDS, stale_seconds / 4)
    last_counter, last_mtime = "", 0.0
    last_change = time.monotonic()
    while True:
        current, mtime = await asyncio.to_thread(_read_heartbeat, path)
        now = time.monotonic()
        if heartbeat_alive(current, last_counter, mtime, last_mtime):
            last_counter, last_mtime, last_change = current, mtime, now
        if now - last_change > stale_seconds:
            _log(f"STALE heartbeat {path} unchanged for more than {stale_seconds:g}s; stopping")
            return
        await asyncio.sleep(poll_seconds)


async def _reply_and_close(writer: asyncio.StreamWriter, response: bytes) -> None:
    try:
        writer.write(response)
        await writer.drain()
    except (ConnectionError, OSError) as exc:
        _log(f"BAD client gone before the reply: {exc!r}")
    finally:
        writer.close()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(PIPE_CHUNK_BYTES):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        writer.close()
        peer.close()


def _origin_form_head(method: str, path: str, version: str, header_block: bytes) -> bytes:
    kept = [
        line
        for line in header_block.split(b"\r\n")
        if line and not line.lower().startswith((b"proxy-", b"connection:"))
    ]
    request_line = f"{method} {path} {version}".encode("latin-1")
    return b"\r\n".join([request_line, *kept, b"Connection: close"]) + b"\r\n\r\n"


def _parse_absolute_http(target: str) -> tuple[str, int, str] | None:
    rest = target[len("http://") :]
    cut = min((i for i in (rest.find(c) for c in "/?#") if i >= 0), default=len(rest))
    authority, path = rest[:cut], rest[cut:]
    path = path.split("#", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    parsed = split_host_port(authority.rpartition("@")[2], 80)
    return None if parsed is None else (*parsed, path)


async def handle_client(
    allow: frozenset[str], client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
) -> None:
    """Serve one client connection: parse its request head, then tunnel, forward or refuse."""
    try:
        head = await asyncio.wait_for(client_reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_SECONDS)
    except asyncio.LimitOverrunError:
        _log("BAD head-too-large")
        client_writer.close()
        return
    except (asyncio.IncompleteReadError, TimeoutError, ConnectionError, OSError):
        client_writer.close()
        return
    request_line, _, header_block = head[:-4].partition(b"\r\n")
    fields = request_line.decode("latin-1").split(" ")
    if len(fields) != 3 or not _is_visible_ascii(request_line) or not fields[2].startswith("HTTP/"):
        _log(f"BAD {request_line!r}")
        await _reply_and_close(client_writer, _BAD_REQUEST)
        return
    method, target, version = fields
    prefix = b""
    if method == "CONNECT":
        parsed = split_host_port(target, None)
    elif target.lower().startswith("http://"):
        absolute = _parse_absolute_http(target)
        parsed = None if absolute is None else absolute[:2]
        if absolute is not None:
            prefix = _origin_form_head(method, absolute[2], version, header_block)
    elif target.lower().startswith("https://"):
        https = split_host_port(target[len("https://") :].split("/", 1)[0].rpartition("@")[2], 443)
        shown = f"{https[0]}:{https[1]}" if https else repr(target)
        _log(f"BAD {shown} absolute-form https (use CONNECT)")
        await _reply_and_close(client_writer, _BAD_REQUEST)
        return
    else:
        parsed = None
    if parsed is None:
        _log(f"BAD {request_line!r}")
        await _reply_and_close(client_writer, _BAD_REQUEST)
        return
    host, port = parsed
    destination = f"{host}:{port}"
    if destination not in allow:
        _log(f"DENY {destination} {method}")
        await _reply_and_close(client_writer, _FORBIDDEN)
        return
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), DIAL_TIMEOUT_SECONDS
        )
    except (OSError, TimeoutError) as exc:
        _log(f"FAIL {destination} {exc!r}")
        await _reply_and_close(client_writer, _BAD_GATEWAY)
        return
    _log(f"ALLOW {destination} {method}")
    try:
        if method == "CONNECT":
            client_writer.write(_ESTABLISHED)
            await client_writer.drain()
        else:
            upstream_writer.write(prefix)
            await upstream_writer.drain()
        await asyncio.gather(
            _pipe(client_reader, upstream_writer, client_writer),
            _pipe(upstream_reader, client_writer, upstream_writer),
        )
    except (ConnectionError, OSError) as exc:
        _log(f"FAIL {destination} {exc!r}")
    finally:
        upstream_writer.close()
        client_writer.close()


async def start_proxy(allow: frozenset[str], host: str, port: int) -> asyncio.Server:
    """Start listening; the returned server is already accepting connections."""

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_client(allow, reader, writer)

    return await asyncio.start_server(_handle, host, port, limit=HEAD_LIMIT_BYTES)


async def serve(allow: frozenset[str], host: str, port: int, heartbeat: str | None, stale_seconds: float) -> None:
    """Run the proxy until the heartbeat goes stale (forever when ``heartbeat`` is None)."""
    server = await start_proxy(allow, host, port)
    _log(f"READY {host}:{port} allow={','.join(sorted(allow))}")
    try:
        if heartbeat is None:
            await server.serve_forever()
        else:
            await watch_heartbeat(heartbeat, stale_seconds)
    finally:
        server.close()
        server.abort_clients()


async def _probe_one(proxy_host: str, proxy_port: int, target: str, timeout: float) -> str | None:
    started = time.monotonic()
    while True:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(proxy_host, proxy_port), timeout)
            break
        except (OSError, TimeoutError) as exc:
            if time.monotonic() - started >= PROBE_STARTUP_RETRY_SECONDS:
                return f"proxy unreachable: {exc!r}"
            await asyncio.sleep(0.2)
    try:
        writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode("latin-1"))
        await writer.drain()
        status_line = await asyncio.wait_for(reader.readline(), timeout)
    except (OSError, TimeoutError) as exc:
        return repr(exc)
    finally:
        writer.close()
    status = status_line.split()
    if len(status) >= 2 and status[1] == b"200":
        return None
    return status_line.decode("latin-1").strip() or "no response"


async def probe(proxy: str, targets: list[str], timeout: float) -> int:
    """CONNECT to each target through ``proxy``; 0 when all succeed, else 1."""
    parsed = split_host_port(proxy, None)
    if parsed is None:
        _log(f"FAIL {proxy} invalid --proxy (expected host:port)")
        return 1
    errors = await asyncio.gather(*(_probe_one(*parsed, target, timeout) for target in targets))
    for target, error in zip(targets, errors, strict=True):
        _log(f"OK {target}" if error is None else f"FAIL {target} {error}")
    return 0 if all(error is None for error in errors) else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="egress_proxy", description=__doc__.split("\n", 1)[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    serve_cmd = commands.add_parser("serve", help="run the allowlisting proxy")
    serve_cmd.add_argument("--listen", default="0.0.0.0:3128", help="host:port to listen on")
    serve_cmd.add_argument("--heartbeat", default=None, help="host heartbeat file; stop when it goes stale")
    serve_cmd.add_argument("--stale", type=float, default=None, help="seconds without a heartbeat change")
    serve_cmd.add_argument("--allow", action="append", default=[], help="allowed host:port (repeatable)")
    probe_cmd = commands.add_parser("probe", help="CONNECT to each target through a running proxy")
    probe_cmd.add_argument("--proxy", required=True, help="proxy host:port")
    probe_cmd.add_argument("--timeout", type=float, default=5.0, help="per-target timeout in seconds")
    probe_cmd.add_argument("targets", nargs="+", help="host:port targets")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point; returns the process exit code."""
    args = _build_parser().parse_args(argv)
    if args.command == "probe":
        return asyncio.run(probe(args.proxy, args.targets, args.timeout))
    listen = split_host_port(args.listen, None)
    if listen is None:
        _log(f"BAD --listen {args.listen!r} (expected host:port)")
        return 2
    if args.heartbeat is not None and not (args.stale is not None and 0 < args.stale < float("inf")):
        _log("BAD --heartbeat needs a finite, positive --stale")
        return 2
    allow: set[str] = set()
    for entry in args.allow:
        parsed = None if "[" in entry else split_host_port(entry.strip(), None)
        if parsed is None or ":" in parsed[0]:
            _log(f"BAD --allow {entry!r} (expected host:port)")
            return 2
        allow.add(f"{parsed[0]}:{parsed[1]}")
    asyncio.run(serve(frozenset(allow), listen[0], listen[1], args.heartbeat, args.stale or 0.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
