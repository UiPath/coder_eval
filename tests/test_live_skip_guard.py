"""Unit coverage for the live enforcement tests' refusal guard.

``_attempted_or_skip`` decides whether a refusal reds the live job or reports
an honest skip, so it is worth pinning without the API call the module it
lives in needs. The module-level ``live`` marker there does not apply here.
"""

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.test_claude_settings_enforcement_live import _attempted_or_skip


@dataclass
class _Call:
    tool_name: str = "Read"
    parameters: dict = field(default_factory=dict)


@dataclass
class _Turn:
    agent_output: str | None = ""


def test_returns_only_the_reads_that_touched_the_target():
    target = Path("/tmp/run/archive")
    hit = _Call(parameters={"file_path": "/tmp/run/archive/notes.txt"})
    miss = _Call(parameters={"file_path": "/tmp/run/workspace/inside.txt"})

    assert _attempted_or_skip([hit, miss], target, _Turn()) == [hit]


def test_skips_rather_than_fails_when_the_agent_never_tried():
    """A refusal must not red the job — the enforcement was never exercised."""
    target = Path("/tmp/run/archive")
    elsewhere = _Call(parameters={"file_path": "/tmp/run/workspace/inside.txt"})

    for calls in ([], [elsewhere]):
        with pytest.raises(pytest.skip.Exception) as excinfo:
            _attempted_or_skip(calls, target, _Turn(agent_output="I can't read that."))
        assert "declined" in str(excinfo.value)
        # The reason has to carry enough to tell a refusal from a harness bug.
        assert str(target) in str(excinfo.value)
        assert "I can't read that." in str(excinfo.value)


def test_tolerates_a_turn_with_no_output():
    target = Path("/tmp/run/archive")
    with pytest.raises(pytest.skip.Exception):
        _attempted_or_skip([], target, _Turn(agent_output=None))
