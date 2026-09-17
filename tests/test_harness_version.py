"""``Agent.harness_version``: every built-in adapter names the CLI or SDK version it drives."""

from __future__ import annotations

import os
import sys
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import pytest

from coder_eval.agent import command_version
from coder_eval.agents.antigravity_agent import AntigravityAgent
from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.agents.codex_agent import CodexAgent
from coder_eval.agents.noop_agent import NoOpAgent
from coder_eval.agents.opencode_agent import OpenCodeAgent
from coder_eval.agents.pi_agent import PiAgent
from coder_eval.models import AgentKind, parse_agent_config


posix_only = pytest.mark.skipif(os.name != "posix", reason="the fake CLI is a shell script")


class TestCommandVersion:
    async def test_the_first_non_empty_stdout_line(self) -> None:
        argv = [sys.executable, "-c", "print(); print('  tool 1.2.3  '); print('extra')"]
        assert await command_version(argv) == "tool 1.2.3"

    async def test_a_non_zero_exit_is_unknown(self) -> None:
        assert await command_version([sys.executable, "-c", "print('1.0'); raise SystemExit(2)"]) is None

    async def test_a_missing_binary_is_unknown(self) -> None:
        assert await command_version(["coder-eval-no-such-binary-7f3a", "--version"]) is None


@posix_only
@pytest.mark.parametrize(("cls", "kind"), [(PiAgent, AgentKind.PI), (OpenCodeAgent, AgentKind.OPENCODE)])
async def test_a_cli_harness_runs_its_executable_on_the_turn_path(
    cls: type[PiAgent] | type[OpenCodeAgent], kind: AgentKind, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / cls.executable
    fake.write_text(f"#!/bin/sh\n[ \"$1\" = --version ] && echo '{cls.executable} 9.9.9'\n")
    fake.chmod(0o755)
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: str(bin_dir / name))
    agent = cls(parse_agent_config(type=kind, model="provider/model"), task_id="t1")  # type: ignore[arg-type]
    await agent.start(str(tmp_path / "work"), env_path_prepend=[str(bin_dir)])
    try:
        assert await agent.harness_version() == f"{cls.executable} 9.9.9"
    finally:
        await agent.stop()


async def test_claude_code_names_the_sdk_and_its_cli() -> None:
    agent = ClaudeCodeAgent(parse_agent_config(type=AgentKind.CLAUDE_CODE))
    reported = await agent.harness_version()
    assert reported is not None
    assert reported.startswith(f"claude-agent-sdk {version('claude-agent-sdk')}; Claude Code ")


async def test_codex_names_the_sdk_and_the_app_server_from_the_handshake() -> None:
    agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
    agent.codex_client = SimpleNamespace(metadata=SimpleNamespace(serverInfo=SimpleNamespace(version="0.200.0")))
    assert await agent.harness_version() == f"openai-codex {version('openai-codex')}; codex app-server 0.200.0"


async def test_antigravity_names_the_sdk_package() -> None:
    agent = AntigravityAgent(parse_agent_config(type=AgentKind.ANTIGRAVITY))
    assert await agent.harness_version() == f"google-antigravity {version('google-antigravity')}"


async def test_the_base_default_is_unknown() -> None:
    assert await NoOpAgent(parse_agent_config(type=AgentKind.NONE)).harness_version() is None
