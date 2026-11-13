"""The real bundled codex binary accepts the app-server provider overrides and routes a turn through them.

Starts the app-server against a local HTTP stub, with no network and a throw-away CODEX_HOME. The
thread options lose their provider here, so only the app-server seam can reach the stub.
Skips when the codex extra or its binary is absent.
"""

import asyncio
import contextlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from coder_eval.agents.codex_agent import CodexAgent
from coder_eval.models import AgentKind, parse_agent_config


pytest.importorskip("openai_codex")
codex_cli_bin = pytest.importorskip("codex_cli_bin")
try:
    codex_cli_bin.bundled_codex_path()
except FileNotFoundError:
    pytest.skip("openai-codex-cli-bin is installed without its codex binary", allow_module_level=True)

pytestmark = pytest.mark.integration

_STUB_ERROR = b'{"error":{"message":"stub","type":"invalid_request_error"}}'


def _start_stub(paths: list[str]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _reply(self) -> None:
            paths.append(self.path)
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


def test_app_server_overrides_route_turn_to_custom_endpoint(monkeypatch, tmp_path):
    paths: list[str] = []
    server = _start_stub(paths)
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
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="stub-model"))
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
                with contextlib.suppress(Exception):
                    await agent.communicate("say hi", timeout=20)
            finally:
                await agent.stop()

        errors: list[BaseException] = []

        def run() -> None:
            try:
                asyncio.run(scenario())
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout=60)
        if worker.is_alive():
            agent.kill_sync()
            worker.join(timeout=5)
            pytest.fail("codex app-server scenario did not finish within 60 s")
    finally:
        server.shutdown()
        server.server_close()

    assert any(p.startswith("/v1/responses") and "api-version=2025-04-01-preview" in p for p in paths), (
        f"stub saw {paths}; scenario errors: {errors}"
    )
