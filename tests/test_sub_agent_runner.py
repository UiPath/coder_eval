"""Tests for SubAgentRunner — sandbox-copy isolation + subprocess lifecycle."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coder_eval.errors.timeout import TurnTimeoutError
from coder_eval.evaluation.sub_agent import (
    SubAgentRunner,
    ignore_patterns_and_symlinks,
)
from coder_eval.models import AgentKind, ClaudeCodeAgentConfig, TurnRecord, parse_agent_config
from coder_eval.models.routing import DirectRoute
from coder_eval.sandbox import Sandbox


# Symlink creation on Windows requires either admin privileges or Developer
# Mode enabled; CI runners usually have neither. Mark the tests that rely on
# os.symlink so they skip cleanly there.
_SKIP_NO_SYMLINK = pytest.mark.skipif(
    os.name == "nt",
    reason="Symlink creation on Windows requires admin or Developer Mode; not asserted in CI.",
)


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    from coder_eval.models import SandboxConfig

    sb = Sandbox(SandboxConfig(driver="tempdir"), task_id="sub_agent_test")
    sb.sandbox_dir = tmp_path
    return sb


def _make_agent_config() -> ClaudeCodeAgentConfig:
    return cast(
        ClaudeCodeAgentConfig,
        parse_agent_config(
            type=AgentKind.CLAUDE_CODE,
            model="claude-opus-4-6",
            permission_mode="bypassPermissions",
            allowed_tools=["Read"],
            system_prompt="x",
            setting_sources=[],  # security contract
        ),
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


async def test_runner_happy_path(sandbox: Sandbox, tmp_path: Path) -> None:
    (tmp_path / "Main.xaml").write_text("<x/>")

    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        ignore_patterns=[],
        route=DirectRoute(),
    )
    mock_agent = _make_mock_agent()
    with patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent):
        turn = await runner.run_async("grade this", max_turns=10, turn_timeout=30.0)

    assert turn.agent_output == '{"score": 1.0, "rationale": "ok"}'
    mock_agent.start.assert_awaited_once()
    mock_agent.communicate.assert_awaited_once_with("grade this", timeout=30.0, max_turns=10)
    mock_agent.stop.assert_awaited()


async def test_runner_mounts_reference_dir_at_underscore_reference(sandbox: Sandbox, tmp_path: Path) -> None:
    """When reference_dir is provided, copy it into the judge's working dir at _reference/."""
    (tmp_path / "Main.xaml").write_text("<x/>")

    # Pre-stage a reference solution outside the sandbox.
    ref_root = tmp_path / "outside_ref"
    ref_root.mkdir()
    (ref_root / "Main.xaml").write_text("<reference/>")
    (ref_root / "project.json").write_text('{"name":"reference"}')
    (ref_root / "subdir").mkdir()
    (ref_root / "subdir" / "Helper.xaml").write_text("<helper/>")

    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        ignore_patterns=[],
        route=DirectRoute(),
        reference_dir=ref_root,
    )
    mock_agent = _make_mock_agent()
    captured: dict[str, str] = {}

    async def capture_start(workdir: str, **_kwargs: object) -> None:
        captured["workdir"] = workdir

    mock_agent.start.side_effect = capture_start
    with patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent):
        # Capture the workdir before it's torn down by the finally block.
        async def capture_files(_msg: str, **_kw: object) -> TurnRecord:
            workdir = Path(captured["workdir"])
            captured["has_reference_dir"] = str((workdir / "_reference").is_dir())
            captured["has_main"] = str((workdir / "_reference" / "Main.xaml").is_file())
            captured["main_content"] = (workdir / "_reference" / "Main.xaml").read_text()
            captured["has_subdir"] = str((workdir / "_reference" / "subdir" / "Helper.xaml").is_file())
            return _make_turn()

        mock_agent.communicate.side_effect = capture_files
        await runner.run_async("grade", max_turns=10, turn_timeout=30.0)

    assert captured["has_reference_dir"] == "True"
    assert captured["has_main"] == "True"
    assert captured["main_content"] == "<reference/>"
    assert captured["has_subdir"] == "True"


