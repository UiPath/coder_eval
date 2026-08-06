"""Tests for the protected fixture-backed CLI service and its thin wrappers."""

from __future__ import annotations

import json
import socket
import socketserver
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from coder_eval.models import ProtectedMockConfig, SandboxConfig
from coder_eval.protected_mock import client
from coder_eval.protected_mock.protocol import (
    CALL_LOG_ENV,
    ENDPOINT_ENV,
    TOKEN_ENV,
    parse_endpoint,
)
from coder_eval.protected_mock.runtime import (
    ProtectedMockRuntime,
    fixture_digest,
    resolve_fixture_path,
)
from coder_eval.protected_mock.server import ProtectedMockServer, create_server, load_config
from coder_eval.sandbox import Sandbox


FIXTURE_MARKER = '{"errors":[]}'


def _fixture(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "responses": [
                    {
                        "argv": ["rpa", "get-errors", "--output", "json"],
                        "exit_code": 0,
                        "stdout": '{"errors":[]}\n',
                    }
                ],
                "default": {"exit_code": 2, "stderr": "not configured\n"},
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_config(config: Path, fixture: Path, max_requests: int = 10) -> Path:
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "tools": [{"tool": "uip", "fixture": str(fixture), "max_requests": max_requests}],
            }
        ),
        encoding="utf-8",
    )
    return config


def _fake_server(tools) -> MagicMock:
    fake = MagicMock()
    fake.tools = tools
    fake.budget_lock = threading.Lock()
    fake.passthrough_lock = threading.Lock()
    return fake


def _unix_transport_usable(tmp_path: Path) -> bool:
    af_unix = getattr(socket, "AF_UNIX", None)
    if af_unix is None or not hasattr(socketserver, "UnixStreamServer"):
        return False
    probe = tmp_path / "probe.sock"
    try:
        with socket.socket(af_unix, socket.SOCK_STREAM) as sock:
            sock.bind(str(probe))
        return True
    except OSError:
        return False
    finally:
        probe.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Model validation
# --------------------------------------------------------------------------- #


def test_protected_mocks_supported_under_tempdir(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "uip.json")
    config = SandboxConfig(
        driver="tempdir",
        protected_mocks=[ProtectedMockConfig(tool="uip", fixture=str(fixture))],
    )
    assert config.protected_mocks is not None and config.protected_mocks[0].tool == "uip"


def test_protected_mocks_fail_closed_under_docker(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "uip.json")
    with pytest.raises(ValidationError, match="UID/GID isolation"):
        SandboxConfig(
            driver="docker",
            protected_mocks=[ProtectedMockConfig(tool="uip", fixture=str(fixture))],
        )


def test_protected_mock_names_are_unique_and_do_not_collide_with_recorders(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "uip.json")
    spec = {"tool": "uip", "fixture": str(fixture)}
    with pytest.raises(ValidationError, match="must be unique"):
        SandboxConfig(driver="tempdir", protected_mocks=[spec, spec])  # type: ignore[list-item]
    with pytest.raises(ValidationError, match="cannot both provide"):
        SandboxConfig(
            driver="tempdir",
            protected_mocks=[spec],  # type: ignore[list-item]
            record_cli=[{"tool": "uip"}],  # type: ignore[list-item]
        )


def test_protected_mock_tool_name_is_validated() -> None:
    with pytest.raises(ValidationError, match="bare executable name"):
        ProtectedMockConfig(tool="dir/uip", fixture="f.json")
    with pytest.raises(ValidationError, match="platform executable suffix"):
        ProtectedMockConfig(tool="uip.exe", fixture="f.json")


def test_passthrough_prefixes_are_validated() -> None:
    with pytest.raises(ValidationError, match="must be unique"):
        ProtectedMockConfig(
            tool="uip",
            fixture="fixture.json",
            passthrough_argv_prefixes=[["docsai", "ask"], ["docsai", "ask"]],
        )
    with pytest.raises(ValidationError, match="1 to 8"):
        ProtectedMockConfig(tool="uip", fixture="fixture.json", passthrough_argv_prefixes=[[]])


