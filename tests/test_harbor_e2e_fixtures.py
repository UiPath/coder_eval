"""Offline validation of the Harbor E2E fixtures -- the only guard between a
malformed fixture and a ~20-minute Docker+model CI round trip.

`harbor_e2e.py` (invoked only by `.github/workflows/harbor-e2e.yml`, a required
PR check) reads these fixtures with no offline check anywhere else in the
suite: a bad key, a dropped required field, or an uncompilable
`command_pattern`/`exclude_pattern` regex would otherwise surface only as an
opaque `reward != 1.0` (`criteria/command_executed.py` degrades a bad regex to
`score=0.0` rather than raising).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from coder_eval.models import TaskDefinition


FIXTURES_DIR = Path(__file__).resolve().parent / "harbor_e2e" / "fixtures"
FIXTURE_PATHS = sorted(FIXTURES_DIR.glob("*.yaml"))


@pytest.mark.parametrize("path", FIXTURE_PATHS, ids=lambda p: p.name)
def test_fixture_validates_as_a_task_definition(path: Path) -> None:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    task = TaskDefinition.model_validate(raw)
    assert task.success_criteria


@pytest.mark.parametrize("path", FIXTURE_PATHS, ids=lambda p: p.name)
def test_fixture_command_patterns_compile(path: Path) -> None:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    task = TaskDefinition.model_validate(raw)
    for criterion in task.success_criteria:
        for field in ("command_pattern", "exclude_pattern"):
            pattern = getattr(criterion, field, None)
            if pattern is not None:
                re.compile(pattern)


def test_fixtures_dir_is_not_empty() -> None:
    """A guard with nothing to parametrize over silently passes -- catch a
    fixture directory that stopped resolving instead of a suite that quietly
    stopped checking it."""
    assert FIXTURE_PATHS