async def test_runner_handles_sandbox_side_reference_collision(sandbox: Sandbox, tmp_path: Path) -> None:
    """Regression for bug_002: when the sandbox already contains _reference/
    (template-staged or agent-planted), the second copytree must not raise
    FileExistsError. Default ignore_patterns includes _reference, and the
    runner rmtree's the destination as a defense-in-depth safety net."""
    # Pre-stage a sandbox that already contains _reference/ — typical of an
    # agent that decided to create a directory with that exact name.
    sandbox_ref = tmp_path / "_reference"
    sandbox_ref.mkdir()
    (sandbox_ref / "agent_planted.txt").write_text("PLANTED-CONTENT-FROM-AGENT")
    (tmp_path / "Main.xaml").write_text("<x/>")

    # Real reference solution lives outside the sandbox.
    real_ref = tmp_path.parent / "real_reference"
    real_ref.mkdir()
    (real_ref / "Main.xaml").write_text("<reference/>")

    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        # Default agent_judge ignore_patterns include _reference; emulate that here.
        ignore_patterns=["_reference"],
        route=DirectRoute(),
        reference_dir=real_ref,
    )
    mock_agent = _make_mock_agent()
    captured: dict[str, str] = {}

    async def capture_start(workdir: str, **_kwargs: object) -> None:
        captured["workdir"] = workdir

    mock_agent.start.side_effect = capture_start
    with patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent):

        async def capture_state(_msg: str, **_kw: object) -> TurnRecord:
            workdir = Path(captured["workdir"])
            ref_main = workdir / "_reference" / "Main.xaml"
            captured["ref_main_content"] = ref_main.read_text()
            captured["agent_planted_present"] = str((workdir / "_reference" / "agent_planted.txt").exists())
            return _make_turn()

        mock_agent.communicate.side_effect = capture_state
        # Must not raise — this is the regression assertion.
        await runner.run_async("grade", max_turns=10, turn_timeout=30.0)

    # _reference/ contains the REAL reference content, NOT the agent-planted file.
    assert captured["ref_main_content"] == "<reference/>"
    assert captured["agent_planted_present"] == "False"


async def test_runner_skips_reference_when_not_provided(sandbox: Sandbox, tmp_path: Path) -> None:
    """No reference_dir → no _reference/ in the judge's working directory."""
    (tmp_path / "Main.xaml").write_text("<x/>")

    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        ignore_patterns=[],
        route=DirectRoute(),
        # reference_dir defaults to None
    )
    mock_agent = _make_mock_agent()
    captured: dict[str, str] = {}

    async def capture_start(workdir: str, **_kwargs: object) -> None:
        captured["workdir"] = workdir

    mock_agent.start.side_effect = capture_start
    with patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent):

        async def capture_no_ref(_msg: str, **_kw: object) -> TurnRecord:
            workdir = Path(captured["workdir"])
            captured["has_reference_dir"] = str((workdir / "_reference").exists())
            return _make_turn()

        mock_agent.communicate.side_effect = capture_no_ref
        await runner.run_async("grade", max_turns=10, turn_timeout=30.0)

    assert captured["has_reference_dir"] == "False"


async def test_runner_starts_in_temp_copy_not_original(sandbox: Sandbox, tmp_path: Path) -> None:
    (tmp_path / "Main.xaml").write_text("<x/>")

    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        ignore_patterns=[],
        route=DirectRoute(),
    )
    mock_agent = _make_mock_agent()
    with patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent):
        await runner.run_async("grade", max_turns=10, turn_timeout=30.0)

    start_arg = mock_agent.start.call_args.args[0]
    assert start_arg != str(sandbox.sandbox_dir)
    assert "sub_agent_" in start_arg


async def test_runner_cleans_up_on_success(sandbox: Sandbox) -> None:
    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        ignore_patterns=[],
        route=DirectRoute(),
    )
    mock_agent = _make_mock_agent()
    captured: dict[str, str] = {}

    async def capture_start(path: str, **_kwargs: object) -> None:
        captured["path"] = path

    mock_agent.start.side_effect = capture_start
    with patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent):
        await runner.run_async("grade", max_turns=10, turn_timeout=30.0)

    assert captured["path"]
    assert not Path(captured["path"]).exists()


