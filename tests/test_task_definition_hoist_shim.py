"""Tests for the legacy `agent.max_turns` / `agent.turn_timeout` hoist shim.

The shim lifts these two fields from the deprecated under-`agent:` location to
the new top-level location on `TaskDefinition`. Scheduled removal: 2026-05-15.
See c/2026-05-07-move-agent-timing-to-task.md.
"""

from __future__ import annotations

import warnings

import pytest

from coder_eval.models import (
    AgentKind,
    FileExistsCriterion,
    SandboxConfig,
    TaskDefinition,
)


def _base_task_kwargs() -> dict:
    return {
        "task_id": "shim_test",
        "description": "shim test",
        "initial_prompt": "do something",
        "sandbox": SandboxConfig(driver="tempdir"),
        "success_criteria": [FileExistsCriterion(type="file_exists", path="x.py", description="x.py exists")],
    }


def test_hoist_max_turns_lifts_to_top_level_with_warning() -> None:
    kwargs = _base_task_kwargs()
    kwargs["agent"] = {"type": "claude-code", "max_turns": 50}
    with pytest.warns(DeprecationWarning, match=r"max_turns.*deprecated.*2026-05-15"):
        task = TaskDefinition(**kwargs)
    assert task.max_turns == 50
    assert task.agent is not None
    # max_turns is no longer a field on AgentConfig — it lives on TaskDefinition.
    assert not hasattr(task.agent, "max_turns")


def test_hoist_turn_timeout_lifts_to_top_level_with_warning() -> None:
    kwargs = _base_task_kwargs()
    kwargs["agent"] = {"type": "claude-code", "turn_timeout": 60}
    with pytest.warns(DeprecationWarning, match=r"turn_timeout.*deprecated.*2026-05-15"):
        task = TaskDefinition(**kwargs)
    assert task.turn_timeout == 60
    assert task.agent is not None
    # turn_timeout is no longer a field on AgentConfig — it lives on TaskDefinition.
    assert not hasattr(task.agent, "turn_timeout")


def test_hoist_both_fields_emits_warning_per_field() -> None:
    kwargs = _base_task_kwargs()
    kwargs["agent"] = {"type": "claude-code", "max_turns": 30, "turn_timeout": 120}
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        task = TaskDefinition(**kwargs)
    deprecation_msgs = [str(w.message) for w in captured if issubclass(w.category, DeprecationWarning)]
    # Exactly one warning per field, per file.
    assert sum("'max_turns'" in m for m in deprecation_msgs) == 1
    assert sum("'turn_timeout'" in m for m in deprecation_msgs) == 1
    assert task.max_turns == 30
    assert task.turn_timeout == 120


def test_conflict_top_level_and_under_agent_raises() -> None:
    kwargs = _base_task_kwargs()
    kwargs["max_turns"] = 10
    kwargs["agent"] = {"type": "claude-code", "max_turns": 50}
    with pytest.raises(ValueError, match=r"max_turns.*both at top level and under agent"):
        TaskDefinition(**kwargs)


def test_conflict_turn_timeout_top_level_and_under_agent_raises() -> None:
    kwargs = _base_task_kwargs()
    kwargs["turn_timeout"] = 30
    kwargs["agent"] = {"type": "claude-code", "turn_timeout": 60}
    with pytest.raises(ValueError, match=r"turn_timeout.*both at top level and under agent"):
        TaskDefinition(**kwargs)


def test_no_agent_block_is_noop() -> None:
    """Task with no `agent:` block at all triggers no warning and resolves cleanly."""
    kwargs = _base_task_kwargs()
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        task = TaskDefinition(**kwargs)
    assert task.agent is None
    assert task.max_turns is None
    assert task.turn_timeout is None
    deprecation_msgs = [w for w in captured if issubclass(w.category, DeprecationWarning)]
    assert deprecation_msgs == []


def test_neither_field_under_agent_is_noop() -> None:
    """Agent block without the legacy fields triggers no warning."""
    kwargs = _base_task_kwargs()
    kwargs["agent"] = {"type": "claude-code", "model": "claude-sonnet-4-6"}
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        TaskDefinition(**kwargs)
    deprecation_msgs = [w for w in captured if issubclass(w.category, DeprecationWarning)]
    assert deprecation_msgs == []


def test_top_level_only_no_warning() -> None:
    """Setting only the new top-level fields (no `agent.*`) emits no DeprecationWarning."""
    kwargs = _base_task_kwargs()
    kwargs["max_turns"] = 25
    kwargs["turn_timeout"] = 90
    kwargs["agent"] = {"type": "claude-code"}
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        task = TaskDefinition(**kwargs)
    assert task.max_turns == 25
    assert task.turn_timeout == 90
    deprecation_msgs = [w for w in captured if issubclass(w.category, DeprecationWarning)]
    assert deprecation_msgs == []


def test_explicit_agent_kind_value_works() -> None:
    """Sanity check: AgentKind value 'claude-code' parses both as enum instance and string."""
    kwargs = _base_task_kwargs()
    kwargs["agent"] = {"type": AgentKind.CLAUDE_CODE.value, "max_turns": 5}
    with pytest.warns(DeprecationWarning):
        task = TaskDefinition(**kwargs)
    assert task.max_turns == 5
