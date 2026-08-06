"""Constants and endpoint helpers shared by the protected mock client, server, and runtime.

The service endpoint is chosen per run at server start: an AF_UNIX socket in the
run's scratch directory when the platform supports it (probed with an actual
bind), else TCP on 127.0.0.1 with an ephemeral port. Either way the client must
present the run's random token in each request. The token is same-user hygiene
(it keeps other local processes from casually querying the service), not a
security boundary: the agent can read it out of its own shim.
"""

from __future__ import annotations

from typing import Literal


PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
CLIENT_TIMEOUT_SECONDS = 5.0

# Environment variables the generated shim sets for the client process. The shim
# bakes the values in itself -- the client never relies on the agent process
# environment for its connection details.
ENDPOINT_ENV = "CODER_EVAL_PROTECTED_MOCK_ENDPOINT"
TOKEN_ENV = "CODER_EVAL_PROTECTED_MOCK_TOKEN"
CALL_LOG_ENV = "CODER_EVAL_MOCK_CALL_LOG"

# Files inside the per-run runtime (scratch) directory. The token file is written
# by the runtime before the server starts; the endpoint file is written by the
# server once bound (atomically, via rename), and doubles as the readiness signal.
TOKEN_FILE_NAME = "token"
ENDPOINT_FILE_NAME = "endpoint"
SOCKET_FILE_NAME = "mock.sock"


def format_unix_endpoint(path: str) -> str:
    """Render an AF_UNIX endpoint string (``unix:<socket path>``)."""
    return f"unix:{path}"


def format_tcp_endpoint(host: str, port: int) -> str:
    """Render a TCP loopback endpoint string (``tcp:<host>:<port>``)."""
    return f"tcp:{host}:{port}"


def parse_endpoint(value: str) -> tuple[Literal["unix"], str] | tuple[Literal["tcp"], tuple[str, int]]:
    """Parse an endpoint string into ``("unix", path)`` or ``("tcp", (host, port))``.

    The ``unix:`` payload is taken verbatim (Windows socket paths contain a
    drive colon); the ``tcp:`` payload splits on the last colon.

    Raises:
        ValueError: The string is not a recognized endpoint.
    """
    if value.startswith("unix:"):
        path = value[len("unix:") :]
        if not path:
            raise ValueError(f"invalid unix endpoint: {value!r}")
        return ("unix", path)
    if value.startswith("tcp:"):
        host, sep, port_text = value[len("tcp:") :].rpartition(":")
        if not sep or not host:
            raise ValueError(f"invalid tcp endpoint: {value!r}")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ValueError(f"invalid tcp endpoint port: {value!r}") from exc
        if not 0 < port <= 65535:
            raise ValueError(f"invalid tcp endpoint port: {value!r}")
        return ("tcp", (host, port))
    raise ValueError(f"unrecognized protected mock endpoint: {value!r}")
