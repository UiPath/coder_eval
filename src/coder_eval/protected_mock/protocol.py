"""Constants shared by the protected mock client, server, and runtime."""

from __future__ import annotations


PROTOCOL_VERSION = 1
SOCKET_PATH = "/run/coder-eval/uip.sock"
CLIENT_EXECUTABLE = "/usr/local/bin/coder_eval_mock_client"
SERVER_LAUNCHER = "/usr/local/bin/coder_eval_mockd.sh"
CONTAINER_FIXTURE_DIR = "/opt/coder-eval/mock/fixtures"
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
CLIENT_TIMEOUT_SECONDS = 5.0

