"""Drift guards for the Linux UID/GID agent boundary."""

from __future__ import annotations

import logging
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from coder_eval.agents.codex_agent import CodexAgent
from coder_eval.isolation.docker_runner import DockerRunError, DockerRunner, _image_supports_agent_isolation
from coder_eval.models import (
    AGENT_GID,
    AGENT_HOME,
    AGENT_UID,
    AgentKind,
    ClaudeCodeAgentConfig,
    DockerDriverConfig,
    RunCommandCriterion,
    SandboxConfig,
    TaskDefinition,
    parse_agent_config,
)
from coder_eval.utils import scrub_agent_env_overrides


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_image_identity_literals_and_capability_label_match_models() -> None:
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert f"ARG AGENT_UID={AGENT_UID}" in dockerfile
    assert f"ARG AGENT_GID={AGENT_GID}" in dockerfile
    assert 'LABEL org.coder-eval.agent-isolation="uid-gid-v1"' in dockerfile
    assert "USER agent" not in dockerfile
    assert DockerDriverConfig().agent_isolation is True


def test_privilege_launcher_clears_capabilities_and_sets_no_new_privs() -> None:
    script = (REPO_ROOT / "docker" / "coder_eval_drop_privilege.sh").read_text(encoding="utf-8")
    assert "--inh-caps=-all" in script
    assert "--ambient-caps=-all" in script
    assert "--bounding-set=-all" in script
    assert "--no-new-privs" in script
    assert "--clear-groups" in script


def test_agent_launcher_targets_only_agent_identity() -> None:
    script = (REPO_ROOT / "docker" / "coder_eval_drop_privilege.sh").read_text(encoding="utf-8")
    assert "--reuid=agent" in script
    assert "--regid=agent" in script


def test_agent_environment_scrubs_only_present_harness_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKILLS_REPO_PATH", "/private/skills")
    monkeypatch.setenv("TASK_DIR", "/private/task")
    monkeypatch.setenv("CODER_EVAL_AGENT_ISOLATION", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "needed-by-agent")
    # Keep the exact-equality assertion below valid on a host that exports it.
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)

    overrides = scrub_agent_env_overrides()

    assert overrides == {
        "SKILLS_REPO_PATH": "",
        "TASK_DIR": "",
        "CODER_EVAL_AGENT_ISOLATION": "",
    }
    assert "ANTHROPIC_API_KEY" not in overrides


