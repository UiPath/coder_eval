"""Drift guards for the Linux UID/GID agent boundary."""

from __future__ import annotations

import signal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from coder_eval.isolation.agent_worker import build_agent_worker_environment
from coder_eval.isolation.docker_runner import DockerRunError, DockerRunner, _preflight_agent_isolation_image
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
)


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


def test_agent_worker_environment_scrubs_harness_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKILLS_REPO_PATH", "/private/skills")
    monkeypatch.setenv("TASK_DIR", "/private/task")
    monkeypatch.setenv("CODER_EVAL_AGENT_ISOLATION", "1")
    monkeypatch.setenv("CODER_EVAL_IN_CONTAINER", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "needed-by-agent")

    worker_env = build_agent_worker_environment()

    assert "SKILLS_REPO_PATH" not in worker_env
    assert "TASK_DIR" not in worker_env
    assert "CODER_EVAL_AGENT_ISOLATION" not in worker_env
    assert worker_env["CODER_EVAL_IN_CONTAINER"] == "1"
    assert worker_env["ANTHROPIC_API_KEY"] == "needed-by-agent"
    assert worker_env["HOME"] == AGENT_HOME
    assert worker_env["PYTHONNOUSERSITE"] == "1"
    assert worker_env["PYTHONSAFEPATH"] == "1"


def test_agent_worker_environment_scrubs_inherited_bedrock_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The evaluated agent must not inherit the evaluator's Bedrock token.

    The UID barrier blocks filesystem access to grading material but cannot hide a
    process's own environment, so a credential left there is readable by the agent
    itself. Claude re-sets this explicitly from a resolved BedrockRoute, so masking
    the inherited value costs the Bedrock path nothing.
    """

    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "evaluator-only-secret")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    worker_env = build_agent_worker_environment()

    assert "AWS_BEARER_TOKEN_BEDROCK" not in worker_env
    # The region is not a credential and stays inherited.
    assert worker_env["AWS_REGION"] == "us-east-1"
    assert worker_env["USER"] == "agent"
    assert worker_env["LOGNAME"] == "agent"
    assert worker_env["ZDOTDIR"] == AGENT_HOME


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


def test_isolation_rejects_dynamic_privileged_criterion(tmp_path: Path) -> None:
    task = TaskDefinition(
        task_id="unsafe-grader",
        description="test",
        initial_prompt="work",
        agent=ClaudeCodeAgentConfig(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig(agent_isolation=True)),
        success_criteria=[RunCommandCriterion(description="unsafe", command="python check.py")],
    )
    rt = MagicMock(task=task, task_file=tmp_path / "task.yaml", run_dir=tmp_path / "run")
    runner = DockerRunner(rt)

    with pytest.raises(RuntimeError, match="dynamic criteria"):
        runner._validate_agent_isolation_compatibility()


def test_isolation_does_not_hardcode_agent_kinds(tmp_path: Path) -> None:
    task = MagicMock()
    task.agent.type = "third-party-agent"
    task.success_criteria = []
    task.sandbox.docker = DockerDriverConfig(agent_isolation=True)
    rt = MagicMock(task=task, task_file=tmp_path / "task.yaml", run_dir=tmp_path / "run")

    DockerRunner(rt)._validate_agent_isolation_compatibility()
