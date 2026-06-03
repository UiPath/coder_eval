"""One-shot post-migration check: every task and experiment YAML still loads.

Asserts no `extra fields` errors after the 2026-05-12 unify-run-limits
migration. Delete this file after the migration has been on main for a
release cycle.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent

# Reference-flow `metadata.yaml` files are not task definitions — they're
# auxiliary catalog entries that share the `tasks/` tree. Skip them.
SKIP_NAMES = {"metadata.yaml"}


def _task_yamls() -> list[Path]:
    return [p for p in (ROOT / "tasks").rglob("*.yaml") if p.name not in SKIP_NAMES]


def _experiment_yamls() -> list[Path]:
    return list((ROOT / "experiments").glob("*.yaml"))


@pytest.mark.parametrize("path", _task_yamls(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_task_yaml_has_no_stale_top_level_keys(path: Path) -> None:
    """No top-level max_turns / task_timeout / turn_timeout on any task YAML."""
    # `encoding="utf-8"` matches CE008/CE011 (which guard the same in src/);
    # task YAMLs frequently contain UTF-8 (← arrows, ≤, em-dashes, smart quotes)
    # and the Windows-default cp1252 raises UnicodeDecodeError on those bytes.
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return
    for key in ("max_turns", "task_timeout", "turn_timeout"):
        assert key not in data, f"{path}: stale top-level {key!r} (should be under run_limits:)"


@pytest.mark.parametrize("path", _experiment_yamls(), ids=lambda p: p.name)
def test_experiment_yaml_has_no_stale_top_level_keys(path: Path) -> None:
    """No top-level max_turns / task_timeout / turn_timeout on experiment defaults / variants."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return
    defaults = data.get("defaults") or {}
    variants = data.get("variants") or []
    for blob, label in [(defaults, "defaults"), *((v, f"variant {v.get('variant_id')!r}") for v in variants)]:
        if not isinstance(blob, dict):
            continue
        for key in ("max_turns", "task_timeout", "turn_timeout"):
            assert key not in blob, f"{path} {label}: stale top-level {key!r} (should be under run_limits:)"


def test_migrated_smoke_tasks_load() -> None:
    """Spot-check that the hand-edited smoke YAMLs load and surface the new shape.

    Broad ``every task YAML loads`` coverage is unrelated to the 2026-05-12
    migration — those failures are pre-existing and tracked separately.
    """
    from coder_eval.orchestration.task_loader import load_task

    samples = [
        ("tasks/smoke_task_timeout.yaml", {"max_turns": 2, "task_timeout": 30}),
        ("tasks/smoke_cost_budget_exceeded.yaml", {"max_turns": 2, "max_usd": 0.0001}),
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for rel, expected in samples:
            task, _ = load_task(ROOT / rel)
            assert task.run_limits is not None, f"{rel}: run_limits should be populated"
            for key, value in expected.items():
                assert getattr(task.run_limits, key) == value, f"{rel}.run_limits.{key}"