def test_agent_environment_scrubs_inherited_bedrock_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The evaluated agent must not inherit the evaluator's Bedrock token.

    The UID barrier blocks filesystem access to grading material but cannot hide a
    process's own environment, so a credential left there is readable by the agent
    itself. Claude re-sets this explicitly from a resolved BedrockRoute, so masking
    the inherited value costs the Bedrock path nothing.
    """

    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "evaluator-only-secret")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    overrides = scrub_agent_env_overrides()

    assert overrides["AWS_BEARER_TOKEN_BEDROCK"] == ""
    # The region is not a credential and stays inherited.
    assert "AWS_REGION" not in overrides


def test_isolated_codex_profiles_never_restore_root_harness_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/root")
    monkeypatch.setenv("ZDOTDIR", "/root/private-zdot")
    monkeypatch.setenv("CODER_EVAL_AGENT_ISOLATION", "1")
    monkeypatch.setattr(CodexAgent, "_login_shell_profiles_supported", staticmethod(lambda: True))
    agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
    agent._env_path_prepend = ["/work/agent/cli_mocks"]

    agent._setup_login_shell_home()
    try:
        assert agent._login_shell_home is not None
        for profile in (".bash_profile", ".profile", ".zshenv", ".zprofile", ".zshrc"):
            content = (agent._login_shell_home / profile).read_text(encoding="utf-8")
            assert f"export HOME={AGENT_HOME}" in content
            assert "/root" not in content
    finally:
        agent._cleanup_login_shell_home()


def test_agent_teardown_rescans_until_uid_has_no_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    from coder_eval.isolation import agent_identity

    scans = iter([[41, 42], [42], []])
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setenv("CODER_EVAL_AGENT_ISOLATION", "1")
    monkeypatch.setattr(agent_identity, "require_isolation_runtime", lambda: None)
    monkeypatch.setattr(agent_identity, "_agent_pids", lambda: next(scans))
    monkeypatch.setattr(
        agent_identity,
        "_signal_agent_pids",
        lambda pids, sig: signals.extend((pid, sig) for pid in pids),
    )
    monkeypatch.setattr(agent_identity.time, "sleep", lambda _seconds: None)

    agent_identity.terminate_agent_processes()

    expected_kill = getattr(signal, "SIGKILL", signal.SIGTERM)
    assert signals == [(41, signal.SIGTERM), (42, signal.SIGTERM), (42, expected_kill)]


def test_isolation_image_capability_check_reports_support_without_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "coder_eval.isolation.docker_runner.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="uid-gid-v1\n"),
    )
    assert _image_supports_agent_isolation("image:good") is True

    monkeypatch.setattr(
        "coder_eval.isolation.docker_runner.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="\n"),
    )
    assert _image_supports_agent_isolation("image:old") is False

    def _inspect_fails(*args: object, **kwargs: object) -> SimpleNamespace:
        raise subprocess.CalledProcessError(1, "docker")

    monkeypatch.setattr("coder_eval.isolation.docker_runner.subprocess.run", _inspect_fails)
    with pytest.raises(DockerRunError, match="cannot verify"):
        _image_supports_agent_isolation("image:missing")


def _make_runner(tmp_path: Path, task: object) -> DockerRunner:
    return DockerRunner(MagicMock(task=task, task_file=tmp_path / "task.yaml", run_dir=tmp_path / "run"))


def test_isolation_stays_active_with_dynamic_criteria(tmp_path: Path) -> None:
    """run_command / uipath_eval / agent_judge execute in the grader phase and never gate isolation."""

    task = TaskDefinition(
        task_id="dynamic-grader",
        description="test",
        initial_prompt="work",
        agent=ClaudeCodeAgentConfig(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig(agent_isolation=True)),
        success_criteria=[RunCommandCriterion(description="dynamic", command="python check.py")],
    )
    runner = _make_runner(tmp_path, task)

    runner._resolve_agent_isolation()

    assert runner._isolation_active is True


def test_isolation_downgrades_for_unsupported_agent_type(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    task = SimpleNamespace(
        task_id="mystery-agent",
        agent=SimpleNamespace(type="mystery"),
        sandbox=SimpleNamespace(docker=SimpleNamespace(agent_isolation=True, working_dir=None, extra_mounts=[])),
    )
    runner = _make_runner(tmp_path, task)

    with caplog.at_level(logging.WARNING):
        runner._resolve_agent_isolation()

    assert runner._isolation_active is False
    assert "WITHOUT agent isolation" in caplog.text


@pytest.mark.parametrize(
    ("docker_kwargs", "reason_fragment"),
    [
        ({"working_dir": "/app"}, "working_dir"),
        ({"extra_mounts": ["/host/data:/data:ro"]}, "extra_mounts"),
    ],
)
def test_isolation_downgrades_for_unsupported_docker_config(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    docker_kwargs: dict[str, object],
    reason_fragment: str,
) -> None:
    task = TaskDefinition(
        task_id="legacy-config",
        description="test",
        initial_prompt="work",
        agent=ClaudeCodeAgentConfig(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig(agent_isolation=True, **docker_kwargs)),
        success_criteria=[RunCommandCriterion(description="dynamic", command="python check.py")],
    )
    runner = _make_runner(tmp_path, task)

    with caplog.at_level(logging.WARNING):
        runner._resolve_agent_isolation()

    assert runner._isolation_active is False
    assert reason_fragment in caplog.text


def test_explicitly_disabled_isolation_stays_disabled(tmp_path: Path) -> None:
    task = TaskDefinition(
        task_id="opt-out",
        description="test",
        initial_prompt="work",
        agent=ClaudeCodeAgentConfig(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig(agent_isolation=False)),
        success_criteria=[RunCommandCriterion(description="dynamic", command="python check.py")],
    )
    runner = _make_runner(tmp_path, task)

    runner._resolve_agent_isolation()

    assert runner._isolation_active is False
