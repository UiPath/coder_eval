"""Phase 1: ``DelegateAgentConfig`` model + ``AgentKind.DELEGATE`` enum member.

Registry-dispatch coverage (``parse_agent_config(type="delegate")`` returning a
``DelegateAgentConfig``) lives in ``tests/test_delegate_agent.py`` — it needs
Phase 2's registration. Here we exercise the model directly.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coder_eval.models import AgentConfig, AgentKind, DelegateAgentConfig


def test_agentkind_delegate_value():
    assert AgentKind.DELEGATE == "delegate"


def test_importable_from_models():
    # `from coder_eval.models import DelegateAgentConfig` must succeed (CE001).
    assert DelegateAgentConfig.__name__ == "DelegateAgentConfig"


def test_validates_and_defaults():
    cfg = DelegateAgentConfig(type="delegate")
    assert cfg.type == AgentKind.DELEGATE
    assert cfg.effort is None
    assert cfg.project_id == ""
    assert cfg.session_id == ""
    assert cfg.enable_computer_use is False


def test_round_trips_through_model_dump():
    cfg = DelegateAgentConfig(
        type="delegate",
        model="virtuoso-1-5",
        effort="high",
        project_id="invoice-approval",
        session_id="abc-123",
        enable_computer_use=True,
    )
    restored = DelegateAgentConfig.model_validate(cfg.model_dump())
    assert restored == cfg
    assert restored.model == "virtuoso-1-5"
    assert restored.effort == "high"
    assert restored.project_id == "invoice-approval"
    assert restored.session_id == "abc-123"
    assert restored.enable_computer_use is True


def test_effort_accepts_any_string_including_future_tiers():
    # Deliberately permissive: the SDK ignores an unrecognized value, so a
    # strict Literal here would reject a tier a future SDK release adds.
    assert DelegateAgentConfig(type="delegate", effort="ultra-max").effort == "ultra-max"


def test_unknown_extra_field_rejected():
    with pytest.raises(ValidationError):
        DelegateAgentConfig(type="delegate", not_a_field=1)  # type: ignore[call-arg]


def test_member_of_discriminated_union():
    from pydantic import TypeAdapter

    adapter = TypeAdapter(AgentConfig)
    cfg = adapter.validate_python({"type": "delegate", "model": "virtuoso-1-5"})
    assert isinstance(cfg, DelegateAgentConfig)
