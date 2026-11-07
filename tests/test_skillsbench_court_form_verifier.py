"""Regression tests for the vendored court-form-filling verifier."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError


_TASK_DIR = Path(__file__).resolve().parents[1] / "tasks" / "samples" / "skillsbench" / "court-form-filling"
_BLANK_PDF = _TASK_DIR / "environment" / "sc100-blank.pdf"
_SPEC = importlib.util.spec_from_file_location("court_form_verifier", _TASK_DIR / "verifier" / "test_outputs.py")
assert _SPEC is not None and _SPEC.loader is not None
_VERIFIER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_VERIFIER)


@pytest.fixture(autouse=True)
def reject_verifier_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """A direct verifier call must fail if it tries to skip a check."""
    monkeypatch.setattr(pytest, "skip", pytest.fail)


def test_verifier_skip_fails() -> None:
    with pytest.raises(pytest.fail.Exception, match="missing"):
        pytest.skip("missing")


@pytest.mark.parametrize("case", _VERIFIER.REQUIRED_CHECKBOXES, ids=lambda case: case[1])
def test_required_checkbox_missing_fails(case: tuple) -> None:
    with pytest.raises(pytest.fail.Exception, match="not found"):
        _VERIFIER.TestCheckboxes().test_checkbox_value({}, *case)


@pytest.mark.parametrize("case", _VERIFIER.REQUIRED_CHECKBOXES, ids=lambda case: case[1])
def test_required_checkbox_wrong_value_fails(case: tuple) -> None:
    with pytest.raises(AssertionError, match="expected value"):
        _VERIFIER.TestCheckboxes().test_checkbox_value({case[0]: "wrong"}, *case)


@pytest.mark.parametrize("case", _VERIFIER.REQUIRED_CHECKBOXES, ids=lambda case: case[1])
def test_required_checkbox_correct_value_passes(case: tuple) -> None:
    _VERIFIER.TestCheckboxes().test_checkbox_value({case[0]: case[2]}, *case)


@pytest.mark.parametrize("case", _VERIFIER.UNCHECKED_CHECKBOXES, ids=lambda case: case[1])
def test_unchecked_checkbox_absent_passes(case: tuple) -> None:
    _VERIFIER.TestUncheckedCheckboxes().test_checkbox_unchecked({}, *case)


@pytest.mark.parametrize("case", _VERIFIER.UNCHECKED_CHECKBOXES, ids=lambda case: case[1])
def test_unchecked_checkbox_checked_fails(case: tuple) -> None:
    with pytest.raises(AssertionError, match="should be unchecked"):
        _VERIFIER.TestUncheckedCheckboxes().test_checkbox_unchecked({case[0]: "/1"}, *case)


@pytest.mark.parametrize("case", _VERIFIER.EMPTY_FIELDS, ids=lambda case: case[1])
def test_empty_field_missing_fails(case: tuple) -> None:
    values = {name: "" for name, _ in _VERIFIER.EMPTY_FIELDS if name != case[0]}
    with pytest.raises(AssertionError, match="not found"):
        _VERIFIER.TestEmptyFields().test_field_empty(values, *case)


@pytest.mark.parametrize("case", _VERIFIER.EMPTY_FIELDS, ids=lambda case: case[1])
def test_empty_field_filled_fails(case: tuple) -> None:
    with pytest.raises(AssertionError, match="should be empty"):
        _VERIFIER.TestEmptyFields().test_field_empty({case[0]: "filled"}, *case)


@pytest.mark.parametrize("case", _VERIFIER.EMPTY_FIELDS, ids=lambda case: case[1])
def test_empty_field_blank_passes(case: tuple) -> None:
    _VERIFIER.TestEmptyFields().test_field_empty({case[0]: ""}, *case)


def test_expected_fields_exist_in_shipped_pdf() -> None:
    fields = PdfReader(_BLANK_PDF).get_fields()
    assert fields is not None
    for name, _ in _VERIFIER.EMPTY_FIELDS:
        assert name in fields, name
        assert fields[name]["/FT"] == "/Tx", name
    for pattern, *_ in [*_VERIFIER.REQUIRED_CHECKBOXES, *_VERIFIER.UNCHECKED_CHECKBOXES]:
        matches = [field for name, field in fields.items() if pattern in name]
        assert matches, pattern
        assert all(field["/FT"] == "/Btn" for field in matches), pattern


def test_correct_pdf_field_roundtrip_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reader = PdfReader(_BLANK_PDF)
    fields = reader.get_fields()
    assert fields is not None
    values = {
        name: expected
        for pattern, _, expected, _ in _VERIFIER.REQUIRED_CHECKBOXES
        for name, field in fields.items()
        if pattern in name and expected in field.get("/_States_", [])
    }
    output = tmp_path / "filled.pdf"
    writer = PdfWriter(clone_from=reader)
    writer.update_page_form_field_values(list(writer.pages), values, auto_regenerate=False)
    writer.write(output)
    monkeypatch.setattr(_VERIFIER, "OUTPUT_FILE", output)
    checkboxes = _VERIFIER.checkbox_data.__wrapped__()
    field_values = _VERIFIER.pdf_field_values.__wrapped__()
    for case in _VERIFIER.REQUIRED_CHECKBOXES:
        _VERIFIER.TestCheckboxes().test_checkbox_value(checkboxes, *case)
    for case in _VERIFIER.UNCHECKED_CHECKBOXES:
        _VERIFIER.TestUncheckedCheckboxes().test_checkbox_unchecked(checkboxes, *case)
    for case in _VERIFIER.EMPTY_FIELDS:
        _VERIFIER.TestEmptyFields().test_field_empty(field_values, *case)


def test_blank_pdf_required_checkboxes_fail() -> None:
    checkboxes = _VERIFIER.extract_checkbox_values(_BLANK_PDF)
    for case in _VERIFIER.REQUIRED_CHECKBOXES:
        with pytest.raises(pytest.fail.Exception, match="not found"):
            _VERIFIER.TestCheckboxes().test_checkbox_value(checkboxes, *case)


def test_pdf_without_form_fields_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "no-form.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(output)
    monkeypatch.setattr(_VERIFIER, "OUTPUT_FILE", output)
    values = _VERIFIER.pdf_field_values.__wrapped__()
    assert values == {}
    for case in _VERIFIER.EMPTY_FIELDS:
        with pytest.raises(AssertionError, match="not found"):
            _VERIFIER.TestEmptyFields().test_field_empty(values, *case)


def test_checkbox_extraction_preserves_pdf_error(tmp_path: Path) -> None:
    output = tmp_path / "invalid.pdf"
    output.write_bytes(b"not a PDF")
    with pytest.raises(PdfReadError):
        _VERIFIER.extract_checkbox_values(output)


def test_field_extraction_preserves_dereference_error(monkeypatch: pytest.MonkeyPatch) -> None:
    value = Mock()
    value.get_object.side_effect = RuntimeError("broken field reference")
    monkeypatch.setattr("pypdf.PdfReader", Mock(return_value=Mock(get_fields=lambda: {"field": {"/V": value}})))
    monkeypatch.setattr(_VERIFIER, "OUTPUT_FILE", _BLANK_PDF)
    with pytest.raises(RuntimeError, match="broken field reference"):
        _VERIFIER.pdf_field_values.__wrapped__()
