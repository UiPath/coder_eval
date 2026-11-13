"""The real bundled codex binary accepts the custom provider and routes a turn through it.

Starts the app-server against a local HTTP stub, with no network and a throw-away CODEX_HOME.
The ``app-server-only`` case strips the provider from the thread options, so only the
app-server seam can reach the stub; ``app-server-and-thread`` sends the production shape.
Skips when the codex extra or its binary is absent.
"""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, NamedTuple

import pytest

from coder_eval.agents.codex_agent import CodexAgent
from coder_eval.models import AgentKind, parse_agent_config


codex_client = pytest.importorskip("openai_codex.client")
codex_cli_bin = pytest.importorskip("codex_cli_bin")
try:
    codex_cli_bin.bundled_codex_path()
except FileNotFoundError:
    pytest.skip("openai-codex-cli-bin is installed without its codex binary", allow_module_level=True)

pytestmark = pytest.mark.integration

_STUB_ERROR = b'{"error":{"message":"stub","type":"invalid_request_error"}}'
_SCENARIO_TIMEOUT_S = 60


class _Request(NamedTuple):
    path: str
    authorization: str | None


def _start_stub(requests: list[_Request]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _reply(self) -> None:
            requests.append(_Request(self.path, self.headers.get("Authorization")))
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(_STUB_ERROR)))
            self.end_headers()
            self.wfile.write(_STUB_ERROR)

        def do_GET(self) -> None:
            self._reply()

        def do_POST(self) -> None:
            self._reply()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _track_clients(monkeypatch) -> list[Any]:
    """Record every ``CodexClient`` that spawns an app-server, so a hung one can be reaped."""
    clients: list[Any] = []
    original_start = codex_client.CodexClient.start

    def start(self: Any) -> None:
        clients.append(self)
        original_start(self)

    monkeypatch.setattr(codex_client.CodexClient, "start", start)
    return clients


@pytest.mark.parametrize("thread_provider", [False, True], ids=["app-server-only", "app-server-and-thread"])
def test_provider_routes_turn_to_custom_endpoint(monkeypatch, tmp_path, thread_provider):
    requests: list[_Request] = []
    errors: list[BaseException] = []
    server = _start_stub(requests)
    try:
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.setenv("CODEX_API_KEY", "dummy")
        monkeypatch.setenv("CODEX_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}/v1")
        monkeypatch.setenv("CODEX_API_VERSION", "2025-04-01-preview")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        clients = _track_clients(monkeypatch)
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="stub-model"))
        if not thread_provider:
            build_thread_options = agent._build_thread_options

            def thread_options_without_provider() -> dict:
                options = build_thread_options()
                options.pop("model_provider")
                options["config"].pop("model_providers")
                return options

            monkeypatch.setattr(agent, "_build_thread_options", thread_options_without_provider)

        async def scenario() -> None:
            await agent.start(str(tmp_path))
            try:
                await agent.communicate("say hi", timeout=20)
            except Exception as exc:
                errors.append(exc)
            finally:
                await agent.stop()

        def run() -> None:
            try:
                asyncio.run(scenario())
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout=_SCENARIO_TIMEOUT_S)
        if worker.is_alive():
            agent.kill_sync()
            for client in clients:
                client.close()
            worker.join(timeout=10)
            pytest.fail(
                f"codex app-server scenario did not finish within {_SCENARIO_TIMEOUT_S} s; "
                + f"stub saw {requests}; errors: {errors!r}"
            )
    finally:
        server.shutdown()
        server.server_close()

    routed = [r for r in requests if r.path.startswith("/v1/responses") and "api-version=2025-04-01-preview" in r.path]
    assert routed, f"stub saw {requests}; errors: {errors!r}"
    assert all(r.authorization == "Bearer dummy" for r in routed), f"stub saw {requests}"