# --------------------------------------------------------------------------- #
# Endpoint parsing
# --------------------------------------------------------------------------- #


def test_parse_endpoint_round_trips() -> None:
    assert parse_endpoint("unix:/run/x/mock.sock") == ("unix", "/run/x/mock.sock")
    # Windows socket paths carry a drive colon; the unix payload is verbatim.
    assert parse_endpoint("unix:C:\\scratch\\mock.sock") == ("unix", "C:\\scratch\\mock.sock")
    assert parse_endpoint("tcp:127.0.0.1:5001") == ("tcp", ("127.0.0.1", 5001))
    for bad in ["", "unix:", "tcp:127.0.0.1", "tcp:127.0.0.1:notaport", "tcp:127.0.0.1:0", "http://x"]:
        with pytest.raises(ValueError, match="endpoint"):
            parse_endpoint(bad)


# --------------------------------------------------------------------------- #
# Dispatch: exact / normalized / subset matching
# --------------------------------------------------------------------------- #


def test_fixture_service_matches_exact_argv_and_enforces_budget(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "uip.json")
    config = _write_config(tmp_path / "config.json", fixture, max_requests=2)
    fake_server = _fake_server(load_config(config))

    expected = ProtectedMockServer.dispatch(fake_server, "uip", ["rpa", "get-errors", "--output", "json"])
    assert expected.exit_code == 0
    assert expected.stdout == '{"errors":[]}\n'

    # There is no generic file-read endpoint: an arbitrary path-bearing argv is
    # merely an unmatched CLI command and receives the fixture's fixed default.
    unmatched = ProtectedMockServer.dispatch(fake_server, "uip", ["read", "/etc/passwd"])
    assert unmatched.exit_code == 2
    assert unmatched.stderr == "not configured\n"

    exhausted = ProtectedMockServer.dispatch(fake_server, "uip", ["rpa", "get-errors", "--output", "json"])
    assert exhausted.exit_code == 75
    assert "budget exhausted" in exhausted.stderr


def test_unknown_tool_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "uip.json")
    fake_server = _fake_server(load_config(_write_config(tmp_path / "config.json", fixture)))
    response = ProtectedMockServer.dispatch(fake_server, "other", ["x"])
    assert response.exit_code == 127


def test_fixture_rejects_duplicate_argv(tmp_path: Path) -> None:
    fixture = tmp_path / "bad.json"
    response = {"argv": ["same"], "exit_code": 0}
    fixture.write_text(json.dumps({"version": 1, "responses": [response, response]}), encoding="utf-8")
    config = _write_config(tmp_path / "config.json", fixture, max_requests=1)

    with pytest.raises(ValueError, match="duplicate argv"):
        load_config(config)


def test_normalized_fixture_matching_remains_finite(tmp_path: Path) -> None:
    fixture = tmp_path / "normalized.json"
    fixture.write_text(
        json.dumps(
            {
                "version": 1,
                "responses": [
                    {
                        "argv": ["rpa", "get-errors", "--job-id", "42"],
                        "match_mode": "normalized",
                        "exit_code": 0,
                        "stdout": "configured\n",
                    }
                ],
                "default": {"exit_code": 2, "stderr": "not configured\n"},
            }
        ),
        encoding="utf-8",
    )
    fake_server = _fake_server(load_config(_write_config(tmp_path / "config.json", fixture, max_requests=2)))

    matched = ProtectedMockServer.dispatch(
        fake_server,
        "uip",
        ["--job-id=42", "get-errors", "rpa", "--output", "json"],
    )
    assert matched.stdout == "configured\n"

    extra_argument = ProtectedMockServer.dispatch(
        fake_server,
        "uip",
        ["rpa", "get-errors", "--job-id", "42", "--include-secrets"],
    )
    assert extra_argument.exit_code == 2


