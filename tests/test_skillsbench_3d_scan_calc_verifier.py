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


def _load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("scan_calc_verifier", _TASK_DIR / "verifier" / "test_outputs.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_VERIFIER = _load_verifier()
_CORRECT_MASS: float = _VERIFIER.EXPECTED_MASS
_CORRECT_ID: int = _VERIFIER.EXPECTED_ID
_CORRECT = {"main_part_mass": _CORRECT_MASS, "material_id": _CORRECT_ID}
_MASS_ZERO = {"main_part_mass": 0, "material_id": _CORRECT_ID}
# One all-zero triangle whose attribute (material ID) is the expected one: volume 0.
_ZERO_STL = b"\0" * 80 + struct.pack("<I", 1) + struct.pack("<12f", *[0.0] * 12) + struct.pack("<H", _CORRECT_ID)


def _grade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report: dict[str, float], stl: bytes | None = None) -> None:
    """Run ``test_values_correct`` with the verifier's ``/root/...`` paths redirected to ``tmp_path``.

    ``tmp_path`` stands in for the container's ``/root``: it holds the agent's
    ``mass_report.json`` (*report*) and, if *stl* is given, an agent-written ``scan_data.stl``.
    """
    module = _load_verifier()
    if stl is not None:
        (tmp_path / "scan_data.stl").write_bytes(stl)
    (tmp_path / "mass_report.json").write_text(json.dumps(report))

    def _open(path: str, *args: Any, **kwargs: Any) -> IO[Any]:
        return open(str(path).replace("/root/", f"{tmp_path}/"), *args, **kwargs)

    monkeypatch.setattr(module, "open", _open, raising=False)
    module.TestOutputs().test_values_correct()


@pytest.mark.parametrize("mass", [_CORRECT_MASS, _CORRECT_MASS / 1000.0], ids=["cm", "mm"])
def test_correct_answer_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mass: float) -> None:
    _grade(tmp_path, monkeypatch, {"main_part_mass": mass, "material_id": _CORRECT_ID})


@pytest.mark.parametrize(
    ("report", "message"),
    [
        ({"main_part_mass": _CORRECT_MASS, "material_id": _CORRECT_ID + 1}, "Expected Material ID"),
        ({"main_part_mass": _CORRECT_MASS * 1.002, "material_id": _CORRECT_ID}, "Mass mismatch"),
    ],
    ids=["wrong-material-id", "mass-just-outside-tolerance"],
)
def test_wrong_answer_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report: dict[str, float], message: str
) -> None:
    with pytest.raises(AssertionError, match=message):
        _grade(tmp_path, monkeypatch, report)


def test_overwritten_stl_does_not_change_the_expected_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AssertionError, match="Mass mismatch"):
        _grade(tmp_path, monkeypatch, _MASS_ZERO, stl=_ZERO_STL)
    _grade(tmp_path, monkeypatch, _CORRECT, stl=_ZERO_STL)


def test_expected_values_match_the_shipped_stl() -> None:
    module = _load_verifier()
    derived = module.TestOutputs()._get_ground_truth(str(_SHIPPED_STL_PATH))
    assert derived == (module.EXPECTED_MASS, module.EXPECTED_VOL, module.EXPECTED_ID)
