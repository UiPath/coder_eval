"""Tests for JudgeContextBuilder and scrub_reference."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from coder_eval.evaluation.judge_context import (
    FileBlock,
    JudgeContextBuilder,
    collect_reference_secrets,
    format_details,
    scrub_reference,
    truncate,
)
from coder_eval.models import CommandTelemetry, TurnRecord
from coder_eval.sandbox import Sandbox


_SKIP_NO_SYMLINK = pytest.mark.skipif(
    os.name == "nt",
    reason="Symlink creation on Windows requires admin or Developer Mode; not asserted in CI.",
)


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


def test_builder_task_dir_token_resolves_to_host_file(tmp_path: Path) -> None:
    """`$TASK_DIR/...` reads from the task YAML's parent dir, not the sandbox."""
    from coder_eval.models import SandboxConfig

    task_dir = tmp_path / "tasks" / "subdir"
    task_dir.mkdir(parents=True)
    (task_dir.parent / "rubric.md").write_text("RUBRIC BODY")

    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    sb = Sandbox(SandboxConfig(driver="tempdir"), task_id="t", task_dir=task_dir)
    sb.sandbox_dir = sandbox_dir

    ctx = _make_builder(files=["$TASK_DIR/../rubric.md"]).build(sb, None, None)
    assert len(ctx.files) == 1
    assert ctx.files[0].path == "$TASK_DIR/../rubric.md"
    assert ctx.files[0].content == "RUBRIC BODY"
    assert ctx.missing_files == []


def test_builder_task_dir_token_missing_host_file(tmp_path: Path) -> None:
    """A `$TASK_DIR/...` reference that doesn't resolve is tracked as missing."""
    from coder_eval.models import SandboxConfig

    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    sb = Sandbox(SandboxConfig(driver="tempdir"), task_id="t", task_dir=task_dir)
    sb.sandbox_dir = sandbox_dir

    ctx = _make_builder(files=["$TASK_DIR/nope.md"]).build(sb, None, None)
    assert ctx.files == [FileBlock(path="$TASK_DIR/nope.md", content=None)]
    assert ctx.missing_files == ["$TASK_DIR/nope.md"]


def test_builder_task_dir_token_no_task_dir_treated_as_missing(tmp_path: Path) -> None:
    """When the runner has no task_dir context, `$TASK_DIR/...` records as missing."""
    from coder_eval.models import SandboxConfig

    sb = Sandbox(SandboxConfig(driver="tempdir"), task_id="t")  # task_dir defaults to None
    sb.sandbox_dir = tmp_path

    ctx = _make_builder(files=["$TASK_DIR/anything.md"]).build(sb, None, None)
    assert ctx.missing_files == ["$TASK_DIR/anything.md"]
    assert ctx.files[0].content is None


def test_builder_non_token_path_uses_sandbox(tmp_path: Path) -> None:
    """A path without the token still reads from the sandbox even when task_dir is set."""
    from coder_eval.models import SandboxConfig

    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    (task_dir / "rubric.md").write_text("HOST")  # exists on host, NOT in sandbox

    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    (sandbox_dir / "main.py").write_text("SANDBOX")
    sb = Sandbox(SandboxConfig(driver="tempdir"), task_id="t", task_dir=task_dir)
    sb.sandbox_dir = sandbox_dir

    # Plain "rubric.md" must not accidentally pick up the host file.
    ctx = _make_builder(files=["rubric.md", "main.py"]).build(sb, None, None)
    paths = {f.path: f.content for f in ctx.files}
    assert paths["rubric.md"] is None
    assert paths["main.py"] == "SANDBOX"
    assert ctx.missing_files == ["rubric.md"]


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