def _subset_fixture(path: Path, responses: list[dict]) -> Path:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "responses": responses,
                "default": {"exit_code": 2, "stderr": "not configured\n"},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_subset_matching_is_order_independent_and_tolerates_extra_tokens(tmp_path: Path) -> None:
    fixture = _subset_fixture(
        tmp_path / "subset.json",
        [{"argv": ["rpa", "get-errors"], "match_mode": "subset", "exit_code": 0, "stdout": "subset\n"}],
    )
    fake_server = _fake_server(load_config(_write_config(tmp_path / "config.json", fixture)))

    # Extra tokens, reordering, and --flag=value form are all tolerated.
    matched = ProtectedMockServer.dispatch(fake_server, "uip", ["get-errors", "--job-id=42", "rpa"])
    assert matched.stdout == "subset\n"

    # A rule token missing from the invocation is not a match.
    missing = ProtectedMockServer.dispatch(fake_server, "uip", ["rpa", "list-jobs"])
    assert missing.exit_code == 2


def test_subset_rules_scan_in_fixture_order_first_match_wins(tmp_path: Path) -> None:
    fixture = _subset_fixture(
        tmp_path / "subset.json",
        [
            {"argv": ["rpa"], "match_mode": "subset", "exit_code": 0, "stdout": "broad\n"},
            {"argv": ["rpa", "get-errors"], "match_mode": "subset", "exit_code": 0, "stdout": "narrow\n"},
        ],
    )
    fake_server = _fake_server(load_config(_write_config(tmp_path / "config.json", fixture)))

    # Both rules match; the earlier (broader) one wins because order decides.
    response = ProtectedMockServer.dispatch(fake_server, "uip", ["rpa", "get-errors"])
    assert response.stdout == "broad\n"


def test_exact_and_normalized_take_precedence_over_subset(tmp_path: Path) -> None:
    fixture = _subset_fixture(
        tmp_path / "subset.json",
        [
            {"argv": ["rpa", "get-errors"], "match_mode": "subset", "exit_code": 0, "stdout": "subset\n"},
            {"argv": ["rpa", "get-errors"], "exit_code": 0, "stdout": "exact\n"},
            {
                "argv": ["rpa", "list-jobs", "--job-id", "42"],
                "match_mode": "normalized",
                "exit_code": 0,
                "stdout": "normalized\n",
            },
            {"argv": ["rpa", "list-jobs"], "match_mode": "subset", "exit_code": 0, "stdout": "subset-jobs\n"},
        ],
    )
    fake_server = _fake_server(load_config(_write_config(tmp_path / "config.json", fixture)))

    assert ProtectedMockServer.dispatch(fake_server, "uip", ["rpa", "get-errors"]).stdout == "exact\n"
    assert (
        ProtectedMockServer.dispatch(fake_server, "uip", ["--job-id=42", "list-jobs", "rpa"]).stdout == "normalized\n"
    )
    # No exact/normalized hit -> the subset rule catches the variant.
    assert ProtectedMockServer.dispatch(fake_server, "uip", ["rpa", "list-jobs", "--all"]).stdout == "subset-jobs\n"


def test_subset_duplicates_are_allowed_and_empty_argv_is_rejected(tmp_path: Path) -> None:
    duplicate = {"argv": ["rpa"], "match_mode": "subset", "exit_code": 0, "stdout": "first\n"}
    fixture = _subset_fixture(tmp_path / "dup.json", [duplicate, {**duplicate, "stdout": "second\n"}])
    fake_server = _fake_server(load_config(_write_config(tmp_path / "config.json", fixture)))
    assert ProtectedMockServer.dispatch(fake_server, "uip", ["rpa"]).stdout == "first\n"

    empty = _subset_fixture(tmp_path / "empty.json", [{"argv": [], "match_mode": "subset", "exit_code": 0}])
    with pytest.raises(ValueError, match="non-empty for subset"):
        load_config(_write_config(tmp_path / "config2.json", empty))

    # Noise-flag-only argv normalizes to an empty token set: also rejected.
    noise = _subset_fixture(
        tmp_path / "noise.json", [{"argv": ["--output", "json"], "match_mode": "subset", "exit_code": 0}]
    )
    with pytest.raises(ValueError, match="non-empty for subset"):
        load_config(_write_config(tmp_path / "config3.json", noise))


