"""Regression tests for the vendored court-form-filling verifier."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError


_REPO = Path(__file__).resolve().parents[1]
_TASK_DIR = _REPO / "tasks" / "samples" / "skillsbench" / "court-form-filling"
_BLANK_PDF = _TASK_DIR / "environment" / "sc100-blank.pdf"
_SPEC = importlib.util.spec_from_file_location("court_form_verifier", _TASK_DIR / "verifier" / "test_outputs.py")
assert _SPEC is not None and _SPEC.loader is not None
_VERIFIER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_VERIFIER)

pytestmark = pytest.mark.filterwarnings("ignore:XFA datasets:UserWarning")

type RequiredCase = tuple[str, str, str, bool]
type NamedCase = tuple[str, str]


def _write_correct_fill(output: Path) -> Path:
    reader = PdfReader(_BLANK_PDF)
    fields = reader.get_fields()
    assert fields is not None
    values = {
        name: expected
        for pattern, _, expected, _ in _VERIFIER.REQUIRED_CHECKBOXES
        for name, field in fields.items()
        if pattern in name and expected in field.get("/_States_", [])
    }
    writer = PdfWriter(clone_from=reader)
    writer.update_page_form_field_values(list(writer.pages), values, auto_regenerate=False)
    writer.write(output)
    return output


@pytest.mark.parametrize("case", _VERIFIER.REQUIRED_CHECKBOXES, ids=lambda case: case[1])
def test_required_checkbox_missing_fails(case: RequiredCase) -> None:
    with pytest.raises(pytest.fail.Exception, match="not found"):
        _VERIFIER.TestCheckboxes().test_checkbox_value({}, *case)


@pytest.mark.parametrize("case", _VERIFIER.REQUIRED_CHECKBOXES, ids=lambda case: case[1])
def test_required_checkbox_wrong_value_fails(case: RequiredCase) -> None:
    with pytest.raises(AssertionError, match="expected value"):
        _VERIFIER.TestCheckboxes().test_checkbox_value({case[0]: "wrong"}, *case)


@pytest.mark.parametrize("case", _VERIFIER.REQUIRED_CHECKBOXES, ids=lambda case: case[1])
def test_required_checkbox_correct_value_passes(case: RequiredCase) -> None:
    _VERIFIER.TestCheckboxes().test_checkbox_value({case[0]: case[2]}, *case)


@pytest.mark.parametrize("case", _VERIFIER.UNCHECKED_CHECKBOXES, ids=lambda case: case[1])
def test_unchecked_checkbox_absent_passes(case: NamedCase) -> None:
    _VERIFIER.TestUncheckedCheckboxes().test_checkbox_unchecked({}, *case)


@pytest.mark.parametrize("case", _VERIFIER.UNCHECKED_CHECKBOXES, ids=lambda case: case[1])
def test_unchecked_checkbox_checked_fails(case: NamedCase) -> None:
    with pytest.raises(AssertionError, match="should be unchecked"):
        _VERIFIER.TestUncheckedCheckboxes().test_checkbox_unchecked({case[0]: "/1"}, *case)


@pytest.mark.parametrize("case", _VERIFIER.EMPTY_FIELDS, ids=lambda case: case[1])
def test_empty_field_missing_fails(case: NamedCase) -> None:
    values = {name: "" for name, _ in _VERIFIER.EMPTY_FIELDS if name != case[0]}
    with pytest.raises(AssertionError, match="not found"):
        _VERIFIER.TestEmptyFields().test_field_empty(values, *case)


@pytest.mark.parametrize("case", _VERIFIER.EMPTY_FIELDS, ids=lambda case: case[1])
def test_empty_field_filled_fails(case: NamedCase) -> None:
    with pytest.raises(AssertionError, match="should be empty"):
        _VERIFIER.TestEmptyFields().test_field_empty({case[0]: "filled"}, *case)


@pytest.mark.parametrize("case", _VERIFIER.EMPTY_FIELDS, ids=lambda case: case[1])
def test_empty_field_blank_passes(case: NamedCase) -> None:
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


def test_correct_pdf_field_roundtrip_passes(tmp_path: Path) -> None:
    output = _write_correct_fill(tmp_path / "filled.pdf")
    checkboxes = _VERIFIER.extract_checkbox_values(output)
    field_values = _VERIFIER.extract_field_values(output)
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


def test_pdf_without_form_fields_fails(tmp_path: Path) -> None:
    output = tmp_path / "no-form.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(output)
    values = _VERIFIER.extract_field_values(output)
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
    with pytest.raises(RuntimeError, match="broken field reference"):
        _VERIFIER.extract_field_values(_BLANK_PDF)


def test_shipped_xfa_datasets_do_not_parse_and_warn() -> None:
    """Pins the upstream contract: checkbox grading uses AcroForm values only.

    The namespace cleanup leaves ``xfa:`` attribute prefixes, so the shipped datasets never parse. Making them
    parse would grade XFA values too (a blank ``"0"`` matches unchecked patterns such as ``Checkbox11``), which
    is a separate grading change.
    """
    xfa_xml = _VERIFIER.extract_xfa_xml(_BLANK_PDF)
    assert xfa_xml
    with pytest.warns(UserWarning, match="XFA datasets did not parse.*unbound prefix"):
        assert _VERIFIER.parse_xfa_checkboxes(xfa_xml) == {}


def _run_verifier(tmp_path: Path, output_pdf: Path) -> tuple[int, ET.Element]:
    """Run the verifier as test.sh does, with OUTPUT_FILE pointed at ``output_pdf``."""
    source = (_TASK_DIR / "verifier" / "test_outputs.py").read_text(encoding="utf-8")
    original = 'OUTPUT_FILE = Path("/root/sc100-filled.pdf")'
    assert source.count(original) == 1
    verifier = tmp_path / "test_outputs.py"
    verifier.write_text(source.replace(original, f"OUTPUT_FILE = Path({str(output_pdf)!r})"), encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    junit = tmp_path / "junit.xml"
    command = [sys.executable, "-m", "pytest", str(verifier), "-k", "Checkboxes or EmptyFields"]
    proc = subprocess.run(
        [*command, "-p", "no:cacheprovider", "-c", str(tmp_path / "pytest.ini"), f"--junitxml={junit}"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    suite = ET.parse(junit).getroot().find("testsuite")
    assert suite is not None, proc.stdout
    return proc.returncode, suite


def test_verifier_run_fails_blank_form_without_skips(tmp_path: Path) -> None:
    returncode, suite = _run_verifier(tmp_path, _BLANK_PDF)
    assert returncode != 0
    assert int(suite.get("failures", "0")) > 0
    assert suite.get("skipped") == "0"


def test_verifier_run_passes_correct_fill(tmp_path: Path) -> None:
    output = _write_correct_fill(tmp_path / "filled.pdf")
    returncode, suite = _run_verifier(tmp_path, output)
    assert returncode == 0
    assert int(suite.get("tests", "0")) > 0
    assert suite.get("skipped") == "0"


def test_dockerfile_pins_match_lock() -> None:
    """The regression tests above only vouch for the PDF stack the verifier image installs."""
    dockerfile = (_TASK_DIR / "environment" / "Dockerfile").read_text(encoding="utf-8")
    locked = {
        package["name"]: package["version"]
        for package in tomllib.loads((_REPO / "uv.lock").read_text(encoding="utf-8"))["package"]
    }
    for name in ("pypdf", "cryptography"):
        assert re.findall(rf"\b{name}==(\S+)", dockerfile) == [locked[name]], name
