"""Tests for the legacy `agent.max_turns` / `agent.turn_timeout` hoist shim.

The shim lifts these two fields from the deprecated under-`agent:` location to
`run_limits.max_turns` / `run_limits.turn_timeout`. Scheduled removal: 2026-05-20.
See c/2026-05-12-unify-run-limits.md.
"""

from __future__ import annotations

import warnings

import pytest

from coder_eval.models import (
    AgentKind,
    FileExistsCriterion,
    RunLimits,
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


def test_hoist_max_turns_lifts_to_run_limits_with_warning() -> None:
    kwargs = _base_task_kwargs()
    kwargs["agent"] = {"type": "claude-code", "max_turns": 50}
    with pytest.warns(DeprecationWarning, match=r"max_turns.*deprecated.*2026-05-20"):
        task = TaskDefinition(**kwargs)
    assert task.run_limits is not None
    assert task.run_limits.max_turns == 50
    assert task.agent is not None
    # max_turns is not a field on AgentConfig — it lives on RunLimits.
    assert not hasattr(task.agent, "max_turns")


def test_hoist_turn_timeout_lifts_to_run_limits_with_warning() -> None:
    kwargs = _base_task_kwargs()
    kwargs["agent"] = {"type": "claude-code", "turn_timeout": 60}
    with pytest.warns(DeprecationWarning, match=r"turn_timeout.*deprecated.*2026-05-20"):
        task = TaskDefinition(**kwargs)
    assert task.run_limits is not None
    assert task.run_limits.turn_timeout == 60
    assert task.agent is not None
    assert not hasattr(task.agent, "turn_timeout")


def test_hoist_both_fields_emits_warning_per_field() -> None:
    kwargs = _base_task_kwargs()
    kwargs["agent"] = {"type": "claude-code", "max_turns": 30, "turn_timeout": 120}
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        task = TaskDefinition(**kwargs)
    deprecation_msgs = [str(w.message) for w in captured if issubclass(w.category, DeprecationWarning)]
    assert sum("'max_turns'" in m for m in deprecation_msgs) == 1
    assert sum("'turn_timeout'" in m for m in deprecation_msgs) == 1
    assert task.run_limits is not None
    assert task.run_limits.max_turns == 30
    assert task.run_limits.turn_timeout == 120


def test_hoist_merges_with_existing_run_limits() -> None:
    """Agent-level max_turns + an existing run_limits block: both are preserved."""
    kwargs = _base_task_kwargs()
    kwargs["agent"] = {"type": "claude-code", "max_turns": 10}
    kwargs["run_limits"] = {"max_usd": 0.5}
    with pytest.warns(DeprecationWarning):
        task = TaskDefinition(**kwargs)
    assert task.run_limits is not None
    assert task.run_limits.max_turns == 10
    assert task.run_limits.max_usd == 0.5


def test_hoist_merges_with_existing_run_limits_model_instance() -> None:
    """run_limits as a RunLimits instance is also handled."""
    kwargs = _base_task_kwargs()
    kwargs["agent"] = {"type": "claude-code", "max_turns": 10}
    kwargs["run_limits"] = RunLimits(max_usd=0.5)
    with pytest.warns(DeprecationWarning):
        task = TaskDefinition(**kwargs)
    assert task.run_limits is not None
    assert task.run_limits.max_turns == 10
    assert task.run_limits.max_usd == 0.5


def test_conflict_agent_and_run_limits_raises() -> None:
    """Setting agent.max_turns and run_limits.max_turns both is an error."""
    kwargs = _base_task_kwargs()
    kwargs["run_limits"] = {"max_turns": 20}
    kwargs["agent"] = {"type": "claude-code", "max_turns": 50}
    with pytest.raises(ValueError, match=r"max_turns.*both under agent.*run_limits"):
        TaskDefinition(**kwargs)


def test_conflict_turn_timeout_raises() -> None:
    kwargs = _base_task_kwargs()
    kwargs["run_limits"] = {"turn_timeout": 30}
    kwargs["agent"] = {"type": "claude-code", "turn_timeout": 60}
    with pytest.raises(ValueError, match=r"turn_timeout.*both under agent.*run_limits"):
        TaskDefinition(**kwargs)


def test_conflict_top_level_and_agent_emits_dual_location_error() -> None:
    """Top-level max_turns AND agent.max_turns both set → error names BOTH locations.

    Regression guard: the original Job-2 error message said "remove the
    top-level entry", which silently endorsed the value from the (also
    deprecated) agent: location. The fixed message names both locations
    so the user knows neither was canonical.
    """
    kwargs = _base_task_kwargs()
    kwargs["max_turns"] = 10
    kwargs["agent"] = {"type": "claude-code", "max_turns": 5}
    with pytest.raises(ValueError, match=r"max_turns.*both at top level and under agent"):
        TaskDefinition(**kwargs)


def test_conflict_top_level_and_run_limits_unchanged() -> None:
    """Top-level + run_limits: still raises the original message — no regression."""
    kwargs = _base_task_kwargs()
    kwargs["max_turns"] = 10
    kwargs["run_limits"] = {"max_turns": 20}
    with pytest.raises(ValueError, match=r"max_turns.*both at top level and in run_limits"):
        TaskDefinition(**kwargs)


def test_no_agent_block_is_noop() -> None:
    """Task with no `agent:` block at all triggers no warning and resolves cleanly."""
    kwargs = _base_task_kwargs()
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        task = TaskDefinition(**kwargs)
    assert task.agent is None
    assert task.run_limits is None
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


def test_run_limits_only_no_warning() -> None:
    """Setting run_limits canonically (no agent.*) emits no DeprecationWarning."""
    kwargs = _base_task_kwargs()
    kwargs["run_limits"] = {"max_turns": 25, "turn_timeout": 90}
    kwargs["agent"] = {"type": "claude-code"}
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        task = TaskDefinition(**kwargs)
    assert task.run_limits is not None
    assert task.run_limits.max_turns == 25
    assert task.run_limits.turn_timeout == 90
    deprecation_msgs = [w for w in captured if issubclass(w.category, DeprecationWarning)]
    assert deprecation_msgs == []


def test_hoist_does_not_mutate_caller_agent_dict() -> None:
    """Programmatic callers that reuse `agent` across two constructions should both succeed."""
    kwargs = _base_task_kwargs()
    agent_dict = {"type": "claude-code", "max_turns": 50}
    kwargs["agent"] = agent_dict
    with pytest.warns(DeprecationWarning):
        first = TaskDefinition(**kwargs)
    assert first.run_limits is not None
    assert first.run_limits.max_turns == 50
    # The caller's agent_dict should still carry max_turns, so a second
    # construction (e.g. inside a parametrize) hoists exactly the same way.
    assert agent_dict == {"type": "claude-code", "max_turns": 50}
    kwargs2 = _base_task_kwargs()
    kwargs2["agent"] = agent_dict
    with pytest.warns(DeprecationWarning):
        second = TaskDefinition(**kwargs2)
    assert second.run_limits is not None
    assert second.run_limits.max_turns == 50


def test_explicit_agent_kind_value_works() -> None:
    """Sanity check: AgentKind value 'claude-code' parses both as enum instance and string."""
    kwargs = _base_task_kwargs()
    kwargs["agent"] = {"type": AgentKind.CLAUDE_CODE.value, "max_turns": 5}
    with pytest.warns(DeprecationWarning):
        task = TaskDefinition(**kwargs)
    assert task.run_limits is not None
    assert task.run_limits.max_turns == 5
