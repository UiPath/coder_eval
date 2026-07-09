"""Tests for the spill/load helpers in ``coder_eval.evaluation.judge_persistence``.

The orchestrator spills judge transcripts to ``judge-<idx>.json`` next to
``task.json`` so the row record stays lean. Re-render paths reload them.
These tests verify the round-trip and back-compat with old runs that
inlined the transcript.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from coder_eval.evaluation.judge_persistence import (
    load_judge_transcripts,
    spill_judge_transcripts,
)
from coder_eval.models import (
    AgentKind,
    CriterionResult,
    EvaluationResult,
    FinalStatus,
    JudgeCriterionResult,
    JudgeTranscript,
    JudgeTranscriptToolCall,
)


def _make_evaluation_result(*, criteria: list[CriterionResult]) -> EvaluationResult:
    return EvaluationResult(
        task_id="t",
        task_description="d",
        variant_id="v",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime(2026, 5, 7, 12, 0, 0),
        final_status=FinalStatus.SUCCESS,
        iteration_count=1,
        environment_info={},
        success_criteria_results=criteria,
    )


def _make_judge_result(
    *,
    score: float = 0.75,
    transcript: JudgeTranscript | None = None,
) -> JudgeCriterionResult:
    return JudgeCriterionResult(
        criterion_type="agent_judge",
        description="grade",
        score=score,
        details=f"score={score:.3f}",
        findings=["finding 1"],
        transcript=transcript,
    )


def _make_transcript() -> JudgeTranscript:
    return JudgeTranscript(
        tool_calls=[
            JudgeTranscriptToolCall(
                tool_name="Read",
                detail="main.xaml",
                status="success",
                result_preview="bytes",
            )
        ],
        duration_seconds=12.3,
        raw_verdict='{"score":0.75,"rationale":"ok"}',
        judge_system_prompt="system",
        judge_prompt="user",
        truncated=False,
    )


# --- spill ---


def test_spill_writes_sibling_file_and_sets_path(tmp_path: Path) -> None:
    import yaml

    judge = _make_judge_result(transcript=_make_transcript())
    result = _make_evaluation_result(criteria=[judge])

    n = spill_judge_transcripts(result, tmp_path)

    assert n == 1
    assert judge.transcript_path == "judge-0.yaml"
    sibling = tmp_path / "judge-0.yaml"
    assert sibling.is_file()
    data = yaml.safe_load(sibling.read_text(encoding="utf-8"))
    assert data["raw_verdict"] == '{"score":0.75,"rationale":"ok"}'
    assert data["judge_prompt"] == "user"
    assert len(data["tool_calls"]) == 1
    # Inline transcript is intentionally left in place — HTML rendering still uses it.
    assert judge.transcript is not None


def test_spill_uses_yaml_block_scalar_for_multiline(tmp_path: Path) -> None:
    """Multi-line strings (judge_prompt etc.) render as readable literal block
    scalars, not as quoted single-line strings with \\n escapes."""
    multi = "line one\nline two\nline three"
    judge = _make_judge_result(
        transcript=JudgeTranscript(
            raw_verdict='{"score":1.0,"rationale":"ok"}',
            judge_prompt=multi,
            judge_system_prompt="",
        )
    )
    result = _make_evaluation_result(criteria=[judge])
    spill_judge_transcripts(result, tmp_path)

    raw_yaml = (tmp_path / "judge-0.yaml").read_text(encoding="utf-8")
    # Block scalar marker for the multi-line judge_prompt; lines render without escapes.
    assert "judge_prompt: |" in raw_yaml
    assert "line one\n" in raw_yaml
    assert "\\n" not in raw_yaml  # not the JSON-escape form


def test_spill_preserves_index_for_multiple_judges(tmp_path: Path) -> None:
    """Spill keys files by criterion index so two judges can coexist."""
    judge1 = _make_judge_result(score=0.6, transcript=_make_transcript())
    judge2 = _make_judge_result(score=0.9, transcript=_make_transcript())
    result = _make_evaluation_result(criteria=[judge1, judge2])

    spill_judge_transcripts(result, tmp_path)

    assert judge1.transcript_path == "judge-0.yaml"
    assert judge2.transcript_path == "judge-1.yaml"
    assert (tmp_path / "judge-0.yaml").is_file()
    assert (tmp_path / "judge-1.yaml").is_file()


def test_spill_skips_non_judge_results(tmp_path: Path) -> None:
    """Plain CriterionResult instances are no-ops — no sibling file written."""
    plain = CriterionResult(
        criterion_type="file_exists",
        description="x",
        score=1.0,
        details="ok",
    )
    result = _make_evaluation_result(criteria=[plain])

    n = spill_judge_transcripts(result, tmp_path)

    assert n == 0
    assert not list(tmp_path.glob("judge-*.*"))


def test_spill_skips_judges_without_transcript(tmp_path: Path) -> None:
    """JudgeCriterionResult with transcript=None doesn't get a file
    (e.g. when capture_transcript was False or the judge errored early)."""
    judge = _make_judge_result(transcript=None)
    result = _make_evaluation_result(criteria=[judge])

    n = spill_judge_transcripts(result, tmp_path)

    assert n == 0
    assert judge.transcript_path is None
    assert not list(tmp_path.glob("judge-*.*"))


def test_spill_creates_output_dir_if_missing(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "nested"
    judge = _make_judge_result(transcript=_make_transcript())
    result = _make_evaluation_result(criteria=[judge])

    spill_judge_transcripts(result, nested)

    assert (nested / "judge-0.yaml").is_file()


# --- load ---


def test_load_reads_sibling_and_attaches_transcript(tmp_path: Path) -> None:
    """The full round trip: spill → reload from JSON → load_judge_transcripts."""
    judge = _make_judge_result(transcript=_make_transcript())
    result = _make_evaluation_result(criteria=[judge])
    spill_judge_transcripts(result, tmp_path)

    # Simulate the re-render path: write task.json with transcript excluded,
    # reload it as a fresh EvaluationResult, then load the transcript back.
    task_json = tmp_path / "task.json"
    task_json.write_text(
        result.model_dump_json(
            indent=2,
            exclude={"success_criteria_results": {"__all__": {"transcript"}}},
        ),
        encoding="utf-8",
    )

    reloaded = EvaluationResult.model_validate_json(task_json.read_text(encoding="utf-8"))
    cr = reloaded.success_criteria_results[0]
    # The discriminated union restores the concrete subclass on reload, so
    # cr is a JudgeCriterionResult with typed fields (not a base CriterionResult
    # with the field in __pydantic_extra__).
    assert isinstance(cr, JudgeCriterionResult)
    assert cr.transcript_path == "judge-0.yaml"
    assert cr.transcript is None

    n = load_judge_transcripts(reloaded, tmp_path)

    assert n == 1
    transcript = cr.transcript
    assert transcript is not None
    # Loader prefers the typed JudgeTranscript so renderer/aggregator code that
    # switches on isinstance(transcript, JudgeTranscript) sees the same shape it
    # gets during the original run. Falls back to dict on ValidationError (see
    # test_load_attaches_dict_when_schema_mismatch).
    from coder_eval.models import JudgeTranscript

    assert isinstance(transcript, JudgeTranscript)
    assert transcript.raw_verdict == '{"score":0.75,"rationale":"ok"}'
    assert transcript.judge_prompt == "user"


def test_load_skips_when_inline_already_present(tmp_path: Path) -> None:
    """When transcript is already inline (orchestrator's own first-render path),
    loader doesn't clobber it."""
    judge = _make_judge_result(transcript=_make_transcript())
    judge.transcript_path = "judge-0.yaml"  # set as if spilled
    # Sibling file exists with different content
    (tmp_path / "judge-0.yaml").write_text(
        '{"raw_verdict":"different","judge_prompt":"different","tool_calls":[],'
        '"duration_seconds":0.0,"judge_system_prompt":"","truncated":false,"token_usage":null}',
        encoding="utf-8",
    )
    result = _make_evaluation_result(criteria=[judge])

    n = load_judge_transcripts(result, tmp_path)

    assert n == 0
    # Inline transcript is preserved, NOT replaced by the (intentionally divergent) file content.
    assert judge.transcript is not None
    assert judge.transcript.raw_verdict == '{"score":0.75,"rationale":"ok"}'


def test_load_silently_skips_missing_sibling_file(tmp_path: Path) -> None:
    """Old runs predate this feature: transcript_path may be unset, or the file may have
    been hand-deleted. Either way the loader logs at debug and moves on."""
    judge = _make_judge_result(transcript=None)
    judge.transcript_path = "judge-0.yaml"  # set but file doesn't exist
    result = _make_evaluation_result(criteria=[judge])

    n = load_judge_transcripts(result, tmp_path)

    assert n == 0
    assert getattr(judge, "transcript", None) is None


def test_load_skips_when_no_transcript_path(tmp_path: Path) -> None:
    """A JudgeCriterionResult with neither transcript nor transcript_path
    (e.g. capture_transcript=False) is skipped."""
    judge = _make_judge_result(transcript=None)  # transcript_path also None
    result = _make_evaluation_result(criteria=[judge])

    n = load_judge_transcripts(result, tmp_path)

    assert n == 0


def test_load_skips_non_judge_results(tmp_path: Path) -> None:
    plain = CriterionResult(
        criterion_type="file_exists",
        description="x",
        score=1.0,
        details="ok",
    )
    result = _make_evaluation_result(criteria=[plain])

    n = load_judge_transcripts(result, tmp_path)

    assert n == 0


def test_load_rejects_absolute_path_traversal(tmp_path: Path) -> None:
    """SECURITY: a tampered task.json with an absolute transcript_path must NOT
    cause arbitrary-file reads. spill_judge_transcripts only ever writes a basename
    sibling, so any non-basename value is suspicious and must be refused."""
    judge = _make_judge_result(transcript=None)
    judge.transcript_path = "/etc/passwd"  # tampered absolute path
    result = _make_evaluation_result(criteria=[judge])

    n = load_judge_transcripts(result, tmp_path)

    assert n == 0
    assert getattr(judge, "transcript", None) is None


def test_load_rejects_dotdot_traversal(tmp_path: Path) -> None:
    """SECURITY: ``..`` segments in transcript_path also rejected — task.json that
    travels across trust boundaries (CI artifacts, shared bundles) shouldn't be
    able to walk out of the run directory."""
    judge = _make_judge_result(transcript=None)
    judge.transcript_path = "../../../../etc/passwd"
    result = _make_evaluation_result(criteria=[judge])

    n = load_judge_transcripts(result, tmp_path)

    assert n == 0
    assert getattr(judge, "transcript", None) is None


def test_load_rejects_subdir_path(tmp_path: Path) -> None:
    """A path with a separator (even within task_dir) is rejected — the spill helper
    only ever writes ``judge-<idx>.yaml`` as a basename, so anything with a slash is
    by definition not from us."""
    judge = _make_judge_result(transcript=None)
    judge.transcript_path = "subdir/judge-0.yaml"
    result = _make_evaluation_result(criteria=[judge])

    n = load_judge_transcripts(result, tmp_path)

    assert n == 0


@pytest.mark.parametrize(
    "name",
    [
        "CON",
        "con",
        "CON.yaml",
        "con.yaml",
        "NUL",
        "NUL.txt",
        "AUX.yaml",
        "PRN",
        "COM1",
        "COM5.yaml",
        "COM9",
        "LPT1",
        "LPT9.json",
    ],
)
def test_load_rejects_windows_reserved_basename(tmp_path: Path, name: str) -> None:
    """SECURITY: Windows device basenames (CON, NUL, COM1-9, LPT1-9) are rejected.

    On Windows the Win32 API opens these as character devices regardless of any
    extension or directory placement: ``open("CON.yaml")`` opens the console.
    The guard is platform-independent so a task.json minted on Linux carrying a
    tampered ``transcript_path: "CON"`` is rejected before it ever travels.

    Even though we plant a real file at that name on POSIX (where these are
    just regular filenames), the loader must refuse to read it.
    """
    import yaml

    transcript = _make_transcript()
    staged = tmp_path / name
    staged.write_text(yaml.safe_dump(transcript.model_dump()), encoding="utf-8")
    judge = _make_judge_result(transcript=None)
    judge.transcript_path = name
    result = _make_evaluation_result(criteria=[judge])

    n = load_judge_transcripts(result, tmp_path)

    assert n == 0
    assert judge.transcript is None


@pytest.mark.parametrize(
    "name",
    [
        # COM10 / LPT10+ are NOT reserved — only COM1..9 / LPT1..9 are device names.
        "COM10.yaml",
        "LPT10.yaml",
        # Reserved stem must match exactly; CON-INSIDE-A-NAME is fine.
        "judge-CON.yaml",
        "console.yaml",
        "auxiliary.yaml",
        # Multi-segment stems are not reserved.
        "judge.CON.yaml",
    ],
)
def test_load_accepts_non_reserved_basenames(tmp_path: Path, name: str) -> None:
    """Names that *look* like reserved devices but aren't must pass the guard."""
    import yaml

    transcript = _make_transcript()
    staged = tmp_path / name
    staged.write_text(yaml.safe_dump(transcript.model_dump()), encoding="utf-8")
    judge = _make_judge_result(transcript=None)
    judge.transcript_path = name
    result = _make_evaluation_result(criteria=[judge])

    n = load_judge_transcripts(result, tmp_path)

    assert n == 1


def test_load_rejects_backslash_separator_path(tmp_path: Path) -> None:
    """SECURITY: a path using backslashes as separators must be rejected too.

    ``PurePosixPath`` treats backslash as a regular character, so a tampered
    ``transcript_path: 'subdir\\judge-0.yaml'`` would pass a POSIX-only basename
    check on Linux runners. On Windows the same string then resolves to a
    nested file, violating the basename-only policy. Reject the shape itself.

    We stage a file at the literal POSIX path ``subdir\\judge-0.yaml`` (the
    backslash is just a regular char in the filename on POSIX) so the loader's
    ``is_file()`` check would succeed if the basename guard fails open.
    """
    import yaml

    transcript = _make_transcript()
    # On POSIX the backslash is a literal filename char (single file in tmp_path);
    # on Windows the backslash is a path separator (file inside a `subdir/` directory).
    # ``mkdir(parents=True, exist_ok=True)`` is a no-op on POSIX (parent is tmp_path,
    # already exists) and creates `subdir/` on Windows — so the write succeeds on both
    # and we can confirm the loader rejects the path regardless of platform.
    staged = tmp_path / "subdir\\judge-0.yaml"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(yaml.safe_dump(transcript.model_dump()), encoding="utf-8")
    judge = _make_judge_result(transcript=None)
    judge.transcript_path = "subdir\\judge-0.yaml"
    result = _make_evaluation_result(criteria=[judge])

    n = load_judge_transcripts(result, tmp_path)

    assert n == 0
    assert getattr(judge, "transcript", None) is None


def test_load_handles_corrupt_sibling_file(tmp_path: Path) -> None:
    """A malformed sibling file should be logged and skipped, not crash the loader."""
    judge = _make_judge_result(transcript=None)
    judge.transcript_path = "judge-0.yaml"
    # Unterminated YAML flow mapping — yaml.safe_load raises.
    (tmp_path / "judge-0.yaml").write_text("{not yaml", encoding="utf-8")
    result = _make_evaluation_result(criteria=[judge])

    n = load_judge_transcripts(result, tmp_path)

    assert n == 0
    assert getattr(judge, "transcript", None) is None


def test_load_skips_non_dict_sibling_payload(tmp_path: Path) -> None:
    """A sibling that parses as scalar / list / None must be refused — silently
    landing one of those on cr.transcript would crash downstream renderers
    (which assume mapping shape) with an AttributeError on first access."""
    judge = _make_judge_result(transcript=None)
    judge.transcript_path = "judge-0.yaml"
    # YAML scalar — yaml.safe_load returns "just a string", not a dict.
    (tmp_path / "judge-0.yaml").write_text("just a string", encoding="utf-8")
    result = _make_evaluation_result(criteria=[judge])

    n = load_judge_transcripts(result, tmp_path)

    assert n == 0
    assert getattr(judge, "transcript", None) is None


def test_load_attaches_dict_when_schema_mismatch(tmp_path: Path) -> None:
    """When the sibling parses as a dict but doesn't match JudgeTranscript's schema
    (forward-compat / older spilled formats / extra fields), the loader falls back
    to attaching the raw dict — better than dropping the whole transcript."""
    judge = _make_judge_result(transcript=None)
    judge.transcript_path = "judge-0.yaml"
    # Wrong types for required-shape fields — model_validate will raise. The dict
    # fallback path keeps the data on the result so the renderer can still show it.
    (tmp_path / "judge-0.yaml").write_text(
        "raw_verdict: 12345\ntool_calls: not-a-list\nfuture_field: from-newer-version\n",
        encoding="utf-8",
    )
    result = _make_evaluation_result(criteria=[judge])

    n = load_judge_transcripts(result, tmp_path)

    assert n == 1
    transcript = getattr(judge, "transcript", None)
    assert isinstance(transcript, dict)
    assert transcript["future_field"] == "from-newer-version"


def test_load_reads_legacy_json_sibling(tmp_path: Path) -> None:
    """Sibling files spilled before the YAML switch are ``judge-<idx>.json``.
    The loader must still read them so previously-archived runs render."""
    judge = _make_judge_result(transcript=None)
    judge.transcript_path = "judge-0.json"
    legacy = (
        '{"raw_verdict":"legacy","judge_prompt":"legacy","tool_calls":[],'
        '"duration_seconds":0.0,"judge_system_prompt":"","truncated":false,"token_usage":null}'
    )
    (tmp_path / "judge-0.json").write_text(legacy, encoding="utf-8")
    result = _make_evaluation_result(criteria=[judge])

    n = load_judge_transcripts(result, tmp_path)

    assert n == 1
    transcript = getattr(judge, "transcript", None)
    assert transcript is not None
    # Loader prefers typed JudgeTranscript even for legacy .json siblings —
    # the on-disk format changed but the schema is the same.
    from coder_eval.models import JudgeTranscript

    assert isinstance(transcript, JudgeTranscript)
    assert transcript.raw_verdict == "legacy"


# --- end-to-end ---


def test_spill_then_dump_then_load_round_trip(tmp_path: Path) -> None:
    """Full orchestrator-style round trip: spill → dump task.json (with exclude) →
    reload → load_judge_transcripts → render-ready result."""
    judge = _make_judge_result(transcript=_make_transcript())
    result = _make_evaluation_result(criteria=[judge])

    spill_judge_transcripts(result, tmp_path)
    task_json = tmp_path / "task.json"
    task_json.write_text(
        result.model_dump_json(
            indent=2,
            exclude={"success_criteria_results": {"__all__": {"transcript"}}},
        ),
        encoding="utf-8",
    )

    raw = task_json.read_text(encoding="utf-8")
    # task.json must NOT carry the verbose transcript fields.
    assert "raw_verdict" not in raw
    assert "judge_prompt" not in raw
    # But MUST carry transcript_path so re-render can find the sibling.
    assert "judge-0.yaml" in raw

    # Re-render path
    reloaded = EvaluationResult.model_validate_json(raw)
    load_judge_transcripts(reloaded, tmp_path)
    cr = reloaded.success_criteria_results[0]
    transcript = getattr(cr, "transcript", None)
    assert transcript is not None
    from coder_eval.models import JudgeTranscript

    assert isinstance(transcript, JudgeTranscript)
    assert transcript.raw_verdict == '{"score":0.75,"rationale":"ok"}'


def test_old_inline_transcript_format_still_loads(tmp_path: Path) -> None:
    """A task.json from before this feature has inline transcript, no transcript_path.
    Loading it should leave the inline transcript intact (no sibling file lookup)."""
    judge = _make_judge_result(transcript=_make_transcript())
    result = _make_evaluation_result(criteria=[judge])

    # Dump WITHOUT exclude — simulates pre-feature behavior.
    raw = result.model_dump_json(indent=2)
    assert "raw_verdict" in raw  # inline
    assert "transcript_path" in raw  # field exists but is null
    reloaded = EvaluationResult.model_validate_json(raw)
    cr = reloaded.success_criteria_results[0]
    # Inline transcript came back as a dict via extra='allow'.
    assert getattr(cr, "transcript", None) is not None
    assert getattr(cr, "transcript_path", None) is None

    # Loader is a no-op — inline transcript stays.
    n = load_judge_transcripts(reloaded, tmp_path)
    assert n == 0
    transcript = getattr(cr, "transcript", None)
    assert transcript is not None
    # Could be dict or typed (extra='allow' may keep typed shape on subclass round-trip).
    if hasattr(transcript, "raw_verdict"):
        assert transcript.raw_verdict == '{"score":0.75,"rationale":"ok"}'
    else:
        assert transcript["raw_verdict"] == '{"score":0.75,"rationale":"ok"}'