def test_builder_reference_included(sandbox: Sandbox, tmp_path: Path) -> None:
    """The whole reference directory is rendered as one labelled-per-file block."""
    ref = tmp_path / "ref"
    (ref / "pkg").mkdir(parents=True)
    (ref / "solution.py").write_text("REF_CODE", encoding="utf-8")
    (ref / "pkg" / "helper.py").write_text("HELPER_CODE", encoding="utf-8")

    ctx = _make_builder(include_reference=True).build(sandbox, ref, None)

    assert ctx.reference is not None
    assert "--- solution.py ---" in ctx.reference
    assert "REF_CODE" in ctx.reference
    # Nested files are labelled by their path relative to the reference root.
    assert "--- pkg/helper.py ---" in ctx.reference
    assert "HELPER_CODE" in ctx.reference


def test_builder_reference_included_empty_dir_is_treated_as_absent(sandbox: Sandbox, tmp_path: Path) -> None:
    """An empty reference dir must not attach an empty REFERENCE SOLUTION block."""
    ref = tmp_path / "empty_ref"
    ref.mkdir()
    assert _make_builder(include_reference=True).build(sandbox, ref, None).reference is None


def test_builder_reference_requested_but_missing(sandbox: Sandbox) -> None:
    ctx = _make_builder(include_reference=True).build(sandbox, None, None)
    assert ctx.reference is None
    assert ctx.degraded_notes == []  # silent, per legacy behavior


def test_builder_reference_not_requested(sandbox: Sandbox, tmp_path: Path) -> None:
    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "solution.py").write_text("REF_CODE", encoding="utf-8")
    assert _make_builder(include_reference=False).build(sandbox, ref, None).reference is None


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
    # Secrets shorter than 8 chars are skipped to avoid mangling unrelated common substrings;
    # use a realistic-length sentinel here.
    secret = "REF_SOLUTION_BLOCK_42"
    assert scrub_reference(f"a {secret} b", secret) == "a <reference redacted> b"


def test_scrub_reference_skips_secrets_below_min_length() -> None:
    # 7 chars and shorter are no-op; redacting a tiny common substring would
    # produce gibberish and isn't a realistic leak vector.
    assert scrub_reference("a REF b", "REF") == "a REF b"
    assert scrub_reference("hello1", "hello1") == "hello1"  # 6 chars


def test_scrub_runs_before_clip_so_partial_secrets_dont_survive() -> None:
    """SECURITY regression for bug_001: scrub must run BEFORE clipping, not after.

    scrub_reference uses str.replace which only matches the secret as a contiguous
    whole string. If the budget clips the prompt mid-secret, the surviving prefix
    no longer matches the full secret string — replace finds nothing — and a
    partial reference fragment is persisted unsanitized.

    Concrete trigger: a multi-KB reference is inlined into the prompt envelope
    by ``include_reference=True``. The transcript budget forces clipping. The
    surviving prefix of the prompt contains the leading portion of the reference
    content. Scrub-before-clip ensures the secret is redacted while still
    present in full, so the post-clip prompt cannot leak any portion.
    """
    from coder_eval.evaluation.judge_context import build_judge_transcript

    secret = "REFERENCE_SOLUTION_BLOCK_" + "A" * 5_000  # 5K-char secret
    # Full secret inlined into the prompt, exactly as ``_render_user_message``
    # does for ``include_reference=true`` with a code/file reference.
    prompt_text = f"REFERENCE SOLUTION:\n```\n{secret}\n```\n\nGRADING PROMPT: ..."

    transcript = build_judge_transcript(
        raw_verdict='{"score": 0.5, "rationale": "ok"}',
        judge_prompt=prompt_text,
        judge_system_prompt="strict reviewer",
        max_chars=500,  # tight budget: forces clipping of the long prompt
        scrub_key=secret,
    )

    # The post-clip prompt MUST NOT contain any portion of the secret payload —
    # scrub-before-clip replaced the full secret with the short marker before
    # any character-budget truncation could fragment it.
    assert "REFERENCE_SOLUTION_BLOCK" not in transcript.judge_prompt
    secret_payload = "A" * 100  # any 100-char run of the secret body
    assert secret_payload not in transcript.judge_prompt
    # Confirm the redaction marker is what survived.
    assert "<reference redacted>" in transcript.judge_prompt


