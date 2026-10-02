"""Egress probes for ``network: llm_only``: each check exits 0 when the container behaves as the mode promises.

Run as ``python3 egress_probe.py <check>`` from a ``run_command`` criterion, or with
``all`` for a table of every check. A ``blocked_*`` check passes when the path out is
closed; a ``model_*`` check passes when the model API stays reachable.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit


TIMEOUT = 6.0
OUTSIDE_HOST = "example.com"
OUTSIDE_IPV4 = "1.1.1.1"
OUTSIDE_IPV6 = "2606:4700:4700::1111"


def _proxy() -> tuple[str, int]:
    parts = urlsplit(os.environ.get("HTTPS_PROXY", ""))
    if not parts.hostname or parts.port is None:
        raise RuntimeError("HTTPS_PROXY is not set: this is not an llm_only container")
    return parts.hostname, parts.port


def _connect_status(target: str) -> str:
    with socket.create_connection(_proxy(), TIMEOUT) as sock:
        sock.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        return sock.recv(256).split(b"\r\n", 1)[0].decode("latin-1")


def _raw_status(request: bytes) -> str:
    with socket.create_connection(_proxy(), TIMEOUT) as sock:
        sock.sendall(request)
        return sock.recv(256).split(b"\r\n", 1)[0].decode("latin-1")


def _model_target() -> tuple[str, int] | None:
    """The ``(host, port)`` the agent's model calls go to, or None when the backend is not known here."""
    backend = os.environ.get("API_BACKEND", "direct")
    if backend == "bedrock" and os.environ.get("AWS_REGION"):
        return f"bedrock-runtime.{os.environ['AWS_REGION'].lower()}.amazonaws.com", 443
    if backend == "direct":
        return "api.anthropic.com", 443
    parts = urlsplit(os.environ.get("LITELLM_BASE_URL", ""))
    if backend == "litellm" and parts.hostname:
        return parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
    return None


def _tcp_refused(host: str, port: int, family: int = socket.AF_INET) -> str | None:
    """None when no connection could be made, else a description of the open path."""
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(TIMEOUT)
            sock.connect((host, port))
    except OSError:
        return None
    return f"connected to {host}:{port}"


def _command_fails(argv: list[str], timeout: float = 60) -> str | None:
    if shutil.which(argv[0]) is None:
        return None
    env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", GIT_TERMINAL_PROMPT="0")
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, cwd="/tmp")
    except subprocess.TimeoutExpired:
        return None
    return None if done.returncode != 0 else f"{' '.join(argv)} succeeded"


def blocked_proxy_https() -> str | None:
    status = _connect_status(f"{OUTSIDE_HOST}:443")
    return None if " 403 " in f"{status} " else f"proxy answered {status!r} for {OUTSIDE_HOST}:443"


def blocked_proxy_http() -> str | None:
    request = f"GET http://{OUTSIDE_HOST}/ HTTP/1.1\r\nHost: {OUTSIDE_HOST}\r\n\r\n".encode()
    status = _raw_status(request)
    return None if " 403 " in f"{status} " else f"proxy answered {status!r} for http://{OUTSIDE_HOST}/"


def blocked_proxy_absolute_https() -> str | None:
    request = f"GET https://{OUTSIDE_HOST}/ HTTP/1.1\r\nHost: {OUTSIDE_HOST}\r\n\r\n".encode()
    status = _raw_status(request)
    return None if status.split(" ")[1:2] in (["400"], ["403"]) else f"proxy answered {status!r}"


def blocked_urllib_through_env_proxy() -> str | None:
    try:
        urllib.request.urlopen(f"https://{OUTSIDE_HOST}/", timeout=TIMEOUT)
    except (urllib.error.URLError, OSError):
        return None
    return f"urllib fetched https://{OUTSIDE_HOST}/"


def blocked_direct_ipv4() -> str | None:
    return _tcp_refused(OUTSIDE_IPV4, 443) or _tcp_refused(OUTSIDE_IPV4, 80)


def blocked_direct_ipv6() -> str | None:
    if not socket.has_ipv6:
        return None
    return _tcp_refused(OUTSIDE_IPV6, 443, socket.AF_INET6)


