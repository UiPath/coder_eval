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
    include_dialog: bool = False,
    max_file_chars: int = 20_000,
    max_dialog_chars: int = 80_000,
) -> JudgeContextBuilder:
    return JudgeContextBuilder(
        files=files or [],
        include_reference=include_reference,
        include_agent_output=include_agent_output,
        include_tool_calls=include_tool_calls,
        include_dialog=include_dialog,
        max_file_chars=max_file_chars,
        max_dialog_chars=max_dialog_chars,
    )


def _make_turn(
    agent_output: str = "",
    commands: list[CommandTelemetry] | None = None,
    *,
    user_input: str = "x",
    iteration: int = 1,
) -> TurnRecord:
    return TurnRecord(iteration=iteration, user_input=user_input, agent_output=agent_output, commands=commands or [])


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


# --- dialog ---


def test_builder_dialog_collects_all_turns(sandbox: Sandbox) -> None:
    turns = [
        _make_turn(user_input="hello", agent_output="hi", iteration=1),
        _make_turn(user_input="add a button", agent_output="done", iteration=2),
    ]
    ctx = _make_builder(include_dialog=True).build(sandbox, None, turns)
    assert ctx.dialog == [("hello", "hi"), ("add a button", "done")]


def test_builder_dialog_no_turns(sandbox: Sandbox) -> None:
    ctx = _make_builder(include_dialog=True).build(sandbox, None, None)
    assert ctx.dialog == []
    assert any("include_dialog" in n for n in ctx.degraded_notes)


def test_builder_dialog_empty_turns_list(sandbox: Sandbox) -> None:
    ctx = _make_builder(include_dialog=True).build(sandbox, None, [])
    assert ctx.dialog == []
    assert any("include_dialog" in n for n in ctx.degraded_notes)


def test_builder_dialog_truncates_long_messages(sandbox: Sandbox) -> None:
    turn = _make_turn(user_input="u" * 500, agent_output="a" * 500)
    ctx = _make_builder(include_dialog=True, max_file_chars=100).build(sandbox, None, [turn])
    assert len(ctx.dialog) == 1
    user_text, agent_text = ctx.dialog[0]
    assert "... (truncated" in user_text
    assert "... (truncated" in agent_text


def test_builder_dialog_handles_empty_strings(sandbox: Sandbox) -> None:
    turn = _make_turn(user_input="", agent_output="")
    ctx = _make_builder(include_dialog=True).build(sandbox, None, [turn])
    assert ctx.dialog == [("", "")]


def test_builder_dialog_aggregate_budget_drops_trailing_turns(sandbox: Sandbox) -> None:
    # Each turn contributes ~200 chars; budget 500 fits 2 turns then trips on the 3rd.
    turns = [_make_turn(user_input="u" * 100, agent_output="a" * 100, iteration=i) for i in range(1, 5)]
    ctx = _make_builder(include_dialog=True, max_dialog_chars=500).build(sandbox, None, turns)
    assert len(ctx.dialog) == 2
    assert any("dropped 2 trailing turn" in n and "max_dialog_chars=500" in n for n in ctx.degraded_notes)


def test_builder_dialog_first_turn_exceeds_budget_kept(sandbox: Sandbox) -> None:
    # First turn always lands so the judge sees something — the cap kicks in only on additions.
    turns = [_make_turn(user_input="u" * 1000, agent_output="a" * 1000)]
    ctx = _make_builder(include_dialog=True, max_dialog_chars=10).build(sandbox, None, turns)
    assert len(ctx.dialog) == 1
    assert all("dropped" not in n for n in ctx.degraded_notes)


def test_builder_dialog_within_budget_no_note(sandbox: Sandbox) -> None:
    turns = [_make_turn(user_input="hi", agent_output="hello", iteration=i) for i in range(1, 4)]
    ctx = _make_builder(include_dialog=True, max_dialog_chars=80_000).build(sandbox, None, turns)
    assert len(ctx.dialog) == 3
    assert all("dropped" not in n for n in ctx.degraded_notes)


def test_builder_dialog_not_requested(sandbox: Sandbox) -> None:
    turn = _make_turn(user_input="hi", agent_output="hello")
    ctx = _make_builder(include_dialog=False).build(sandbox, None, [turn])
    assert ctx.dialog == []
    assert all("include_dialog" not in n for n in ctx.degraded_notes)


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
