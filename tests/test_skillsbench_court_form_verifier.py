"""Regression tests for the vendored court-form-filling verifier."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


_VERIFIER = (
    Path(__file__).resolve().parents[1]
    / "tasks"
    / "samples"
    / "skillsbench"
    / "court-form-filling"
    / "verifier"
    / "test_outputs.py"
)


@pytest.mark.parametrize(
    ("check_class", "fixture_name", "values", "exit_code", "summary"),
    [
        ("TestCheckboxes", "checkbox_data", "{}", 1, "6 failed"),
        ("TestUncheckedCheckboxes", "checkbox_data", "{}", 0, "6 passed"),
        ("TestEmptyFields", "pdf_field_values", "{}", 1, "20 failed"),
        (
            "TestEmptyFields",
            "pdf_field_values",
            '{name: "" for name, _ in EMPTY_FIELDS[1:]}',
            1,
            "1 failed, 19 passed",
        ),
        (
            "TestEmptyFields",
            "pdf_field_values",
            '{name: "filled" for name, _ in EMPTY_FIELDS}',
            1,
            "20 failed",
        ),
        (
            "TestEmptyFields",
            "pdf_field_values",
            '{name: "" for name, _ in EMPTY_FIELDS}',
            0,
            "20 passed",
        ),
        (
            "TestCheckboxes",
            "checkbox_data",
            "{pattern: expected for pattern, _, expected, _ in REQUIRED_CHECKBOXES}",
            0,
            "6 passed",
        ),
        (
            "TestCheckboxes",
            "checkbox_data",
            '{pattern: "wrong" for pattern, _, _, _ in REQUIRED_CHECKBOXES}',
            1,
            "6 failed",
        ),
        (
            "TestUncheckedCheckboxes",
            "checkbox_data",
            '{pattern: "/1" for pattern, _ in UNCHECKED_CHECKBOXES}',
            1,
            "6 failed",
        ),
    ],
    ids=[
        "missing-required-boxes",
        "unchecked-boxes",
        "missing-fields",
        "one-missing-field",
        "nonblank-fields",
        "blank-fields",
        "correct-boxes",
        "wrong-boxes",
        "unexpected-checked-boxes",
    ],
)
def test_verifier_outcome(
    tmp_path: Path, check_class: str, fixture_name: str, values: str, exit_code: int, summary: str
) -> None:
    verifier = tmp_path / "test_outputs.py"
    verifier.write_text(
        _VERIFIER.read_text() + f"\n\n@pytest.fixture\ndef {fixture_name}():\n    return {values}\n",
        encoding="utf-8",
    )
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(config),
            f"{verifier}::{check_class}",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert summary in result.stdout, result.stdout
    assert "skipped" not in result.stdout, result.stdout
