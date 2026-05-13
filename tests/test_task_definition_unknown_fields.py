"""Tests for the soft-launch warning on unknown TaskDefinition fields.

``TaskDefinition`` deliberately does NOT declare ``extra='forbid'`` (see the
class docstring) because the downstream skills task repo carries a long tail
of stale top-level fields (``max_iterations``, ``llm_reviewer``, ``skip``, …)
that used to be silently dropped. Instead, ``_warn_on_unknown_fields`` logs a
``DeprecationWarning`` per unknown key so authors see the typo at load time
without blocking the run.
"""

from __future__ import annotations

import warnings

import pytest

from coder_eval.models import (
    FileExistsCriterion,
    SandboxConfig,
    TaskDefinition,
)


def _base_task_kwargs() -> dict:
    return {
        "task_id": "warn_test",
        "description": "warn test",
        "initial_prompt": "do something",
        "sandbox": SandboxConfig(driver="tempdir"),
        "success_criteria": [FileExistsCriterion(type="file_exists", path="x.py", description="x.py exists")],
    }


def test_unknown_top_level_field_warns_but_does_not_raise() -> None:
    kwargs = _base_task_kwargs()
    kwargs["max_iterations"] = 1  # stale field — should warn, not raise
    with pytest.warns(DeprecationWarning, match=r"unknown top-level field 'max_iterations'"):
        task = TaskDefinition(**kwargs)
    # The unknown field is ignored — not surfaced as an attribute or in model_extra.
    assert not hasattr(task, "max_iterations")


def test_multiple_unknown_fields_each_emit_a_warning() -> None:
    """Three independent unknown keys each get their own DeprecationWarning."""
    kwargs = _base_task_kwargs()
    # Use field names that are NOT on TaskDefinition (max_iterations / llm_reviewer
    # are still stale; ``skip`` was promoted to a real field by #242 so don't reuse it).
    kwargs.update({"max_iterations": 1, "llm_reviewer": {"model": "x"}, "stale_field": "v"})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        TaskDefinition(**kwargs)
    deprecation_msgs = [str(w.message) for w in caught if issubclass(w.category, DeprecationWarning)]
    for key in ("max_iterations", "llm_reviewer", "stale_field"):
        assert any(key in msg for msg in deprecation_msgs), f"missing warning for {key!r}: {deprecation_msgs}"


def test_no_warning_when_all_fields_are_known() -> None:
    """A clean task definition emits zero DeprecationWarnings from this validator."""
    kwargs = _base_task_kwargs()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        TaskDefinition(**kwargs)
    unknown_field_warnings = [
        w for w in caught if issubclass(w.category, DeprecationWarning) and "unknown top-level field" in str(w.message)
    ]
    assert unknown_field_warnings == []
