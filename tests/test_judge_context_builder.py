"""Tests for JudgeContextBuilder and scrub_reference."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from coder_eval.evaluation.judge_context import (
    FileBlock,
    JudgeContextBuilder,
    format_details,
    scrub_reference,
    truncate,
)
from coder_eval.models import CommandTelemetry, TurnRecord
from coder_eval.sandbox import Sandbox


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    from coder_eval.models import SandboxConfig

    sb = Sandbox(SandboxConfig(driver="tempdir"), task_id="judge_ctx_test")
    sb.sandbox_dir = tmp_path
    return sb


def _make_builder(
    *,
    files: list[str] | None = None,
    include_reference: bool = False,
    include_agent_output: bool = False,
    include_tool_calls: bool = False,
    max_file_chars: int = 20_000,
) -> JudgeContextBuilder:
    return JudgeContextBuilder(
        files=files or [],
        include_reference=include_reference,
        include_agent_output=include_agent_output,
        include_tool_calls=include_tool_calls,
        max_file_chars=max_file_chars,
    )


def _make_turn(agent_output: str = "", commands: list[CommandTelemetry] | None = None) -> TurnRecord:
    return TurnRecord(iteration=1, user_input="x", agent_output=agent_output, commands=commands or [])


def _make_cmd(tool_name: str, params: dict[str, object], seq: int = 0) -> CommandTelemetry:
    return CommandTelemetry(
        tool_name=tool_name,
        tool_id=f"t{seq}",
        timestamp=datetime.now(),
        parameters=params,
        result_status="success",
        sequence_number=seq,
    )


# --- files ---


def test_builder_empty_inputs(sandbox: Sandbox) -> None:
    ctx = _make_builder().build(sandbox, None, None)
    assert ctx.files == []
    assert ctx.missing_files == []
    assert ctx.degraded_notes == []
    assert ctx.reference is None
    assert ctx.agent_output is None
    assert ctx.tool_calls_summary is None


def test_builder_single_present_file(sandbox: Sandbox, tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hi')")
    ctx = _make_builder(files=["main.py"]).build(sandbox, None, None)
    assert len(ctx.files) == 1
    assert ctx.files[0].path == "main.py"
    assert ctx.files[0].content == "print('hi')"
    assert ctx.missing_files == []


def test_builder_missing_file(sandbox: Sandbox) -> None:
    ctx = _make_builder(files=["missing.py"]).build(sandbox, None, None)
    assert ctx.files == [FileBlock(path="missing.py", content=None)]
    assert ctx.missing_files == ["missing.py"]


def test_builder_mixed_present_missing(sandbox: Sandbox, tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1")
    ctx = _make_builder(files=["a.py", "b.py"]).build(sandbox, None, None)
    assert len(ctx.files) == 2
    assert ctx.files[0].content == "x = 1"
    assert ctx.files[1].content is None
    assert ctx.missing_files == ["b.py"]


def test_builder_read_exception_recovers(
    sandbox: Sandbox,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "a.py").write_text("x = 1")

    def boom(self: Sandbox, path: str) -> str:
        raise OSError("disk error")

    monkeypatch.setattr(Sandbox, "get_file_content", boom)
    ctx = _make_builder(files=["a.py"]).build(sandbox, None, None)
    assert ctx.files[0].content is not None
    assert "error reading file" in ctx.files[0].content
    assert ctx.missing_files == []  # file existed; read failed — not tracked as missing


def test_builder_truncates_long_content(sandbox: Sandbox, tmp_path: Path) -> None:
    (tmp_path / "big.py").write_text("x" * 500)
    ctx = _make_builder(files=["big.py"], max_file_chars=100).build(sandbox, None, None)
    assert ctx.files[0].content is not None
    assert "... (truncated" in ctx.files[0].content


def test_builder_no_truncation_at_exact_boundary(sandbox: Sandbox, tmp_path: Path) -> None:
    (tmp_path / "exact.py").write_text("x" * 100)
    ctx = _make_builder(files=["exact.py"], max_file_chars=100).build(sandbox, None, None)
    assert ctx.files[0].content == "x" * 100


# --- reference ---


def test_builder_reference_included(sandbox: Sandbox) -> None:
    ctx = _make_builder(include_reference=True).build(sandbox, "REF_CODE", None)
    assert ctx.reference == "REF_CODE"


def test_builder_reference_requested_but_missing(sandbox: Sandbox) -> None:
    ctx = _make_builder(include_reference=True).build(sandbox, None, None)
    assert ctx.reference is None
    assert ctx.degraded_notes == []  # silent, per legacy behavior


def test_builder_reference_not_requested(sandbox: Sandbox) -> None:
    ctx = _make_builder(include_reference=False).build(sandbox, "REF_CODE", None)
    assert ctx.reference is None


# --- agent output ---


def test_builder_agent_output_included(sandbox: Sandbox) -> None:
    turn = _make_turn(agent_output="I did X")
    ctx = _make_builder(include_agent_output=True).build(sandbox, None, [turn])
    assert ctx.agent_output == "I did X"


def test_builder_agent_output_no_turns(sandbox: Sandbox) -> None:
    ctx = _make_builder(include_agent_output=True).build(sandbox, None, None)
    assert ctx.agent_output is None
    assert any("include_agent_output" in n for n in ctx.degraded_notes)


def test_builder_agent_output_empty_turn(sandbox: Sandbox) -> None:
    turn = _make_turn(agent_output="")
    ctx = _make_builder(include_agent_output=True).build(sandbox, None, [turn])
    assert ctx.agent_output is None
    assert any("latest agent output is empty" in n for n in ctx.degraded_notes)


def test_builder_agent_output_truncated(sandbox: Sandbox) -> None:
    turn = _make_turn(agent_output="y" * 500)
    ctx = _make_builder(include_agent_output=True, max_file_chars=100).build(sandbox, None, [turn])
    assert ctx.agent_output is not None
    assert "... (truncated" in ctx.agent_output


# --- tool calls ---


def test_builder_tool_calls_included(sandbox: Sandbox) -> None:
    turn = _make_turn(commands=[_make_cmd("Bash", {"command": "ls"})])
    ctx = _make_builder(include_tool_calls=True).build(sandbox, None, [turn])
    assert ctx.tool_calls_summary is not None
    assert "Bash" in ctx.tool_calls_summary


def test_builder_tool_calls_no_turns(sandbox: Sandbox) -> None:
    ctx = _make_builder(include_tool_calls=True).build(sandbox, None, None)
    assert ctx.tool_calls_summary is None
    assert any("include_tool_calls" in n for n in ctx.degraded_notes)


def test_builder_tool_calls_empty_commands(sandbox: Sandbox) -> None:
    turn = _make_turn(commands=[])
    ctx = _make_builder(include_tool_calls=True).build(sandbox, None, [turn])
    assert ctx.tool_calls_summary is None
    # Silent omission — matches legacy behavior (zero-command turn isn't a degradation).
    assert all("include_tool_calls" not in n for n in ctx.degraded_notes)


# --- scrub_reference ---


def test_scrub_reference_redacts_when_enabled() -> None:
    assert scrub_reference("a REF b", "REF") == "a <reference redacted> b"


def test_scrub_reference_noop_when_none() -> None:
    assert scrub_reference("text", None) == "text"


def test_scrub_reference_noop_when_empty() -> None:
    # Guards against "".replace("", "<redacted>") which would insert the sentinel between every char.
    assert scrub_reference("text", "") == "text"


# --- truncate ---


def test_truncate_shorter_than_limit() -> None:
    assert truncate("abc", 10) == "abc"


def test_truncate_exact_limit() -> None:
    assert truncate("abcde", 5) == "abcde"


def test_truncate_over_limit() -> None:
    out = truncate("x" * 20, 10)
    assert out.startswith("x" * 10)
    assert "truncated, orig 20 chars" in out


# --- format_details ---


def test_format_details_basic() -> None:
    out = format_details(0.5, "ok", [], [])
    assert "score=0.500" in out
    assert "rationale: ok" in out
    assert "missing_files" not in out
    assert "notes" not in out


def test_format_details_with_missing_and_notes() -> None:
    out = format_details(0.5, "ok", ["foo.py"], ["note1", "note2"])
    assert "missing_files: ['foo.py']" in out
    assert "notes: note1; note2" in out
