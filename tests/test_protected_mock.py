"""Tests for the protected exact-command mock protocol and thin wrappers."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
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
from coder_eval.protected_mock.protocol import CLIENT_EXECUTABLE
from coder_eval.protected_mock.server import ProtectedMockServer, load_config
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
