"""Tests for SubAgentRunner — sandbox-copy isolation + subprocess lifecycle."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coder_eval.errors.timeout import TurnTimeoutError
from coder_eval.evaluation.sub_agent import (
    SubAgentRunner,
    UnsupportedRouteError,
    _ignore_patterns_and_symlinks,
)
from coder_eval.models import AgentConfig, AgentKind, TurnRecord
from coder_eval.models.routing import DirectRoute, ProxyRoute
from coder_eval.sandbox import Sandbox


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    from coder_eval.models import SandboxConfig

    sb = Sandbox(SandboxConfig(driver="tempdir"), task_id="sub_agent_test")
    sb.sandbox_dir = tmp_path
    return sb


def _make_agent_config() -> AgentConfig:
    return AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        model="claude-opus-4-6",
        max_turns=3,
        turn_timeout=30,
        permission_mode="bypassPermissions",
        allowed_tools=["Read"],
        system_prompt="x",
        setting_sources=[],  # security contract
    )


def _make_turn() -> TurnRecord:
    return TurnRecord(iteration=1, user_input="x", agent_output='{"score": 1.0, "rationale": "ok"}')


def _make_mock_agent() -> MagicMock:
    agent = MagicMock()
    agent.start = AsyncMock(return_value=None)
    agent.communicate = AsyncMock(return_value=_make_turn())
    agent.stop = AsyncMock(return_value=None)
    agent.kill = AsyncMock(return_value=None)
    return agent


# --- happy path + sandbox isolation ---


def test_runner_happy_path(sandbox: Sandbox, tmp_path: Path) -> None:
    (tmp_path / "Main.xaml").write_text("<x/>")

    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        ignore_patterns=[],
        route=DirectRoute(),
    )
    mock_agent = _make_mock_agent()
    with patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent):
        turn = runner.run("grade this", turn_timeout=30.0)

    assert turn.agent_output == '{"score": 1.0, "rationale": "ok"}'
    mock_agent.start.assert_awaited_once()
    mock_agent.communicate.assert_awaited_once_with("grade this", timeout=30.0)
    mock_agent.stop.assert_awaited()


def test_runner_starts_in_temp_copy_not_original(sandbox: Sandbox, tmp_path: Path) -> None:
    (tmp_path / "Main.xaml").write_text("<x/>")

    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        ignore_patterns=[],
        route=DirectRoute(),
    )
    mock_agent = _make_mock_agent()
    with patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent):
        runner.run("grade", turn_timeout=30.0)

    start_arg = mock_agent.start.call_args.args[0]
    assert start_arg != str(sandbox.sandbox_dir)
    assert "sub_agent_" in start_arg


def test_runner_cleans_up_on_success(sandbox: Sandbox) -> None:
    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        ignore_patterns=[],
        route=DirectRoute(),
    )
    mock_agent = _make_mock_agent()
    captured: dict[str, str] = {}

    async def capture_start(path: str) -> None:
        captured["path"] = path

    mock_agent.start.side_effect = capture_start
    with patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent):
        runner.run("grade", turn_timeout=30.0)

    assert captured["path"]
    assert not Path(captured["path"]).exists()


def test_runner_cleans_up_on_communicate_exception(sandbox: Sandbox) -> None:
    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        ignore_patterns=[],
        route=DirectRoute(),
    )
    mock_agent = _make_mock_agent()
    mock_agent.communicate.side_effect = RuntimeError("boom")
    captured: dict[str, str] = {}

    async def capture_start(path: str) -> None:
        captured["path"] = path

    mock_agent.start.side_effect = capture_start

    with (
        patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent),
        pytest.raises(RuntimeError, match="boom"),
    ):
        runner.run("grade", turn_timeout=30.0)

    assert not Path(captured["path"]).exists()
    mock_agent.kill.assert_awaited()
    mock_agent.stop.assert_awaited()


def test_runner_cleans_up_on_start_failure(sandbox: Sandbox) -> None:
    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        ignore_patterns=[],
        route=DirectRoute(),
    )
    mock_agent = _make_mock_agent()
    mock_agent.start.side_effect = RuntimeError("claude binary not found")

    with (
        patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent),
        pytest.raises(RuntimeError, match="claude binary not found"),
    ):
        runner.run("grade", turn_timeout=30.0)

    mock_agent.kill.assert_awaited()


def test_runner_propagates_turn_timeout(sandbox: Sandbox) -> None:
    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        ignore_patterns=[],
        route=DirectRoute(),
    )
    mock_agent = _make_mock_agent()
    mock_agent.communicate.side_effect = TurnTimeoutError(30.0, task_id="t", iteration=1)
    captured: dict[str, str] = {}

    async def capture_start(path: str) -> None:
        captured["path"] = path

    mock_agent.start.side_effect = capture_start

    with (
        patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent),
        pytest.raises(TurnTimeoutError),
    ):
        runner.run("grade", turn_timeout=30.0)

    assert not Path(captured["path"]).exists()


# --- PROXY fail-fast ---


def test_runner_proxy_fails_fast(sandbox: Sandbox) -> None:
    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        ignore_patterns=[],
        route=ProxyRoute(port=8080),
    )
    with (
        patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent") as mock_cls,
        pytest.raises(UnsupportedRouteError, match="PROXY"),
    ):
        runner.run("grade", turn_timeout=30.0)
    mock_cls.assert_not_called()


# --- security contract ---


@pytest.mark.parametrize("bad_sources", [None, ["project"]])
def test_runner_asserts_setting_sources_empty(sandbox: Sandbox, bad_sources: list[str] | None) -> None:
    """Security contract: caller must build AgentConfig with setting_sources=[] explicitly."""
    bad_config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        model="claude-opus-4-6",
        max_turns=3,
        turn_timeout=30,
        permission_mode="bypassPermissions",
        allowed_tools=["Read"],
        system_prompt="x",
        setting_sources=bad_sources,  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="setting_sources"):
        SubAgentRunner(
            sandbox=sandbox,
            agent_config=bad_config,
            ignore_patterns=[],
            route=DirectRoute(),
        )


# --- symlink + pattern filtering (unit + end-to-end) ---


def test_ignore_callable_skips_symlinks(tmp_path: Path) -> None:
    (tmp_path / "regular.txt").write_text("ok")
    (tmp_path / "sub").mkdir()
    (tmp_path / "leak").symlink_to("/etc/passwd")
    (tmp_path / "rel_link").symlink_to("regular.txt")
    (tmp_path / "broken").symlink_to("does_not_exist")

    ignore = _ignore_patterns_and_symlinks(["__pycache__"])
    skipped = ignore(str(tmp_path), ["regular.txt", "sub", "leak", "rel_link", "broken"])

    assert "leak" in skipped
    assert "rel_link" in skipped
    assert "broken" in skipped
    assert "regular.txt" not in skipped
    assert "sub" not in skipped


def test_ignore_callable_still_honors_patterns(tmp_path: Path) -> None:
    (tmp_path / "keep.py").write_text("x")
    (tmp_path / "__pycache__").mkdir()

    ignore = _ignore_patterns_and_symlinks(["__pycache__"])
    skipped = ignore(str(tmp_path), ["keep.py", "__pycache__"])

    assert "__pycache__" in skipped
    assert "keep.py" not in skipped


def test_runner_copytree_drops_top_level_symlinks(sandbox: Sandbox, tmp_path: Path) -> None:
    """End-to-end: a malicious symlink in the sandbox does not land in the judge workspace."""
    import os

    (tmp_path / "real.txt").write_text("payload")
    (tmp_path / "leak").symlink_to("/etc/passwd")

    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        ignore_patterns=[],
        route=DirectRoute(),
    )
    mock_agent = _make_mock_agent()
    captured: dict[str, str] = {}

    async def capture_start(path: str) -> None:
        captured["entries"] = ",".join(sorted(os.listdir(path)))

    mock_agent.start.side_effect = capture_start
    with patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent):
        runner.run("grade", turn_timeout=30.0)

    assert "real.txt" in captured["entries"]
    assert "leak" not in captured["entries"]


def test_runner_copytree_drops_nested_symlinks(sandbox: Sandbox, tmp_path: Path) -> None:
    """Nested symlinks are stripped too, not just top-level ones."""
    import os

    (tmp_path / "real.txt").write_text("payload")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "keep.txt").write_text("kept")
    (tmp_path / "sub" / "nested_leak").symlink_to("/etc/passwd")

    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        ignore_patterns=[],
        route=DirectRoute(),
    )
    mock_agent = _make_mock_agent()
    captured: dict[str, str] = {}

    async def capture_start(path: str) -> None:
        captured["sub_entries"] = ",".join(sorted(os.listdir(Path(path, "sub"))))

    mock_agent.start.side_effect = capture_start
    with patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent):
        runner.run("grade", turn_timeout=30.0)

    assert "keep.txt" in captured["sub_entries"]
    assert "nested_leak" not in captured["sub_entries"]


def test_runner_copytree_honors_patterns(sandbox: Sandbox, tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (tmp_path / "Main.xaml").write_text("<x/>")

    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        ignore_patterns=[".git"],
        route=DirectRoute(),
    )
    mock_agent = _make_mock_agent()
    captured: dict[str, str] = {}

    async def capture_start(path: str) -> None:
        captured["has_git"] = str(Path(path, ".git").exists())
        captured["has_main"] = str(Path(path, "Main.xaml").exists())

    mock_agent.start.side_effect = capture_start
    with patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent):
        runner.run("grade", turn_timeout=30.0)

    assert captured["has_git"] == "False"
    assert captured["has_main"] == "True"


def test_runner_excludes_nested_claude_and_mcp(sandbox: Sandbox, tmp_path: Path) -> None:
    """Nested .claude/ and .mcp.json planted by a compromised agent must not reach the copy."""
    import os

    (tmp_path / "Main.xaml").write_text("<x/>")
    (tmp_path / "sub").mkdir()
    nested_claude = tmp_path / "sub" / ".claude"
    nested_claude.mkdir()
    (nested_claude / "settings.json").write_text('{"hooks": {}}')
    (tmp_path / "sub" / ".mcp.json").write_text("{}")

    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        ignore_patterns=[".claude", ".mcp.json"],
        route=DirectRoute(),
    )
    mock_agent = _make_mock_agent()
    captured: dict[str, str] = {}

    async def capture_start(path: str) -> None:
        captured["sub_entries"] = ",".join(sorted(os.listdir(Path(path, "sub"))))
        captured["main_present"] = str(Path(path, "Main.xaml").exists())

    mock_agent.start.side_effect = capture_start
    with patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent):
        runner.run("grade", turn_timeout=30.0)

    assert ".claude" not in captured["sub_entries"]
    assert ".mcp.json" not in captured["sub_entries"]
    assert captured["main_present"] == "True"
