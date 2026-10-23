"""Regression tests for the vendored ``skillsbench/3d-scan-calc`` sample verifier."""

from __future__ import annotations

import importlib.util
import json
import struct
from pathlib import Path
from types import ModuleType
from typing import IO, Any

import pytest


_TASK_DIR = Path(__file__).resolve().parents[1] / "tasks" / "samples" / "skillsbench" / "3d-scan-calc"
_SHIPPED_STL_PATH = _TASK_DIR / "environment" / "scan_data.stl"
_SHIPPED_STL = _SHIPPED_STL_PATH.read_bytes()
# One all-zero triangle whose attribute (material ID) is 42: volume 0.
_ZERO_STL = b"\0" * 80 + struct.pack("<I", 1) + struct.pack("<12f", *[0.0] * 12) + struct.pack("<H", 42)
_CORRECT_MASS = 34648.04123490179
_CORRECT = {"main_part_mass": _CORRECT_MASS, "material_id": 42}
_MASS_ZERO = {"main_part_mass": 0, "material_id": 42}


def _load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("scan_calc_verifier", _TASK_DIR / "verifier" / "test_outputs.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _grade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stl: bytes, report: dict[str, float]) -> None:
    """Run ``test_values_correct`` with the verifier's ``/root/...`` paths redirected to ``tmp_path``.

    ``tmp_path`` stands in for the container's ``/root``: it holds the agent's working
    ``scan_data.stl`` (*stl*) and its ``mass_report.json`` (*report*).
    """
    module = _load_verifier()
    (tmp_path / "scan_data.stl").write_bytes(stl)
    (tmp_path / "mass_report.json").write_text(json.dumps(report))

    def _open(path: str, *args: Any, **kwargs: Any) -> IO[Any]:
        return open(str(path).replace("/root/", f"{tmp_path}/"), *args, **kwargs)

    monkeypatch.setattr(module, "open", _open, raising=False)
    module.TestOutputs().test_values_correct()


@pytest.mark.parametrize("mass", [_CORRECT_MASS, _CORRECT_MASS / 1000.0], ids=["cm", "mm"])
def test_correct_answer_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mass: float) -> None:
    _grade(tmp_path, monkeypatch, _SHIPPED_STL, {"main_part_mass": mass, "material_id": 42})


def test_correct_answer_passes_when_the_stl_header_differs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The 80-byte header is not used by the computation, so changing it must not matter.
    _grade(tmp_path, monkeypatch, b"x" * 80 + _SHIPPED_STL[80:], _CORRECT)


def test_overwritten_stl_does_not_change_the_expected_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AssertionError, match="Mass mismatch"):
        _grade(tmp_path, monkeypatch, _ZERO_STL, _MASS_ZERO)
    _grade(tmp_path, monkeypatch, _ZERO_STL, _CORRECT)


def test_expected_values_match_the_shipped_stl() -> None:
    module = _load_verifier()
    derived = module.TestOutputs()._get_ground_truth(str(_SHIPPED_STL_PATH))
    assert derived == (module.EXPECTED_MASS, module.EXPECTED_VOL, module.EXPECTED_ID)
