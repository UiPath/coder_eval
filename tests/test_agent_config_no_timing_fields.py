"""Phase-2 regression: ``max_turns`` and ``turn_timeout`` are no longer fields on
``AgentConfig``. They live on ``TaskDefinition`` (top-level) and on
``Agent.communicate(max_turns=...)`` per-call.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coder_eval.models import AgentConfig, AgentKind


def test_agent_config_rejects_max_turns_as_extra() -> None:
    """Goal is field removal, not value rejection — match Pydantic's extra-forbid wording."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AgentConfig(type=AgentKind.CLAUDE_CODE, max_turns=10)  # type: ignore[call-arg]


def test_agent_config_rejects_turn_timeout_as_extra() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AgentConfig(type=AgentKind.CLAUDE_CODE, turn_timeout=60)  # type: ignore[call-arg]


def test_agent_config_no_timing_fields_in_schema() -> None:
    """Direct schema assertion: the fields are gone from the model definition."""
    assert "max_turns" not in AgentConfig.model_fields
    assert "turn_timeout" not in AgentConfig.model_fields


def test_agent_config_succeeds_without_timing_fields() -> None:
    cfg = AgentConfig(type=AgentKind.CLAUDE_CODE)
    assert cfg.type == AgentKind.CLAUDE_CODE
