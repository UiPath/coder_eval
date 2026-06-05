"""Tests for the slack_summary CI script — owner tagging + traffic-light dots."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "ci" / "slack_summary.py"


def _import_slack_summary():
    spec = importlib.util.spec_from_file_location("slack_summary", _SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["slack_summary"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def slack():
    return _import_slack_summary()


def _tasks(skill: str, passed: int, total: int) -> list[dict]:
    """Build `total` task_results for `skill`, the first `passed` SUCCESS."""
    return [
        {"task_path": f"tasks/{skill}/t{i}.yaml", "status": "SUCCESS" if i < passed else "FAILED"} for i in range(total)
    ]


def _write_owners(path: Path, rows: dict[str, str]) -> Path:
    lines = ["SkillName,Owner", *(f"{skill},{owner}" for skill, owner in rows.items())]
    path.write_text("\n".join(lines) + "\n")
    return path


# --- _pct_dot: the green/yellow/red cutoffs the owner-tag rule keys off of ---


def test_pct_dot_thresholds(slack):
    assert slack._pct_dot(49.9) == ":red_circle:"
    assert slack._pct_dot(50.0) == ":large_yellow_circle:"
    assert slack._pct_dot(84.9) == ":large_yellow_circle:"
    # GREEN_PCT is inclusive — exactly the cutoff reads green.
    assert slack._pct_dot(85.0) == ":large_green_circle:"
    assert slack._pct_dot(100.0) == ":large_green_circle:"


# --- load_skill_owners ---


def test_load_skill_owners_reads_shipped_csv(slack):
    """The real CSV that ships next to the script stays parseable and complete.

    Asserts structure, not specific names, so an owner reassignment doesn't
    break the test — only a malformed/empty CSV or a dropped column does.
    """
    owners = slack.load_skill_owners()
    assert owners, "shipped skill-test-owners.csv produced no owners"
    assert "uipath-rpa" in owners
    assert all(skill and name for skill, name in owners.items())


def test_load_skill_owners_missing_file_degrades(slack, monkeypatch, tmp_path):
    monkeypatch.setattr(slack, "OWNERS_CSV", tmp_path / "nope.csv")
    assert slack.load_skill_owners() == {}


def test_load_skill_owners_first_column_only(slack, monkeypatch, tmp_path):
    csv_path = _write_owners(tmp_path / "owners.csv", {"uipath-rpa": "Mihai Chirculescu"})
    monkeypatch.setattr(slack, "OWNERS_CSV", csv_path)
    assert slack.load_skill_owners() == {"uipath-rpa": "Mihai Chirculescu"}


# --- skill_breakdown: owner tags only on sub-85% rows ---


@pytest.fixture
def owners_csv(slack, monkeypatch, tmp_path):
    csv_path = _write_owners(
        tmp_path / "owners.csv",
        {"skill-red": "Red Owner", "skill-yellow": "Yellow Owner", "skill-green": "Green Owner"},
    )
    monkeypatch.setattr(slack, "OWNERS_CSV", csv_path)
    return csv_path


def test_breakdown_tags_red_and_yellow_not_green(slack, owners_csv):
    run = {
        "task_results": (
            _tasks("skill-red", 2, 10)  # 20% red
            + _tasks("skill-yellow", 7, 10)  # 70% yellow
            + _tasks("skill-green", 10, 10)  # 100% green
        )
    }
    out = slack.skill_breakdown(run)
    assert ":red_circle: skill-red: 2/10 (20%) — Red Owner" in out
    assert ":large_yellow_circle: skill-yellow: 7/10 (70%) — Yellow Owner" in out
    # Green skill: no owner tag even though it has one in the CSV.
    assert ":large_green_circle: skill-green: 10/10 (100%)" in out
    assert "Green Owner" not in out


def test_breakdown_boundary_85_is_green_untagged(slack, owners_csv):
    # 85/100 == 85% is green (no tag); 84/100 is yellow (tagged).
    run = {"task_results": _tasks("skill-green", 85, 100) + _tasks("skill-yellow", 84, 100)}
    out = slack.skill_breakdown(run)
    assert "skill-green: 85/100 (85%)" in out
    assert "skill-green: 85/100 (85%) —" not in out
    assert "skill-yellow: 84/100 (84%) — Yellow Owner" in out


def test_breakdown_skill_absent_from_csv_untagged(slack, owners_csv):
    run = {"task_results": _tasks("skill-unknown", 1, 10)}  # red, but not in CSV
    out = slack.skill_breakdown(run)
    assert ":red_circle: skill-unknown: 1/10 (10%)" in out
    assert "—" not in out


def test_breakdown_missing_csv_renders_without_tags(slack, monkeypatch, tmp_path):
    monkeypatch.setattr(slack, "OWNERS_CSV", tmp_path / "nope.csv")
    run = {"task_results": _tasks("skill-red", 1, 10)}
    out = slack.skill_breakdown(run)
    assert ":red_circle: skill-red: 1/10 (10%)" in out
    assert "—" not in out


def test_breakdown_below_min_tasks_excluded(slack, owners_csv):
    # Fewer than min_tasks (default 4) → skill is dropped entirely.
    run = {"task_results": _tasks("skill-red", 0, 3)}
    assert slack.skill_breakdown(run) == ""
