"""Drift guards for the Linux UID/GID agent boundary."""

from __future__ import annotations

import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from coder_eval.agents.codex_agent import CodexAgent
from coder_eval.isolation.docker_runner import DockerRunError, _preflight_agent_isolation_image
from coder_eval.models import (
    AGENT_GID,
    AGENT_HOME,
    AGENT_UID,
    MOCK_RPC_GID,
    MOCKD_GID,
    MOCKD_UID,
    AgentKind,
    DockerDriverConfig,
    parse_agent_config,
)
from coder_eval.utils import scrub_agent_env_overrides


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_image_identity_literals_and_capability_label_match_models() -> None:
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert f"ARG AGENT_UID={AGENT_UID}" in dockerfile
    assert f"ARG AGENT_GID={AGENT_GID}" in dockerfile
    assert f"ARG MOCKD_UID={MOCKD_UID}" in dockerfile
    assert f"ARG MOCKD_GID={MOCKD_GID}" in dockerfile
    assert f"ARG MOCK_RPC_GID={MOCK_RPC_GID}" in dockerfile
    assert 'LABEL org.coder-eval.agent-isolation="uid-gid-v1"' in dockerfile
    assert "USER agent" not in dockerfile
    assert DockerDriverConfig().agent_isolation is True


@pytest.mark.parametrize("script_name", ["coder_eval_drop_privilege.sh", "coder_eval_mockd.sh"])
def test_privilege_launchers_clear_capabilities_and_set_no_new_privs(script_name: str) -> None:
    script = (REPO_ROOT / "docker" / script_name).read_text(encoding="utf-8")
    assert "--inh-caps=-all" in script
    assert "--ambient-caps=-all" in script
    assert "--bounding-set=-all" in script
    assert "--no-new-privs" in script
    if script_name == "coder_eval_drop_privilege.sh":
        assert "--clear-groups" in script
        assert "CODER_EVAL_AGENT_ALLOW_RPC" in script
    else:
        assert "--groups=uip-rpc" in script


def test_agent_launcher_targets_only_agent_identity() -> None:
    script = (REPO_ROOT / "docker" / "coder_eval_drop_privilege.sh").read_text(encoding="utf-8")
    assert "--reuid=agent" in script
    assert "--regid=agent" in script
    assert "mockd" not in script


def test_agent_environment_scrubs_only_present_harness_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKILLS_REPO_PATH", "/private/skills")
    monkeypatch.setenv("TASK_DIR", "/private/task")
    monkeypatch.setenv("CODER_EVAL_AGENT_ISOLATION", "1")
    monkeypatch.setenv("CODER_EVAL_AGENT_ALLOW_RPC", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "needed-by-agent")

    overrides = scrub_agent_env_overrides()

    assert overrides == {
        "SKILLS_REPO_PATH": "",
        "TASK_DIR": "",
        "CODER_EVAL_AGENT_ISOLATION": "",
    }
    assert "ANTHROPIC_API_KEY" not in overrides
    assert "CODER_EVAL_AGENT_ALLOW_RPC" not in overrides


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


def test_isolation_image_label_preflight_accepts_only_declared_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "coder_eval.isolation.docker_runner.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="uid-gid-v1\n"),
    )
    _preflight_agent_isolation_image("image:good")

    monkeypatch.setattr(
        "coder_eval.isolation.docker_runner.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="\n"),
    )
    with pytest.raises(DockerRunError, match="does not declare"):
        _preflight_agent_isolation_image("image:old")
