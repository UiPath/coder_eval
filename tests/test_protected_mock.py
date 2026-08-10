"""Tests for the protected exact-command mock protocol and thin wrappers."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from coder_eval.isolation.docker_runner import DockerRunner
from coder_eval.models import (
    AgentKind,
    ClaudeCodeAgentConfig,
    DockerDriverConfig,
    FileExistsCriterion,
    ProtectedMockConfig,
    SandboxConfig,
    TaskDefinition,
)
from coder_eval.protected_mock import client
from coder_eval.protected_mock.protocol import CLIENT_EXECUTABLE, MAX_REQUEST_BYTES
from coder_eval.protected_mock.runtime import running_mock_server
from coder_eval.protected_mock.server import ProtectedMockServer, ToolState, _normalized_argv, load_config
from coder_eval.sandbox import Sandbox


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


def _fake_server(tools: dict[str, ToolState]) -> MagicMock:
    fake = MagicMock()
    fake.tools = tools
    fake.budget_lock = threading.Lock()
    fake.passthrough_lock = threading.Lock()
    return fake


def test_protected_mocks_require_docker_driver(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "uip.json")
    with pytest.raises(ValidationError, match="requires driver: docker"):
        SandboxConfig(
            driver="tempdir",
            protected_mocks=[ProtectedMockConfig(tool="uip", fixture=str(fixture))],
        )


def test_protected_mock_names_are_unique_and_do_not_collide_with_recorders(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "uip.json")
    spec = {"tool": "uip", "fixture": str(fixture)}
    with pytest.raises(ValidationError, match="must be unique"):
        SandboxConfig(driver="docker", protected_mocks=[spec, spec])  # type: ignore[list-item]
    with pytest.raises(ValidationError, match="cannot both provide"):
        SandboxConfig(
            driver="docker",
            protected_mocks=[spec],  # type: ignore[list-item]
            record_cli=[{"tool": "uip"}],  # type: ignore[list-item]
        )


def test_fixture_service_matches_exact_argv_and_enforces_budget(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "uip.json")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "tools": [{"tool": "uip", "fixture": str(fixture), "max_requests": 2}],
            }
        ),
        encoding="utf-8",
    )
    tools = load_config(config)
    fake_server = MagicMock()
    fake_server.tools = tools
    fake_server.budget_lock = threading.Lock()

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
    fake_server = _fake_server(load_config(_write_config(tmp_path / "config.json", fixture, max_requests=1)))

    unknown = ProtectedMockServer.dispatch(fake_server, "other", ["rpa", "get-errors"])
    assert unknown.exit_code == 127
    assert "unknown tool" in unknown.stderr

    # The rejected call charges nobody: the one configured request is still
    # available, and only the request AFTER it trips the budget.
    served = ProtectedMockServer.dispatch(fake_server, "uip", ["rpa", "get-errors", "--output", "json"])
    assert served.exit_code == 0
    exhausted = ProtectedMockServer.dispatch(fake_server, "uip", ["rpa", "get-errors", "--output", "json"])
    assert exhausted.exit_code == 75


def test_fixture_rejects_duplicate_argv(tmp_path: Path) -> None:
    fixture = tmp_path / "bad.json"
    response = {"argv": ["same"], "exit_code": 0}
    fixture.write_text(json.dumps({"version": 1, "responses": [response, response]}), encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"version": 1, "tools": [{"tool": "uip", "fixture": str(fixture), "max_requests": 1}]}),
        encoding="utf-8",
    )

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
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"version": 1, "tools": [{"tool": "uip", "fixture": str(fixture), "max_requests": 2}]}),
        encoding="utf-8",
    )
    fake_server = MagicMock()
    fake_server.tools = load_config(config)
    fake_server.budget_lock = threading.Lock()

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


def test_noise_flag_tokenizer_only_swallows_real_values() -> None:
    # An --output <format> pair is still stripped, in either flag form.
    assert _normalized_argv(["rpa", "get-errors", "--output", "json"]) == ("get-errors", "rpa")
    assert _normalized_argv(["rpa", "get-errors", "--output=json"]) == ("get-errors", "rpa")

    # A valueless --output must not swallow the flag that follows it.
    assert _normalized_argv(["deploy", "--output", "--delete-all"]) == ("--delete-all", "deploy")
    assert _normalized_argv(["deploy", "--output=", "--delete-all"]) == ("--delete-all", "deploy")

    # A trailing --output is simply dropped.
    assert _normalized_argv(["deploy", "--output"]) == ("deploy",)


def _subset_fixture(path: Path, responses: list[dict[str, Any]]) -> Path:
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


def _stub_mockd_child(monkeypatch: pytest.MonkeyPatch, script: str, tmp_path: Path) -> list[Path]:
    """Run ``script`` in place of mockd; returns the stderr temp files it created.

    ``script`` is formatted with a ``{ready}`` marker path it must create once its
    stderr is flushed. The fake ``Popen`` blocks on that marker (or on the child
    exiting), so the readiness deadline only starts ticking after the child has
    actually said something -- interpreter startup cost stays out of the clock.
    """
    monkeypatch.setattr("coder_eval.protected_mock.runtime.SOCKET_PATH", str(tmp_path / "uip.sock"))
    ready = tmp_path / "child-ready"
    source = script.format(ready=str(ready))
    real_popen = subprocess.Popen
    real_named_temp = tempfile.NamedTemporaryFile
    created: list[Path] = []

    def fake_popen(_argv: list[str], **kwargs: Any) -> Any:
        child = real_popen([sys.executable, "-c", source], **kwargs)
        while not ready.exists() and child.poll() is None:
            time.sleep(0.01)
        return child

    def recording_named_temp(**kwargs: Any) -> Any:
        handle = real_named_temp(**kwargs)
        created.append(Path(handle.name))
        return handle

    monkeypatch.setattr("coder_eval.protected_mock.runtime.subprocess.Popen", fake_popen)
    monkeypatch.setattr("coder_eval.protected_mock.runtime.tempfile.NamedTemporaryFile", recording_named_temp)
    return created


def test_mockd_startup_exit_reports_child_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created = _stub_mockd_child(
        monkeypatch,
        "import sys; sys.stderr.write('mockd fixture load failed\\n'); raise SystemExit(3)",
        tmp_path,
    )
    monkeypatch.setattr("coder_eval.protected_mock.runtime.STARTUP_TIMEOUT_SECONDS", 10.0)

    with (
        pytest.raises(RuntimeError, match="exited during startup") as excinfo,
        running_mock_server(tmp_path / "config.json"),
    ):
        pass

    assert "mockd fixture load failed" in str(excinfo.value)
    assert created and not created[0].exists()


def test_mockd_startup_timeout_reports_wait_time_and_child_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _stub_mockd_child(
        monkeypatch,
        "import sys, time, pathlib; "
        "sys.stderr.write('mockd still binding\\n'); sys.stderr.flush(); "
        "pathlib.Path(r'{ready}').write_text('1'); time.sleep(30)",
        tmp_path,
    )
    monkeypatch.setattr("coder_eval.protected_mock.runtime.STARTUP_TIMEOUT_SECONDS", 0.3)

    with (
        pytest.raises(RuntimeError, match="did not create its socket within") as excinfo,
        running_mock_server(tmp_path / "config.json"),
    ):
        pass

    assert "mockd still binding" in str(excinfo.value)
    if sys.platform != "win32":
        # Windows releases a just-terminated child's inherited handle
        # asynchronously, so the best-effort unlink only lands reliably on the
        # platform mockd actually runs on.
        assert created and not created[0].exists()


def _real_loader_script(config: Path) -> str:
    """Child source that runs the real fixture loader, so a bad config kills it.

    Embeds the config path literally (no ``{}`` placeholders) so it survives the
    ``{ready}`` formatting in :func:`_stub_mockd_child` untouched.
    """
    return (
        "import pathlib; "
        "from coder_eval.protected_mock.server import load_config; "
        f"load_config(pathlib.Path({str(config)!r}))"
    )


def test_runtime_start_fails_loudly_on_missing_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _write_config(tmp_path / "config.json", tmp_path / "missing.json")

    # Loader level: a fixture that is not on disk is an error, never an empty
    # response table that would silently serve the default to every command.
    with pytest.raises(FileNotFoundError, match=r"missing\.json"):
        load_config(config)

    # Runtime level: the same failure inside mockd surfaces as a startup error
    # carrying the child's exit code and a tail of its stderr.
    _stub_mockd_child(monkeypatch, _real_loader_script(config), tmp_path)
    monkeypatch.setattr("coder_eval.protected_mock.runtime.STARTUP_TIMEOUT_SECONDS", 10.0)

    with (
        pytest.raises(RuntimeError, match="exited during startup") as excinfo,
        running_mock_server(config),
    ):
        pass

    assert "FileNotFoundError" in str(excinfo.value)
    assert "missing.json" in str(excinfo.value)


def test_runtime_start_fails_loudly_on_bad_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    truncated = tmp_path / "truncated.json"
    truncated.write_text('{"version": 1, "responses": [', encoding="utf-8")
    with pytest.raises(ValueError, match="Expecting"):
        load_config(_write_config(tmp_path / "config-truncated.json", truncated))

    wrong_version = tmp_path / "wrong-version.json"
    wrong_version.write_text(json.dumps({"version": 999, "responses": []}), encoding="utf-8")
    wrong_version_config = _write_config(tmp_path / "config-version.json", wrong_version)
    with pytest.raises(ValueError, match="must declare version 1"):
        load_config(wrong_version_config)

    bad_mode = tmp_path / "bad-mode.json"
    bad_mode.write_text(
        json.dumps({"version": 1, "responses": [{"argv": ["rpa"], "match_mode": "prefix"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="match_mode must be exact, normalized, or subset"):
        load_config(_write_config(tmp_path / "config-mode.json", bad_mode))

    bad_exit = tmp_path / "bad-exit.json"
    bad_exit.write_text(
        json.dumps({"version": 1, "responses": [{"argv": ["rpa"], "exit_code": 300}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exit_code must be an integer from 0 to 255"):
        load_config(_write_config(tmp_path / "config-exit.json", bad_exit))

    # And the same rejection inside mockd aborts startup rather than binding a
    # socket that would serve a partially loaded fixture.
    _stub_mockd_child(monkeypatch, _real_loader_script(wrong_version_config), tmp_path)
    monkeypatch.setattr("coder_eval.protected_mock.runtime.STARTUP_TIMEOUT_SECONDS", 10.0)

    with (
        pytest.raises(RuntimeError, match="exited during startup") as excinfo,
        running_mock_server(wrong_version_config),
    ):
        pass

    assert "must declare version 1" in str(excinfo.value)


def test_client_reports_unreachable_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    call_log = tmp_path / "calls.jsonl"
    monkeypatch.setattr("coder_eval.protected_mock.client.SOCKET_PATH", str(tmp_path / "absent.sock"))
    monkeypatch.setenv("CODER_EVAL_MOCK_CALL_LOG", str(call_log))

    exit_code = client.invoke("uip", ["rpa", "get-errors"])

    # No fabricated success: a service the client cannot reach is a hard failure.
    assert exit_code == 125
    captured = capsys.readouterr()
    assert "service unavailable" in captured.err
    assert captured.out == ""
    record = json.loads(call_log.read_text(encoding="utf-8").splitlines()[0])
    assert record["tool"] == "uip"
    assert record["exit"] == 125


def test_client_main_and_size_guard(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("CODER_EVAL_MOCK_CALL_LOG", raising=False)

    monkeypatch.setattr(sys, "argv", ["client"])
    assert client.main() == 64
    assert "missing tool name" in capsys.readouterr().err

    # An oversized request is refused before any connection attempt, so the
    # socket constructor must never run.
    guard = MagicMock(side_effect=AssertionError("size guard must reject before opening a socket"))
    monkeypatch.setattr("coder_eval.protected_mock.client.socket.socket", guard)
    assert client.invoke("uip", ["x" * (MAX_REQUEST_BYTES + 1024)]) == 125
    captured = capsys.readouterr()
    assert "request exceeds size limit" in captured.err
    assert captured.out == ""
    guard.assert_not_called()


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
    fake_server = MagicMock()
    fake_server.tools = load_config(config)
    fake_server.budget_lock = threading.Lock()
    fake_server.passthrough_lock = threading.Lock()
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


def test_passthrough_prefixes_are_validated() -> None:
    with pytest.raises(ValidationError, match="must be unique"):
        ProtectedMockConfig(
            tool="uip",
            fixture="fixture.json",
            passthrough_argv_prefixes=[["docsai", "ask"], ["docsai", "ask"]],
        )
    with pytest.raises(ValidationError, match="1 to 8"):
        ProtectedMockConfig(tool="uip", fixture="fixture.json", passthrough_argv_prefixes=[[]])


def test_sandbox_generates_data_free_client_wrapper(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "uip.json")
    config = SandboxConfig(
        driver="docker",
        python=None,
        protected_mocks=[ProtectedMockConfig(tool="uip", fixture=str(fixture))],
    )
    # Mirrors _run-task-internal: validate protected_mocks while the authored
    # driver is docker, then execute the already-containerized sandbox locally.
    config = config.model_copy(update={"driver": "tempdir"})
    sandbox = Sandbox(config, task_id="protected-client")
    workspace = tmp_path / "workspace"
    try:
        sandbox.setup(workspace)
        wrapper = workspace / "cli_mocks" / "uip"
        text = wrapper.read_text(encoding="utf-8")
        assert CLIENT_EXECUTABLE in text
        assert str(fixture) not in text
        assert '{"errors":[]}' not in text
        assert sandbox.resolved_mock_path_dirs == [(workspace / "cli_mocks").resolve()]
        assert (workspace / "cli_mocks" / "calls.jsonl").is_file()
    finally:
        sandbox.cleanup()


def test_sandbox_client_wrapper_collision_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "uip.json")
    config = SandboxConfig(
        driver="docker",
        python=None,
        protected_mocks=[ProtectedMockConfig(tool="uip", fixture=str(fixture))],
    )
    config = config.model_copy(update={"driver": "tempdir"})
    sandbox = Sandbox(config, task_id="protected-clash")
    workspace = tmp_path / "workspace"
    # A file already sitting at the wrapper path (a preserved --run-dir, a
    # template that ships its own `uip`) must not be silently replaced.
    (workspace / "cli_mocks").mkdir(parents=True)
    (workspace / "cli_mocks" / "uip").write_text("#!/bin/sh\n", encoding="utf-8")
    try:
        with pytest.raises(RuntimeError, match="would overwrite"):
            sandbox.setup(workspace)
    finally:
        sandbox.cleanup()


def test_docker_stages_fixture_copy_only_under_mockd_parent(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "uip.json")
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    task = TaskDefinition(
        task_id="protected-mock",
        description="test",
        initial_prompt="run uip",
        agent=ClaudeCodeAgentConfig(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(
            driver="docker",
            docker=DockerDriverConfig(agent_isolation=True),
            protected_mocks=[
                ProtectedMockConfig(
                    tool="uip",
                    fixture=str(fixture),
                    max_requests=3,
                    passthrough_argv_prefixes=[["docsai", "ask"]],
                )
            ],
        ),
        success_criteria=[FileExistsCriterion(description="done", path="done.txt")],
    )
    rt = MagicMock(task=task, task_file=task_dir / "task.yaml", run_dir=tmp_path / "run")
    runner = DockerRunner(rt)
    staging = tmp_path / "staging"
    staging.mkdir()

    runner._prepare_isolated_sources(staging)
    payload = runner._rewrite_task_paths(task.model_dump(mode="json"))

    assert runner._mock_fixture_mount == staging / "protected-mock-fixtures"
    assert (runner._mock_fixture_mount / "fixture-0.json").read_text(encoding="utf-8") == fixture.read_text(
        encoding="utf-8"
    )
    assert str(fixture) not in json.dumps(payload)
    protected = payload["sandbox"]["protected_mocks"]  # type: ignore[index]
    assert protected[0]["fixture"] == "/opt/coder-eval/mock/fixtures/fixture-0.json"  # type: ignore[index]
    config = json.loads((runner._mock_fixture_mount / "mock-config.json").read_text(encoding="utf-8"))
    assert config["tools"][0]["passthrough_argv_prefixes"] == [["docsai", "ask"]]
