"""Tests for JudgeVerdict model + parse_judge_verdict parser."""

from __future__ import annotations

import pytest

from coder_eval.evaluation.judge_verdict import (
    _iter_top_level_object_spans,
    parse_judge_verdict,
)
from coder_eval.models import JudgeVerdict


# --- JudgeVerdict model ---


def test_verdict_accepts_valid_json() -> None:
    v, err = parse_judge_verdict('{"score": 0.8, "rationale": "ok"}')
    assert err is None
    assert v is not None
    assert v.score == 0.8
    assert v.rationale == "ok"


def test_verdict_clamps_high() -> None:
    v, err = parse_judge_verdict('{"score": 1.7, "rationale": "too high"}')
    assert err is None
    assert v is not None
    assert v.score == 1.0


def test_verdict_clamps_low() -> None:
    v, err = parse_judge_verdict('{"score": -0.4, "rationale": "negative"}')
    assert err is None
    assert v is not None
    assert v.score == 0.0


def test_verdict_rejects_nan() -> None:
    v, err = parse_judge_verdict('{"score": NaN, "rationale": "oops"}')
    assert v is None
    assert err is not None
    assert "finite" in err


@pytest.mark.parametrize("literal", ["Infinity", "-Infinity"])
def test_verdict_rejects_infinity(literal: str) -> None:
    v, err = parse_judge_verdict(f'{{"score": {literal}, "rationale": "oops"}}')
    assert v is None
    assert err is not None
    assert "finite" in err


def test_verdict_rejects_boolean_score() -> None:
    v, err = parse_judge_verdict('{"score": true, "rationale": "oops"}')
    assert v is None
    assert err is not None
    assert "not a number" in err


def test_verdict_rejects_string_score() -> None:
    v, err = parse_judge_verdict('{"score": "great", "rationale": "oops"}')
    assert v is None
    assert err is not None
    assert "not a number" in err


def test_verdict_coerces_null_rationale_to_empty() -> None:
    """null rationale is coerced to "" — lenient form of the legacy `str(None)` bug fix."""
    v, err = parse_judge_verdict('{"score": 0.5, "rationale": null}')
    assert err is None
    assert v is not None
    assert v.rationale == ""


def test_verdict_rejects_dict_rationale() -> None:
    v, err = parse_judge_verdict('{"score": 0.5, "rationale": {"x": 1}}')
    assert v is None
    assert err is not None
    assert "rationale" in err
    assert "dict" in err


def test_verdict_rejects_list_rationale() -> None:
    v, err = parse_judge_verdict('{"score": 0.5, "rationale": ["a", "b"]}')
    assert v is None
    assert err is not None
    assert "rationale" in err
    assert "list" in err


def test_verdict_missing_score_fails() -> None:
    v, err = parse_judge_verdict('{"rationale": "forgot score"}')
    assert v is None
    assert err is not None
    assert "missing" in err


def test_verdict_default_rationale_empty() -> None:
    v, err = parse_judge_verdict('{"score": 0.5}')
    assert err is None
    assert v is not None
    assert v.rationale == ""


def test_verdict_strips_rationale_whitespace() -> None:
    v, err = parse_judge_verdict('{"score": 0.5, "rationale": "  ok  "}')
    assert err is None
    assert v is not None
    assert v.rationale == "ok"


def test_verdict_collapses_multi_line_rationale_to_single_line() -> None:
    """Regression for bug_005: a multi-line rationale would survive `.strip()`
    and break two consumers — format_details writes 'rationale: <text>' on one
    line, and reports_html._extract_rationale parses by line. Coerce to single
    line at validation time so the contract is enforced everywhere."""
    v, err = parse_judge_verdict(
        '{"score": 0.5, "rationale": "Looks correct overall.\\nBut missing tests for edge cases."}'
    )
    assert err is None
    assert v is not None
    assert "\n" not in v.rationale
    assert v.rationale == "Looks correct overall. But missing tests for edge cases."


def test_verdict_collapses_runs_of_whitespace() -> None:
    """Tabs and multi-space runs are also normalized — anything word-split sees
    becomes a single space."""
    v, err = parse_judge_verdict('{"score": 0.5, "rationale": "a\\t\\tb   c\\nd"}')
    assert err is None
    assert v is not None
    assert v.rationale == "a b c d"


def test_rationale_rejects_whitespace_only() -> None:
    """Whitespace-only input collapses to ``""`` and surfaces a parse error.

    The pre-fix code silently emitted a blank ``rationale: `` line in
    ``format_details`` — invisible to authors. Now the validator raises so
    the judge result records an explicit error string.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        JudgeVerdict(score=0.5, rationale="   \n  \t")
    assert "empty after whitespace collapse" in str(excinfo.value)


def test_rationale_rejects_empty_string() -> None:
    """An explicit ``rationale: ""`` is rejected for the same reason."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        JudgeVerdict(score=0.5, rationale="")


def test_parse_judge_verdict_propagates_empty_rationale_error() -> None:
    """The parser surfaces the validator's ValueError to the caller."""
    v, err = parse_judge_verdict('{"score": 0.5, "rationale": "  "}')
    assert v is None
    assert err is not None
    assert "empty" in err.lower()


def test_verdict_model_rejects_bool_directly() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        JudgeVerdict(score=True, rationale="x")  # type: ignore[arg-type]


# --- parser top-level walking ---


def test_parser_picks_last_valid_span() -> None:
    content = '{"score": 0.2, "rationale": "first"} middle {"score": 0.7, "rationale": "last"}'
    v, err = parse_judge_verdict(content)
    assert err is None
    assert v is not None
    assert v.score == 0.7
    assert v.rationale == "last"


