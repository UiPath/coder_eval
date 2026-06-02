"""Phase-2 regression: ``max_turns`` and ``turn_timeout`` are no longer fields on
``AgentConfig``. They live on ``TaskDefinition`` (top-level) and on
``Agent.communicate(max_turns=...)`` per-call.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coder_eval.models import AgentKind, BaseAgentConfig, parse_agent_config


def test_agent_config_rejects_max_turns_as_extra() -> None:
    """Goal is field removal, not value rejection — match Pydantic's extra-forbid wording."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_agent_config(type=AgentKind.CLAUDE_CODE, max_turns=10)  # type: ignore[call-arg]


def test_agent_config_rejects_turn_timeout_as_extra() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_agent_config(type=AgentKind.CLAUDE_CODE, turn_timeout=60)  # type: ignore[call-arg]


def test_agent_config_no_timing_fields_in_schema() -> None:
    """Direct schema assertion: the fields are gone from the model definition."""
    assert "max_turns" not in BaseAgentConfig.model_fields
    assert "turn_timeout" not in BaseAgentConfig.model_fields


def test_agent_config_succeeds_without_timing_fields() -> None:
    cfg = parse_agent_config(type=AgentKind.CLAUDE_CODE)
    assert cfg.type == AgentKind.CLAUDE_CODE


def test_task_yaml_with_agent_max_turns_fails_loudly() -> None:
    """The legacy hoist shim is gone: ``agent.max_turns`` in a task definition now
    raises a clear extra='forbid' error instead of being silently hoisted."""
    from coder_eval.models import FileExistsCriterion, SandboxConfig, TaskDefinition

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="go",
            agent={"type": "claude-code", "max_turns": 10},
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(path="f.py", description="d")],
        )