async def test_runner_cleans_up_when_cancelled_mid_communicate(sandbox: Sandbox) -> None:
    """run_async is awaited directly on the orchestrator's own loop (not under its
    own asyncio.run on a worker thread), so a cancellation — e.g. the task_timeout
    watchdog cancelling the orchestrator task — can land while communicate() is in
    flight. The finally-block cleanup must still remove judge_dir (it's a plain
    synchronous shutil.rmtree, not an awaited to_thread call, precisely so it can't
    itself be interrupted by the same cancellation)."""
    import asyncio

    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        ignore_patterns=[],
        route=DirectRoute(),
    )
    mock_agent = _make_mock_agent()
    captured: dict[str, str] = {}
    started = asyncio.Event()

    async def capture_start(path: str, **_kwargs: object) -> None:
        captured["path"] = path

    async def hang_forever(*_args: object, **_kwargs: object) -> TurnRecord:
        started.set()
        await asyncio.sleep(3600)
        raise AssertionError("should have been cancelled before waking up")

    mock_agent.start.side_effect = capture_start
    mock_agent.communicate.side_effect = hang_forever

    with patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent):
        task = asyncio.ensure_future(runner.run_async("grade", max_turns=10, turn_timeout=30.0))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = await task  # the await's effect is propagating the cancellation raised above

    assert captured["path"]
    assert not Path(captured["path"]).exists()


async def test_runner_cleans_up_when_cancelled_mid_copytree(sandbox: Sandbox) -> None:
    """Cancellation reaching mid-``copytree`` (not just mid-``communicate``)
    must not leak ``judge_dir``. Reproduces the exact race the shield +
    finally-awaits-pending fix addresses: ``asyncio.to_thread`` is not itself
    cancellable, so without shielding + waiting for it in ``finally``, an
    orphan worker thread can keep copying files into ``judge_dir`` — and even
    finish AFTER ``finally``'s ``rmtree`` already ran, leaving a full,
    permanent copy behind."""
    import asyncio
    import shutil
    import tempfile
    import threading
    import time

    copy_started = threading.Event()
    real_copytree = shutil.copytree

    def slow_copytree(*args: object, **kwargs: object) -> object:
        copy_started.set()
        time.sleep(0.3)
        return real_copytree(*args, **kwargs)  # type: ignore[arg-type]

    captured_dir: dict[str, Path] = {}
    real_mkdtemp = tempfile.mkdtemp

    def capture_mkdtemp(*args: object, **kwargs: object) -> str:
        d = real_mkdtemp(*args, **kwargs)  # type: ignore[arg-type]
        captured_dir["path"] = Path(d)
        return d

    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        ignore_patterns=[],
        route=DirectRoute(),
    )

    with (
        patch("coder_eval.evaluation.sub_agent.shutil.copytree", side_effect=slow_copytree),
        patch("coder_eval.evaluation.sub_agent.tempfile.mkdtemp", side_effect=capture_mkdtemp),
    ):
        task = asyncio.ensure_future(runner.run_async("grade", max_turns=10, turn_timeout=30.0))
        while not copy_started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = await task

    assert captured_dir.get("path") is not None
    assert not captured_dir["path"].exists(), "orphan copytree thread recreated judge_dir after cleanup already ran"


async def test_runner_cleans_up_on_communicate_exception(sandbox: Sandbox) -> None:
    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        ignore_patterns=[],
        route=DirectRoute(),
    )
    mock_agent = _make_mock_agent()
    mock_agent.communicate.side_effect = RuntimeError("boom")
    captured: dict[str, str] = {}

    async def capture_start(path: str, **_kwargs: object) -> None:
        captured["path"] = path

    mock_agent.start.side_effect = capture_start

    with (
        patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await runner.run_async("grade", max_turns=10, turn_timeout=30.0)

    assert not Path(captured["path"]).exists()
    mock_agent.kill.assert_awaited()
    mock_agent.stop.assert_awaited()


async def test_runner_cleans_up_on_start_failure(sandbox: Sandbox) -> None:
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
        await runner.run_async("grade", max_turns=10, turn_timeout=30.0)

    mock_agent.kill.assert_awaited()


async def test_runner_propagates_turn_timeout(sandbox: Sandbox) -> None:
    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        ignore_patterns=[],
        route=DirectRoute(),
    )
    mock_agent = _make_mock_agent()
    mock_agent.communicate.side_effect = TurnTimeoutError(30.0, task_id="t", iteration=1)
    captured: dict[str, str] = {}

    async def capture_start(path: str, **_kwargs: object) -> None:
        captured["path"] = path

    mock_agent.start.side_effect = capture_start

    with (
        patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent),
        pytest.raises(TurnTimeoutError),
    ):
        await runner.run_async("grade", max_turns=10, turn_timeout=30.0)

    assert not Path(captured["path"]).exists()