def test_passthrough_is_prefix_limited_and_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture(tmp_path / "uip.json")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "tools": [
                    {
                        "tool": "uip",
                        "fixture": str(fixture),
                        "max_requests": 3,
                        "passthrough_argv_prefixes": [["docsai", "ask"]],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("coder_eval.protected_mock.server.shutil.which", lambda _tool: "/usr/local/bin/uip")
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, "answer\n", ""))
    monkeypatch.setattr("coder_eval.protected_mock.server.subprocess.run", run)
    fake_server = _fake_server(load_config(config))
    fake_server._passthrough.side_effect = lambda state, argv: ProtectedMockServer._passthrough(
        fake_server, state, argv
    )

    argv = ["docsai", "ask", "what failed?"]
    first = ProtectedMockServer.dispatch(fake_server, "uip", argv)
    second = ProtectedMockServer.dispatch(fake_server, "uip", argv)
    blocked = ProtectedMockServer.dispatch(fake_server, "uip", ["auth", "token"])

    assert first.stdout == second.stdout == "answer\n"
    assert blocked.exit_code == 2
    run.assert_called_once()
    assert run.call_args.args[0] == ["/usr/local/bin/uip", *argv]
    assert run.call_args.kwargs["stdin"] is subprocess.DEVNULL


# --------------------------------------------------------------------------- #
# Transport: in-process server + client over TCP loopback and AF_UNIX
# --------------------------------------------------------------------------- #