def blocked_default_route() -> str | None:
    with open("/proc/net/route", encoding="ascii") as routes:
        defaults = [line for line in routes.read().splitlines()[1:] if line.split()[1:2] == ["00000000"]]
    return f"default route present: {defaults}" if defaults else None


def blocked_gateway_ip() -> str | None:
    with open("/proc/net/route", encoding="ascii") as routes:
        rows = [line.split() for line in routes.read().splitlines()[1:]]
    for row in rows:
        destination = socket.inet_ntoa(int(row[1], 16).to_bytes(4, "little"))
        gateway = ".".join([*destination.split(".")[:3], "1"])
        for port in (22, 80, 443, 2375, 4000):
            opened = _tcp_refused(gateway, port)
            if opened:
                return opened
    return None


def blocked_external_dns() -> str | None:
    try:
        addresses = socket.getaddrinfo(OUTSIDE_HOST, 443)
    except OSError:
        return None
    return f"{OUTSIDE_HOST} resolved to {sorted({a[4][0] for a in addresses})}"


def blocked_udp_dns_to_public_resolver() -> str | None:
    query = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(TIMEOUT)
        try:
            sock.sendto(query, ("8.8.8.8", 53))
            sock.recvfrom(512)
        except OSError:
            return None
    return "8.8.8.8 answered a DNS query"


def blocked_docker_host_alias() -> str | None:
    for name in ("host.docker.internal", "gateway.docker.internal"):
        try:
            socket.getaddrinfo(name, 80)
        except OSError:
            continue
        return f"{name} resolves"
    return None


def blocked_raw_socket() -> str | None:
    try:
        socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP).close()
    except PermissionError:
        return None
    return "a raw ICMP socket was opened (NET_RAW is not dropped)"


def blocked_sidecar_other_ports() -> str | None:
    host, proxy_port = _proxy()
    for port in (22, 80, 443, 8080):
        if port != proxy_port and (opened := _tcp_refused(host, port)):
            return opened
    return None


def blocked_curl_noproxy() -> str | None:
    return _command_fails(["curl", "-sS", "-m", "8", "--noproxy", "*", "-o", "/dev/null", f"https://{OUTSIDE_HOST}"])


def blocked_curl() -> str | None:
    return _command_fails(["curl", "-sS", "-m", "8", "-o", "/dev/null", f"https://{OUTSIDE_HOST}"])


def blocked_pip_download() -> str | None:
    argv = ["pip", "download", "--no-deps", "--retries", "0", "--timeout", "5", "-d", "/tmp/pip-probe", "six"]
    return _command_fails(argv)


def blocked_git_clone() -> str | None:
    return _command_fails(["git", "clone", "--depth", "1", "https://github.com/octocat/Hello-World", "/tmp/git-probe"])


def blocked_npm_view() -> str | None:
    return _command_fails(["npm", "view", "left-pad", "version", "--fetch-retries=0", "--fetch-timeout=5000"])


def model_host_reachable() -> str | None:
    target = _model_target()
    if target is None:
        return "the model backend is not known to this probe"
    status = _connect_status(f"{target[0]}:{target[1]}")
    return None if " 200 " in f"{status} " else f"proxy answered {status!r} for the model host {target[0]}:{target[1]}"


def model_host_other_port_blocked() -> str | None:
    target = _model_target()
    if target is None:
        return "the model backend is not known to this probe"
    host, port = target
    other = 8 if port != 8 else 9
    status = _connect_status(f"{host}:{other}")
    return None if " 403 " in f"{status} " else f"proxy answered {status!r} for {host}:{other}"


CHECKS = {name: fn for name, fn in globals().items() if name.startswith(("blocked_", "model_")) and callable(fn)}


def main(argv: list[str]) -> int:
    names = list(CHECKS) if argv[1:] == ["all"] else argv[1:]
    failed = 0
    for name in names:
        try:
            problem = CHECKS[name]()
        except Exception as exc:
            problem = f"probe error: {exc!r}"
        print(f"{'PASS' if problem is None else 'FAIL'} {name}{'' if problem is None else ': ' + problem}")
        failed += problem is not None
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