# --- security contract ---


@pytest.mark.parametrize("bad_sources", [None, ["project"]])
def test_runner_asserts_setting_sources_empty(sandbox: Sandbox, bad_sources: list[str] | None) -> None:
    """Security contract: caller must build AgentConfig with setting_sources=[] explicitly."""
    bad_config = parse_agent_config(
        type=AgentKind.CLAUDE_CODE,
        model="claude-opus-4-6",
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


@_SKIP_NO_SYMLINK
def test_ignore_callable_skips_symlinks(tmp_path: Path) -> None:
    (tmp_path / "regular.txt").write_text("ok")
    (tmp_path / "sub").mkdir()
    (tmp_path / "leak").symlink_to("/etc/passwd")
    (tmp_path / "rel_link").symlink_to("regular.txt")
    (tmp_path / "broken").symlink_to("does_not_exist")

    ignore = ignore_patterns_and_symlinks(["__pycache__"])
    skipped = ignore(str(tmp_path), ["regular.txt", "sub", "leak", "rel_link", "broken"])

    assert "leak" in skipped
    assert "rel_link" in skipped
    assert "broken" in skipped
    assert "regular.txt" not in skipped
    assert "sub" not in skipped


def test_ignore_callable_still_honors_patterns(tmp_path: Path) -> None:
    (tmp_path / "keep.py").write_text("x")
    (tmp_path / "__pycache__").mkdir()

    ignore = ignore_patterns_and_symlinks(["__pycache__"])
    skipped = ignore(str(tmp_path), ["keep.py", "__pycache__"])

    assert "__pycache__" in skipped
    assert "keep.py" not in skipped


@_SKIP_NO_SYMLINK
async def test_runner_copytree_drops_top_level_symlinks(sandbox: Sandbox, tmp_path: Path) -> None:
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

    async def capture_start(path: str, **_kwargs: object) -> None:
        captured["entries"] = ",".join(sorted(os.listdir(path)))

    mock_agent.start.side_effect = capture_start
    with patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent):
        await runner.run_async("grade", max_turns=10, turn_timeout=30.0)

    assert "real.txt" in captured["entries"]
    assert "leak" not in captured["entries"]


@_SKIP_NO_SYMLINK
async def test_runner_copytree_drops_nested_symlinks(sandbox: Sandbox, tmp_path: Path) -> None:
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

    async def capture_start(path: str, **_kwargs: object) -> None:
        captured["sub_entries"] = ",".join(sorted(os.listdir(Path(path, "sub"))))

    mock_agent.start.side_effect = capture_start
    with patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent):
        await runner.run_async("grade", max_turns=10, turn_timeout=30.0)

    assert "keep.txt" in captured["sub_entries"]
    assert "nested_leak" not in captured["sub_entries"]


async def test_runner_copytree_honors_patterns(sandbox: Sandbox, tmp_path: Path) -> None:
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

    async def capture_start(path: str, **_kwargs: object) -> None:
        captured["has_git"] = str(Path(path, ".git").exists())
        captured["has_main"] = str(Path(path, "Main.xaml").exists())

    mock_agent.start.side_effect = capture_start
    with patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent):
        await runner.run_async("grade", max_turns=10, turn_timeout=30.0)

    assert captured["has_git"] == "False"
    assert captured["has_main"] == "True"