def test_parser_prefers_verdict_over_trailing_ack() -> None:
    content = '{"score": 0.8, "rationale": "real"} {"ack": true}'
    v, err = parse_judge_verdict(content)
    assert err is None
    assert v is not None
    assert v.score == 0.8


def test_parser_skips_invalid_json() -> None:
    content = '{not json} {"score": 0.9, "rationale": "ok"}'
    v, err = parse_judge_verdict(content)
    assert err is None
    assert v is not None
    assert v.score == 0.9


def test_parser_handles_markdown_preamble() -> None:
    content = '## Analysis\n...\n{"score": 0.9, "rationale": "ok"}'
    v, err = parse_judge_verdict(content)
    assert err is None
    assert v is not None
    assert v.score == 0.9


def test_parser_handles_braces_in_strings() -> None:
    content = '{"score": 0.8, "rationale": "uses {placeholders} correctly"}'
    v, err = parse_judge_verdict(content)
    assert err is None
    assert v is not None
    assert v.score == 0.8


def test_parser_handles_escaped_quotes() -> None:
    content = r'{"score": 0.8, "rationale": "he said \"hi\""}'
    v, err = parse_judge_verdict(content)
    assert err is None
    assert v is not None
    assert v.score == 0.8
    assert 'he said "hi"' in v.rationale


def test_parser_handles_escaped_backslash_then_quote() -> None:
    # Rationale: C:\\ and "hi" — backslash-escaped backslash, then escaped quote.
    content = r'{"score": 0.5, "rationale": "C:\\ and \"hi\""}'
    v, err = parse_judge_verdict(content)
    assert err is None
    assert v is not None


def test_parser_unterminated_string_returns_error() -> None:
    content = '{"score": 0.5, "rationale": "unterminated'
    v, err = parse_judge_verdict(content)
    assert v is None
    assert err is not None


@pytest.mark.parametrize("empty", ["", "   ", "\n\t"])
def test_parser_empty_input_returns_error(empty: str) -> None:
    v, err = parse_judge_verdict(empty)
    assert v is None
    assert err is not None
    assert "empty" in err


def test_parser_ignores_stray_close_brace() -> None:
    content = 'garbage } then {"score": 1.0, "rationale": "ok"}'
    v, err = parse_judge_verdict(content)
    assert err is None
    assert v is not None
    assert v.score == 1.0


def test_parser_handles_nested_objects() -> None:
    content = '{"score": 0.6, "rationale": "partial", "meta": {"k": 1}}'
    v, err = parse_judge_verdict(content)
    assert err is None
    assert v is not None
    assert v.score == 0.6


# --- verbose verdict (findings) ---


def test_verdict_accepts_findings() -> None:
    content = '{"score": 0.7, "rationale": "ok", "findings": ["main.py:12 missing return", "no tests added"]}'
    v, err = parse_judge_verdict(content)
    assert err is None
    assert v is not None
    assert v.score == 0.7
    assert v.findings == ["main.py:12 missing return", "no tests added"]


def test_verdict_accepts_legacy_two_field_form() -> None:
    """Verdicts emitted before findings was added must still parse — findings defaults empty."""
    v, err = parse_judge_verdict('{"score": 0.5, "rationale": "ok"}')
    assert err is None
    assert v is not None
    assert v.findings == []


def test_verdict_drops_non_list_findings() -> None:
    """A misbehaving model that emits findings as a string shouldn't tank the verdict —
    coerce to empty list and let the score/rationale stand."""
    v, err = parse_judge_verdict('{"score": 0.5, "rationale": "ok", "findings": "string instead of list"}')
    assert err is None
    assert v is not None
    assert v.findings == []


def test_verdict_drops_blank_findings() -> None:
    v, err = parse_judge_verdict('{"score": 0.5, "rationale": "ok", "findings": ["valid", "", "   ", null]}')
    assert err is None
    assert v is not None
    assert v.findings == ["valid"]


def test_verdict_strips_finding_whitespace() -> None:
    v, err = parse_judge_verdict('{"score": 0.5, "rationale": "ok", "findings": ["  hello  "]}')
    assert err is None
    assert v is not None
    assert v.findings == ["hello"]


def test_verdict_ignores_unknown_keys_via_extra_ignore() -> None:
    """Old verdicts that included an analysis field still parse cleanly — analysis is
    no longer in the schema and gets silently dropped via model_config={'extra': 'ignore'}."""
    v, err = parse_judge_verdict('{"score": 0.5, "rationale": "ok", "analysis": "old prose"}')
    assert err is None
    assert v is not None
    assert v.score == 0.5
    assert not hasattr(v, "analysis")


def test_verdict_model_rejects_legacy_invalid_score_unchanged() -> None:
    """Findings must NOT loosen score validation."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        JudgeVerdict.model_validate({"score": "great", "findings": ["x"]})


# --- _iter_top_level_object_spans internals ---


def test_iter_enumerates_top_level_spans() -> None:
    text = 'prefix {"a": 1} middle {"b": 2} suffix'
    assert _iter_top_level_object_spans(text) == ['{"a": 1}', '{"b": 2}']


def test_iter_handles_escaped_quote_at_close() -> None:
    text = r'{"k": "ends with \""}'
    assert _iter_top_level_object_spans(text) == [text]


def test_iter_ignores_stray_close_brace() -> None:
    text = 'garbage } then {"score": 1.0}'
    assert _iter_top_level_object_spans(text) == ['{"score": 1.0}']