class _LiveServer:
    """In-process threaded server around ``create_server`` for transport tests."""

    def __init__(self, tmp_path: Path, transport: str, token: str | None = "test-token") -> None:
        fixture = _fixture(tmp_path / "uip.json")
        tools = load_config(_write_config(tmp_path / "config.json", fixture))
        self.token = token
        self.server, self.endpoint = create_server(tools, token, tmp_path, transport)
        self._thread = threading.Thread(
            target=self.server.serve_forever,  # pyright: ignore[reportAttributeAccessIssue]
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()  # pyright: ignore[reportAttributeAccessIssue]
        self.server.server_close()  # pyright: ignore[reportAttributeAccessIssue]
        self._thread.join(timeout=3)


def _set_client_env(monkeypatch: pytest.MonkeyPatch, endpoint: str, token: str, call_log: Path) -> None:
    monkeypatch.setenv(ENDPOINT_ENV, endpoint)
    monkeypatch.setenv(TOKEN_ENV, token)
    monkeypatch.setenv(CALL_LOG_ENV, str(call_log))


def test_tcp_loopback_end_to_end_with_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    live = _LiveServer(tmp_path, transport="tcp")
    try:
        assert live.endpoint.startswith("tcp:127.0.0.1:")
        call_log = tmp_path / "calls.jsonl"
        _set_client_env(monkeypatch, live.endpoint, "test-token", call_log)

        exit_code = client.invoke("uip", ["rpa", "get-errors", "--output", "json"])
        assert exit_code == 0
        assert capsys.readouterr().out == '{"errors":[]}\n'

        record = json.loads(call_log.read_text(encoding="utf-8").splitlines()[0])
        assert record["tool"] == "uip"
        assert record["argv"] == ["rpa", "get-errors", "--output", "json"]
        assert record["exit"] == 0
    finally:
        live.close()


def test_tcp_token_mismatch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    live = _LiveServer(tmp_path, transport="tcp")
    try:
        _set_client_env(monkeypatch, live.endpoint, "wrong-token", tmp_path / "calls.jsonl")
        exit_code = client.invoke("uip", ["rpa", "get-errors", "--output", "json"])
        assert exit_code == 77
        assert "token rejected" in capsys.readouterr().err
    finally:
        live.close()


def test_unix_socket_end_to_end_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if not _unix_transport_usable(tmp_path):
        pytest.skip("AF_UNIX stream sockets not usable on this platform")
    live = _LiveServer(tmp_path, transport="unix")
    try:
        assert live.endpoint.startswith("unix:")
        _set_client_env(monkeypatch, live.endpoint, "test-token", tmp_path / "calls.jsonl")
        exit_code = client.invoke("uip", ["rpa", "get-errors", "--output", "json"])
        assert exit_code == 0
        assert capsys.readouterr().out == '{"errors":[]}\n'
    finally:
        live.close()


def test_client_reports_unreachable_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    call_log = tmp_path / "calls.jsonl"
    _set_client_env(monkeypatch, "tcp:127.0.0.1:1", "t", call_log)
    assert client.invoke("uip", ["anything"]) == 125
    record = json.loads(call_log.read_text(encoding="utf-8").splitlines()[0])
    assert record["exit"] == 125


def test_client_main_and_size_guard(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv(CALL_LOG_ENV, raising=False)
    monkeypatch.setenv(ENDPOINT_ENV, "tcp:127.0.0.1:1")
    monkeypatch.setenv(TOKEN_ENV, "t")

    monkeypatch.setattr(sys, "argv", ["client"])
    assert client.main() == 64
    assert "missing tool name" in capsys.readouterr().err

    monkeypatch.setattr(sys, "argv", ["client", "uip", "x"])
    assert client.main() == 125  # unreachable endpoint

    # An oversized request is refused before any connection attempt.
    assert client.invoke("uip", ["x" * (70 * 1024)]) == 125
    assert "request exceeds size limit" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Runtime: subprocess lifecycle, per-run isolation, fixture resolution
# --------------------------------------------------------------------------- #


def test_runtime_end_to_end_and_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = _fixture(tmp_path / "uip.json")
    mocks = [ProtectedMockConfig(tool="uip", fixture=str(fixture))]
    runtime = ProtectedMockRuntime(mocks, task_dir=None)
    with runtime:
        assert runtime.endpoint_kind in {"unix", "tcp"}
        assert runtime.fixture_digest == fixture_digest([fixture])
        _set_client_env(monkeypatch, runtime.endpoint, runtime.token, tmp_path / "calls.jsonl")
        assert client.invoke("uip", ["rpa", "get-errors", "--output", "json"]) == 0
        assert capsys.readouterr().out == '{"errors":[]}\n'
        process = runtime._process
        runtime_dir = runtime._runtime_dir
        assert process is not None and process.poll() is None
        assert runtime_dir is not None and runtime_dir.is_dir()
    # Teardown: the server process is gone and the scratch dir is removed.
    assert process.poll() is not None
    assert not runtime_dir.exists()


def test_two_runtimes_get_isolated_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture_a = _fixture(tmp_path / "a.json")
    fixture_b = tmp_path / "b.json"
    fixture_b.write_text(
        json.dumps(
            {
                "version": 1,
                "responses": [{"argv": ["ping"], "exit_code": 0, "stdout": "pong-b\n"}],
                "default": {"exit_code": 2, "stderr": "not configured\n"},
            }
        ),
        encoding="utf-8",
    )
    mocks_a = [ProtectedMockConfig(tool="uip", fixture=str(fixture_a))]
    mocks_b = [ProtectedMockConfig(tool="uip", fixture=str(fixture_b))]
    with ProtectedMockRuntime(mocks_a, task_dir=None) as run_a, ProtectedMockRuntime(mocks_b, task_dir=None) as run_b:
        assert run_a.endpoint != run_b.endpoint
        assert run_a.token != run_b.token
        _set_client_env(monkeypatch, run_b.endpoint, run_b.token, tmp_path / "calls.jsonl")
        assert client.invoke("uip", ["ping"]) == 0


def test_runtime_start_fails_loudly_on_missing_fixture(tmp_path: Path) -> None:
    mocks = [ProtectedMockConfig(tool="uip", fixture=str(tmp_path / "missing.json"))]
    runtime = ProtectedMockRuntime(mocks, task_dir=None)
    with pytest.raises(RuntimeError, match="fixture not found"):
        runtime.start()
    assert runtime._runtime_dir is None  # scratch dir cleaned up on failure


def test_runtime_start_fails_loudly_on_bad_fixture(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": 999}), encoding="utf-8")
    runtime = ProtectedMockRuntime([ProtectedMockConfig(tool="uip", fixture=str(bad))], task_dir=None)
    with pytest.raises(RuntimeError, match="exited during startup"):
        runtime.start()


def test_fixture_paths_resolve_against_task_dir(tmp_path: Path) -> None:
    task_dir = tmp_path / "task"
    (task_dir / "fixtures").mkdir(parents=True)
    fixture = _fixture(task_dir / "fixtures" / "uip.json")

    resolved = resolve_fixture_path("./fixtures/uip.json", task_dir)
    assert resolved == fixture.resolve()

    absolute = resolve_fixture_path(str(fixture), None)
    assert absolute == fixture.resolve()

    with pytest.raises(RuntimeError, match="fixture not found"):
        resolve_fixture_path("./fixtures/other.json", task_dir)


# --------------------------------------------------------------------------- #
# Sandbox shims
# --------------------------------------------------------------------------- #


def test_sandbox_generates_data_free_client_shims(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "uip.json")
    config = SandboxConfig(
        driver="tempdir",
        python=None,
        protected_mocks=[ProtectedMockConfig(tool="uip", fixture=str(fixture))],
    )
    sandbox = Sandbox(config, task_id="protected-client")
    workspace = tmp_path / "workspace"
    try:
        sandbox.setup(workspace)
        # The shim dir does not exist until the orchestrator provides the
        # endpoint, so PATH assembly at setup time skips it.
        assert sandbox.resolved_mock_path_dirs == []

        sandbox.generate_protected_mock_shims(
            endpoint="tcp:127.0.0.1:5001",
            token="run-token",
            call_log=tmp_path / "run" / "protected_mock_calls.jsonl",
        )
        shim = workspace / "protected_mocks" / "uip"
        text = shim.read_text(encoding="utf-8")
        assert "tcp:127.0.0.1:5001" in text
        assert "run-token" in text
        assert "coder_eval.protected_mock.client" in text
        assert (workspace / "protected_mocks" / "uip.cmd").is_file()
        assert sandbox.resolved_mock_path_dirs == [(workspace / "protected_mocks").resolve()]

        # No fixture bytes, and no fixture path, anywhere in the sandbox tree.
        for path in workspace.rglob("*"):
            if path.is_file():
                content = path.read_text(encoding="utf-8", errors="replace")
                assert FIXTURE_MARKER not in content
                assert str(fixture) not in content
    finally:
        sandbox.cleanup()


def test_shim_bakes_harness_interpreter_verbatim(tmp_path: Path) -> None:
    """The baked interpreter is ``sys.executable`` exactly, venv included.

    Regression: resolving it with realpath followed the POSIX venv symlink to
    the base interpreter, where ``coder_eval`` is not installed, so the shim
    died with ModuleNotFoundError on Linux.
    """
    fixture = _fixture(tmp_path / "uip.json")
    config = SandboxConfig(
        driver="tempdir",
        python=None,
        protected_mocks=[ProtectedMockConfig(tool="uip", fixture=str(fixture))],
    )
    sandbox = Sandbox(config, task_id="protected-interpreter")
    workspace = tmp_path / "workspace"
    try:
        sandbox.setup(workspace)
        sandbox.generate_protected_mock_shims(
            endpoint="tcp:127.0.0.1:5001", token="t", call_log=tmp_path / "calls.jsonl"
        )
        text = (workspace / "protected_mocks" / "uip").read_text(encoding="utf-8")
        assert f"INTERPRETER = {sys.executable!r}" in text
    finally:
        sandbox.cleanup()


def test_shim_collision_with_mock_path_dirs_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "uip.json")
    config = SandboxConfig(
        driver="tempdir",
        python=None,
        mock_path_dirs=["mocks"],
        protected_mocks=[ProtectedMockConfig(tool="uip", fixture=str(fixture))],
    )
    sandbox = Sandbox(config, task_id="protected-clash")
    workspace = tmp_path / "workspace"
    try:
        sandbox.setup(workspace)
        mocks_dir = workspace / "mocks"
        mocks_dir.mkdir()
        (mocks_dir / "uip").write_text("#!/bin/sh\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="already provides one"):
            sandbox.generate_protected_mock_shims(
                endpoint="tcp:127.0.0.1:5001", token="t", call_log=tmp_path / "calls.jsonl"
            )
    finally:
        sandbox.cleanup()


def test_shim_executes_against_live_service(tmp_path: Path) -> None:
    """The generated shim is self-sufficient: no service env vars in the caller."""
    fixture = _fixture(tmp_path / "uip.json")
    mocks = [ProtectedMockConfig(tool="uip", fixture=str(fixture))]
    config = SandboxConfig(driver="tempdir", python=None, protected_mocks=mocks)
    sandbox = Sandbox(config, task_id="protected-exec")
    workspace = tmp_path / "workspace"
    call_log = tmp_path / "run" / "protected_mock_calls.jsonl"
    call_log.parent.mkdir(parents=True)
    try:
        sandbox.setup(workspace)
        with ProtectedMockRuntime(mocks, task_dir=None) as runtime:
            sandbox.generate_protected_mock_shims(endpoint=runtime.endpoint, token=runtime.token, call_log=call_log)
            result = subprocess.run(
                [sys.executable, str(workspace / "protected_mocks" / "uip"), "rpa", "get-errors", "--output", "json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
        assert result.returncode == 0
        assert result.stdout == '{"errors":[]}\n'
        record = json.loads(call_log.read_text(encoding="utf-8").splitlines()[0])
        assert record["argv"] == ["rpa", "get-errors", "--output", "json"]
    finally:
        sandbox.cleanup()


# --------------------------------------------------------------------------- #
# Orchestrator wiring (NoOpAgent, no network)
# --------------------------------------------------------------------------- #


async def test_orchestrator_starts_service_generates_shims_and_tears_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full lifecycle through Orchestrator.run(): start, audit record, teardown."""
    from coder_eval.config import settings
    from coder_eval.models import AgentKind, ApiBackend, FileExistsCriterion, TaskDefinition, parse_agent_config
    from coder_eval.orchestrator import Orchestrator

    fixture = _fixture(tmp_path / "uip.json")
    task = TaskDefinition(
        task_id="protected-orch",
        description="d",
        agent=parse_agent_config(type=AgentKind.NONE),
        sandbox=SandboxConfig(
            driver="tempdir",
            python=None,
            protected_mocks=[ProtectedMockConfig(tool="uip", fixture=str(fixture))],
        ),
        # The shim must be on disk while criteria run: proves the service was
        # wired before the agent phase and the sandbox carries only the shim.
        success_criteria=[FileExistsCriterion(description="shim exists", path="protected_mocks/uip")],
    )

    created: list[ProtectedMockRuntime] = []
    real_start = ProtectedMockRuntime.start

    def _recording_start(self: ProtectedMockRuntime) -> None:
        created.append(self)
        real_start(self)

    monkeypatch.setattr(ProtectedMockRuntime, "start", _recording_start)
    monkeypatch.setattr(settings, "api_backend", ApiBackend.DIRECT)

    run_dir = tmp_path / "run" / task.task_id
    run_dir.mkdir(parents=True)
    orch = Orchestrator(task=task, run_dir=run_dir, variant_id="v")
    result = await orch.run()

    from coder_eval.models import FinalStatus

    assert result.final_status == FinalStatus.SUCCESS
    assert result.environment_info["protected_mock_endpoint_kind"] in {"unix", "tcp"}
    assert result.environment_info["protected_mock_fixture_digest"] == fixture_digest([fixture])
    # Call log seeded next to task.json, outside the sandbox.
    assert (run_dir / "protected_mock_calls.jsonl").is_file()
    # The server never outlives the task: stopped and dereferenced in _cleanup.
    assert orch._protected_mock_runtime is None
    assert len(created) == 1
    assert created[0]._process is None
    # The preserved sandbox carries the shim but no fixture bytes.
    assert result.sandbox_path is not None
    preserved = Path(result.sandbox_path)
    assert (preserved / "protected_mocks" / "uip").is_file()
    for path in preserved.rglob("*"):
        if path.is_file():
            assert FIXTURE_MARKER not in path.read_text(encoding="utf-8", errors="replace")