async def test_runner_excludes_nested_claude_and_mcp(sandbox: Sandbox, tmp_path: Path) -> None:
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

    async def capture_start(path: str, **_kwargs: object) -> None:
        captured["sub_entries"] = ",".join(sorted(os.listdir(Path(path, "sub"))))
        captured["main_present"] = str(Path(path, "Main.xaml").exists())

    mock_agent.start.side_effect = capture_start
    with patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent):
        await runner.run_async("grade", max_turns=10, turn_timeout=30.0)

    assert ".claude" not in captured["sub_entries"]
    assert ".mcp.json" not in captured["sub_entries"]
    assert captured["main_present"] == "True"


# --- reference_ignore_patterns split (Phase 4 / finding #9) ---


async def test_runner_reference_dir_with_nested_underscore_reference_preserved(
    sandbox: Sandbox, tmp_path: Path
) -> None:
    """A nested ``_reference/`` inside the user's reference dir is NOT stripped.

    Regression for finding #9: the pre-fix code reused the sandbox-side
    ``ignore_patterns`` (which includes ``_reference``) for the reference-side
    copytree, so a customer who happened to have a nested ``_reference/`` subdir
    in their reference bundle would silently lose it.
    """
    (tmp_path / "Main.xaml").write_text("<x/>")

    real_ref = tmp_path / "real_reference_nested"
    real_ref.mkdir()
    nested = real_ref / "nested" / "_reference"
    nested.mkdir(parents=True)
    (nested / "inner.txt").write_text("CUSTOMER-CONTENT")

    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        # Emulate the agent_judge default: sandbox-side ignores include _reference.
        ignore_patterns=["_reference"],
        route=DirectRoute(),
        reference_dir=real_ref,
        # reference_ignore_patterns omitted → defaults to []
    )
    mock_agent = _make_mock_agent()
    captured: dict[str, str] = {}

    async def capture_start(workdir: str, **_kwargs: object) -> None:
        captured["workdir"] = workdir

    mock_agent.start.side_effect = capture_start
    with patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent):

        async def capture_state(_msg: str, **_kw: object) -> TurnRecord:
            workdir = Path(captured["workdir"])
            inner = workdir / "_reference" / "nested" / "_reference" / "inner.txt"
            captured["inner_present"] = str(inner.exists())
            captured["inner_content"] = inner.read_text(encoding="utf-8") if inner.exists() else ""
            return _make_turn()

        mock_agent.communicate.side_effect = capture_state
        await runner.run_async("grade", max_turns=10, turn_timeout=30.0)

    assert captured["inner_present"] == "True"
    assert captured["inner_content"] == "CUSTOMER-CONTENT"


async def test_runner_reference_ignore_patterns_explicit(sandbox: Sandbox, tmp_path: Path) -> None:
    """Explicit reference_ignore_patterns are honored on the reference-side copy."""
    (tmp_path / "Main.xaml").write_text("<x/>")

    real_ref = tmp_path / "real_reference_ignore"
    real_ref.mkdir()
    (real_ref / "keep.txt").write_text("keep me")
    (real_ref / "drop.log").write_text("noisy log")

    runner = SubAgentRunner(
        sandbox=sandbox,
        agent_config=_make_agent_config(),
        ignore_patterns=[],
        route=DirectRoute(),
        reference_dir=real_ref,
        reference_ignore_patterns=["*.log"],
    )
    mock_agent = _make_mock_agent()
    captured: dict[str, str] = {}

    async def capture_start(workdir: str, **_kwargs: object) -> None:
        captured["workdir"] = workdir

    mock_agent.start.side_effect = capture_start
    with patch("coder_eval.evaluation.sub_agent.ClaudeCodeAgent", return_value=mock_agent):

        async def capture_state(_msg: str, **_kw: object) -> TurnRecord:
            workdir = Path(captured["workdir"])
            captured["keep_present"] = str((workdir / "_reference" / "keep.txt").exists())
            captured["log_present"] = str((workdir / "_reference" / "drop.log").exists())
            return _make_turn()

        mock_agent.communicate.side_effect = capture_state
        await runner.run_async("grade", max_turns=10, turn_timeout=30.0)

    assert captured["keep_present"] == "True"
    assert captured["log_present"] == "False"
