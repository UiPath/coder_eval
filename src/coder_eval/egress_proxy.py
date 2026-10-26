"""Stdlib-only egress proxy for ``network: llm_only``; it runs inside the egress sidecar.

``serve`` forwards ``CONNECT host:port`` and absolute-form plain HTTP only to an exact
allowlisted ``host:port`` and never originates TLS. Every decision is one stdout line
(``ALLOW``, ``DENY``, ``FAIL``, ``BAD``, ``STALE``, ``PHASE``) after one ``READY`` line.
``--heartbeat`` stops it when the host heartbeat goes stale. ``--control`` listens on
loopback for one ``GRADING`` line that logs ``PHASE grading`` and adds ``--grading-allow``
(or any host, with ``--grading-allow-any``); the switch is one-way.

``probe`` checks each target through a running proxy (or, with ``--direct``, by a
TCP connection) and exits 0 only when every target is OK. ``grading`` sends the
``GRADING`` line to a ``--control`` address and exits 0 once the proxy confirms.

The host runs this file with ``python3 -I``, so it imports only ``argparse``,
``asyncio``, ``os``, ``sys`` and ``time``. Importing it starts nothing.

Rationale: .claude/notes/isolation.md § The egress sidecar (network: llm_only)
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
MAX_CONNECTIONS = 256
MAX_HEARTBEAT_POLL_SECONDS = 2.0
PROBE_STARTUP_RETRY_SECONDS = 5.0

_BAD_REQUEST = b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_FORBIDDEN = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_BAD_GATEWAY = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_UNAVAILABLE = b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"
_CONTROL_GRADING = b"GRADING\n"
_CONTROL_OK = b"OK grading\n"


def _log(line: str) -> None:
    print(line, flush=True)


class Policy:
    """Where the proxy forwards: ``allow`` from the start, plus the grading hosts once grading starts."""

    def __init__(
        self, allow: frozenset[str], grading_allow: frozenset[str] = frozenset(), grading_any: bool = False
    ) -> None:
        self.allow = allow
        self.grading_allow = grading_allow
        self.grading_any = grading_any
        self.grading = False

    def permits(self, destination: str) -> bool:
        if destination in self.allow:
            return True
        return self.grading and (self.grading_any or destination in self.grading_allow)

    def describe_grading(self) -> str:
        if self.grading_any:
            return "*"
        return "+" + ",".join(sorted(self.grading_allow)) if self.grading_allow else "unchanged"


def _redacted(request_line: bytes) -> str:
    """A refused request line, shown as its method and length only: the target may carry credentials."""
    method = request_line.split(b" ", 1)[0]
    shown = method.decode("ascii") if method.isalpha() and method.isascii() and len(method) <= 16 else "?"
    return f"{shown} unparseable request line ({len(request_line)} bytes)"


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
    policy: Policy, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
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
        _log(f"BAD {_redacted(request_line)}")
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
        _log(f"BAD {_redacted(request_line)}")
        await _reply_and_close(client_writer, _BAD_REQUEST)
        return
    host, port = parsed
    destination = f"{host}:{port}"
    if not policy.permits(destination):
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


async def start_proxy(policy: Policy, host: str, port: int) -> asyncio.Server:
    """Start listening; the returned server is already accepting connections.

    At most ``MAX_CONNECTIONS`` clients are served at once; one more gets ``503``.
    """
    active = 0

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal active
        if active >= MAX_CONNECTIONS:
            _log("BAD too-many-connections")
            await _reply_and_close(writer, _UNAVAILABLE)
            return
        active += 1
        try:
            await handle_client(policy, reader, writer)
        finally:
            active -= 1

    return await asyncio.start_server(_handle, host, port, limit=HEAD_LIMIT_BYTES)


async def start_control(policy: Policy, host: str, port: int) -> asyncio.Server:
    """Listen for the one-way ``GRADING`` switch; anything else is refused and changes nothing."""

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), HEAD_TIMEOUT_SECONDS)
        except (TimeoutError, ConnectionError, OSError):
            writer.close()
            return
        if line != _CONTROL_GRADING:
            _log("BAD control request")
            await _reply_and_close(writer, b"BAD\n")
            return
        if not policy.grading:
            policy.grading = True
            _log(f"PHASE grading allow={policy.describe_grading()}")
        await _reply_and_close(writer, _CONTROL_OK)

    return await asyncio.start_server(_handle, host, port)


async def serve(
    policy: Policy,
    host: str,
    port: int,
    heartbeat: str | None,
    stale_seconds: float,
    control: tuple[str, int] | None = None,
) -> None:
    """Run the proxy until the heartbeat goes stale (forever when ``heartbeat`` is None)."""
    server = await start_proxy(policy, host, port)
    control_server = None if control is None else await start_control(policy, *control)
    grading = f" grading={policy.describe_grading()}" if control is not None else ""
    _log(f"READY {host}:{port} allow={','.join(sorted(policy.allow))}{grading}")
    try:
        if heartbeat is None:
            await server.serve_forever()
        else:
            await watch_heartbeat(heartbeat, stale_seconds)
    finally:
        server.close()
        server.abort_clients()
        if control_server is not None:
            control_server.close()


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


async def _dial_one(target: str, timeout: float) -> str | None:
    parsed = split_host_port(target, None)
    if parsed is None:
        return "invalid target (expected host:port)"
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(*parsed), timeout)
    except (OSError, TimeoutError) as exc:
        return repr(exc)
    writer.close()
    return None


async def probe(proxy: str | None, targets: list[str], timeout: float) -> int:
    """CONNECT to each target through ``proxy`` (dial it directly when None); 0 when all succeed, else 1."""
    if proxy is None:
        errors = await asyncio.gather(*(_dial_one(target, timeout) for target in targets))
    else:
        parsed = split_host_port(proxy, None)
        if parsed is None:
            _log(f"FAIL {proxy} invalid --proxy (expected host:port)")
            return 1
        errors = await asyncio.gather(*(_probe_one(*parsed, target, timeout) for target in targets))
    for target, error in zip(targets, errors, strict=True):
        _log(f"OK {target}" if error is None else f"FAIL {target} {error}")
    return 0 if all(error is None for error in errors) else 1


async def request_grading(control: str, timeout: float) -> int:
    """Switch the proxy listening on ``control`` to grading; 0 once it confirms, else 1."""
    parsed = split_host_port(control, None)
    if parsed is None:
        _log(f"FAIL {control} invalid --control (expected host:port)")
        return 1
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(*parsed), timeout)
    except (OSError, TimeoutError) as exc:
        _log(f"FAIL {control} control unreachable: {exc!r}")
        return 1
    try:
        writer.write(_CONTROL_GRADING)
        await writer.drain()
        reply = await asyncio.wait_for(reader.readline(), timeout)
    except (OSError, TimeoutError) as exc:
        _log(f"FAIL {control} {exc!r}")
        return 1
    finally:
        writer.close()
    _log(reply.decode("latin-1").strip() or "FAIL no reply")
    return 0 if reply == _CONTROL_OK else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="egress_proxy", description=__doc__.split("\n", 1)[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    serve_cmd = commands.add_parser("serve", help="run the allowlisting proxy")
    serve_cmd.add_argument("--listen", default="0.0.0.0:3128", help="host:port to listen on")
    serve_cmd.add_argument("--heartbeat", default=None, help="host heartbeat file; stop when it goes stale")
    serve_cmd.add_argument("--stale", type=float, default=None, help="seconds without a heartbeat change")
    serve_cmd.add_argument("--allow", action="append", default=[], help="allowed host:port (repeatable)")
    serve_cmd.add_argument("--control", default=None, help="loopback host:port for the GRADING switch")
    serve_cmd.add_argument(
        "--grading-allow", action="append", default=[], help="host:port also allowed after GRADING (repeatable)"
    )
    serve_cmd.add_argument("--grading-allow-any", action="store_true", help="allow any host:port after GRADING")
    probe_cmd = commands.add_parser("probe", help="CONNECT to each target through a running proxy")
    via = probe_cmd.add_mutually_exclusive_group(required=True)
    via.add_argument("--proxy", help="proxy host:port")
    via.add_argument("--direct", action="store_true", help="open a TCP connection to each target instead")
    probe_cmd.add_argument("--timeout", type=float, default=5.0, help="per-target timeout in seconds")
    probe_cmd.add_argument("targets", nargs="+", help="host:port targets")
    grading_cmd = commands.add_parser("grading", help="switch a running proxy to its grading allowlist")
    grading_cmd.add_argument("--control", required=True, help="the proxy's --control host:port")
    grading_cmd.add_argument("--timeout", type=float, default=5.0, help="timeout in seconds")
    return parser


def _parse_allow(entries: list[str], flag: str) -> set[str] | None:
    allow: set[str] = set()
    for entry in entries:
        parsed = None if "[" in entry else split_host_port(entry.strip(), None)
        if parsed is None or ":" in parsed[0]:
            _log(f"BAD {flag} {entry!r} (expected host:port)")
            return None
        allow.add(f"{parsed[0]}:{parsed[1]}")
    return allow


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point; returns the process exit code."""
    args = _build_parser().parse_args(argv)
    if args.command == "probe":
        return asyncio.run(probe(None if args.direct else args.proxy, args.targets, args.timeout))
    if args.command == "grading":
        return asyncio.run(request_grading(args.control, args.timeout))
    listen = split_host_port(args.listen, None)
    if listen is None:
        _log(f"BAD --listen {args.listen!r} (expected host:port)")
        return 2
    if args.heartbeat is not None and not (args.stale is not None and 0 < args.stale < float("inf")):
        _log("BAD --heartbeat needs a finite, positive --stale")
        return 2
    control = None if args.control is None else split_host_port(args.control, None)
    if args.control is not None and (control is None or control[0] not in ("127.0.0.1", "localhost")):
        _log(f"BAD --control {args.control!r} (expected a loopback host:port)")
        return 2
    if (args.grading_allow or args.grading_allow_any) and control is None:
        _log("BAD --grading-allow and --grading-allow-any need --control")
        return 2
    allow = _parse_allow(args.allow, "--allow")
    grading_allow = _parse_allow(args.grading_allow, "--grading-allow")
    if allow is None or grading_allow is None:
        return 2
    policy = Policy(frozenset(allow), frozenset(grading_allow), args.grading_allow_any)
    asyncio.run(serve(policy, listen[0], listen[1], args.heartbeat, args.stale or 0.0, control))
    return 0


if __name__ == "__main__":
    sys.exit(main())