def test_scrub_reference_accepts_iterable_of_secrets() -> None:
    """Directory references produce a list of secrets — every file's content."""
    secrets = ["FIRST_SECRET_LONG_AAAAA", "SECOND_SECRET_LONG_BBBBB"]
    text = "open FIRST_SECRET_LONG_AAAAA and also SECOND_SECRET_LONG_BBBBB"
    out = scrub_reference(text, secrets)
    assert "FIRST_SECRET_LONG_AAAAA" not in out
    assert "SECOND_SECRET_LONG_BBBBB" not in out
    assert out.count("<reference redacted>") == 2


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


# --- collect_reference_secrets ---


def test_collect_reference_secrets_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert collect_reference_secrets(tmp_path / "nope") == []


def test_collect_reference_secrets_collects_file_contents(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("contents of a", encoding="utf-8")
    (tmp_path / "b.py").write_text("contents of b", encoding="utf-8")
    secrets = collect_reference_secrets(tmp_path)
    assert set(secrets) == {"contents of a", "contents of b"}


@_SKIP_NO_SYMLINK
def test_collect_reference_secrets_skips_symlinks(tmp_path: Path) -> None:
    """SECURITY: symlinks inside the reference dir must NOT be followed.

    A reference bundle that ships ``secrets -> /etc/passwd`` would otherwise
    read the host file into the scrub-key list (and quietly grow it), and a
    symlink loop would hang the walk. Skipping symlinks both ways closes both.
    """
    real = tmp_path / "real.txt"
    real.write_text("real file contents", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    loop = tmp_path / "loop"
    loop.symlink_to(tmp_path)  # symlinked subdir back to root — would loop on rglob if followed
    secrets = collect_reference_secrets(tmp_path)
    assert secrets == ["real file contents"]


def test_collect_reference_secrets_bounded_by_file_count(tmp_path: Path) -> None:
    """A reference dir with many files must not load all of them into memory unbounded."""
    for i in range(500):
        (tmp_path / f"file_{i:03d}.txt").write_text(f"content {i}", encoding="utf-8")
    secrets = collect_reference_secrets(tmp_path)
    # Cap is well below 500. The exact value is implementation-defined; assert
    # we stopped *before* reading every file rather than locking in the number.
    assert 0 < len(secrets) < 500


def test_collect_reference_secrets_bounded_by_total_bytes(tmp_path: Path) -> None:
    """A reference dir with a few large files must not load megabytes into memory."""
    big = "x" * (512 * 1024)  # 512 KB per file
    for i in range(10):  # 5 MB total, easily over any reasonable budget
        (tmp_path / f"big_{i}.bin").write_text(big, encoding="utf-8")
    secrets = collect_reference_secrets(tmp_path)
    total_bytes = sum(len(s) for s in secrets)
    # We expect the budget to clamp below the full 5 MB.
    assert total_bytes < 5 * 1024 * 1024


def test_collect_reference_secrets_skips_oversized_file_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single file larger than the remaining budget is rejected before ``read_text``.

    Regression guard for the latent OOM where ``read_text`` ran unconditionally
    on every file and the size check fired only on the *next* iteration — a
    single 100 MB file in an otherwise small reference dir would be loaded in
    full before the loop noticed the budget was blown.
    """
    monkeypatch.setattr("coder_eval.evaluation.judge_context._MAX_REFERENCE_BYTES", 1024)
    big = tmp_path / "huge.bin"
    big.write_text("y" * 4096, encoding="utf-8")  # 4 KB — exceeds the patched 1 KB budget
    small = tmp_path / "small.txt"
    small.write_text("small content here", encoding="utf-8")

    # Mock read_text to track that it WAS NOT called on the oversized file.
    real_read_text = Path.read_text
    read_calls: list[Path] = []

    def tracking_read_text(self: Path, *args: object, **kwargs: object) -> str:
        read_calls.append(self)
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", tracking_read_text)
    secrets = collect_reference_secrets(tmp_path)
    # The 4 KB file must NOT have been opened (the pre-check rejects it).
    assert big not in read_calls
    # The small file may or may not have been read depending on rglob order;
    # what matters is the budget was not silently blown.
    assert all(len(s) <= 4096 for s in secrets)
    # And the result list never carries the 4 KB content.
    assert not any(len(s) > 1024 for s in secrets)


def test_collect_reference_secrets_exact_fit_single_file_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single file sized at exactly ``_MAX_REFERENCE_BYTES`` is accepted.

    Locks in the ``file_size > remaining`` (strict gt) boundary — a regression
    flipping to ``>=`` would reject the exact-fit file and this test fails.
    """
    monkeypatch.setattr("coder_eval.evaluation.judge_context._MAX_REFERENCE_BYTES", 10)
    (tmp_path / "exact.txt").write_text("a" * 10, encoding="utf-8")
    secrets = collect_reference_secrets(tmp_path)
    assert secrets == ["a" * 10]


def test_collect_reference_secrets_one_byte_over_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A single file one byte larger than the budget is rejected before read."""
    monkeypatch.setattr("coder_eval.evaluation.judge_context._MAX_REFERENCE_BYTES", 10)
    (tmp_path / "oversize.txt").write_text("a" * 11, encoding="utf-8")
    secrets = collect_reference_secrets(tmp_path)
    assert secrets == []


def test_collect_reference_secrets_stat_oserror_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``Path.stat`` raising OSError on the size-check (file disappeared mid-walk) → skip.

    ``stat()`` is called by ``is_symlink()`` / ``is_file()`` internally with
    ``follow_symlinks=False``; the explicit size-check call passes no kwargs.
    Raise only on the latter so the pre-checks succeed and the OSError lands on
    the path we're guarding.
    """
    (tmp_path / "real.txt").write_text("real content", encoding="utf-8")
    (tmp_path / "ghost.txt").write_text("ghost content", encoding="utf-8")

    real_stat = Path.stat

    def flaky_stat(self: Path, *args: object, **kwargs: object) -> object:
        # is_symlink / is_file go through stat with follow_symlinks kwarg; let
        # those through. Only fail on the explicit no-arg call used for size.
        if self.name == "ghost.txt" and not args and not kwargs:
            raise OSError("file vanished")
        return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", flaky_stat)
    secrets = collect_reference_secrets(tmp_path)
    assert "real content" in secrets
    assert "ghost content" not in secrets


def test_collect_host_file_reads_utf8(tmp_path: Path) -> None:
    """Host-side file reads pass ``encoding='utf-8'`` so non-ASCII content survives.

    Without the explicit encoding, ``read_text()`` falls back to the platform
    locale — ``cp1252`` on Windows — and a ``é`` mojibakes. This test plants a
    UTF-8 file containing a non-ASCII character and reads it through the
    ``$TASK_DIR`` host-file path.
    """
    from coder_eval.models import SandboxConfig

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    host_file = task_dir / "rubric.md"
    host_file.write_text("Café — rationale\nμ test", encoding="utf-8")

    sb_dir = tmp_path / "sandbox"
    sb_dir.mkdir()
    sb = Sandbox(SandboxConfig(driver="tempdir"), task_id="utf8_test", task_dir=task_dir)
    sb.sandbox_dir = sb_dir

    builder = _make_builder(files=["$TASK_DIR/rubric.md"], include_reference=False, max_file_chars=200)
    ctx = builder.build(sb, reference_dir=None, turn_records=None)
    assert ctx.files[0].content == "Café — rationale\nμ test"
