"""Phase 1: ``PiAgentConfig`` model + ``AgentKind.PI`` enum member.

Registry-dispatch coverage (``parse_agent_config(type="pi")`` returning a
``PiAgentConfig``) lives in ``tests/test_pi_agent.py`` — it needs Phase 2's
registration. Here we exercise the model directly.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coder_eval.models import AgentConfig, AgentKind, PiAgentConfig
from coder_eval.models.agent_config import PiThinkingLevel


def test_agentkind_pi_value():
    assert AgentKind.PI == "pi"


def test_importable_from_models():
    # `from coder_eval.models import PiAgentConfig` must succeed (CE001).
    assert PiAgentConfig.__name__ == "PiAgentConfig"


def test_validates_and_defaults():
    cfg = PiAgentConfig(type="pi")
    assert cfg.type == AgentKind.PI
    assert cfg.thinking_level == "medium"


def test_round_trips_through_model_dump():
    cfg = PiAgentConfig(type="pi", model="openrouter/moonshotai/kimi-k3", thinking_level="high")
    restored = PiAgentConfig.model_validate(cfg.model_dump())
    assert restored == cfg
    assert restored.model == "openrouter/moonshotai/kimi-k3"
    assert restored.thinking_level == "high"


@pytest.mark.parametrize("level", ["off", "minimal", "low", "medium", "high", "xhigh", "max"])
def test_all_seven_thinking_levels_validate(level):
    assert PiAgentConfig(type="pi", thinking_level=level).thinking_level == level


def test_thinking_level_literal_has_exactly_seven_values():
    # PiThinkingLevel is a strict superset of the 4-value ThinkingLevel.
    assert set(PiThinkingLevel.__value__.__args__) == {  # type: ignore[attr-defined]
        "off",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    }


def test_invalid_thinking_level_rejected():
    with pytest.raises(ValidationError):
        PiAgentConfig(type="pi", thinking_level="ultra")  # type: ignore[arg-type]


def test_unknown_extra_field_rejected():
    with pytest.raises(ValidationError):
        PiAgentConfig(type="pi", not_a_field=1)  # type: ignore[call-arg]


def test_member_of_discriminated_union():
    from pydantic import TypeAdapter

    adapter = TypeAdapter(AgentConfig)
    cfg = adapter.validate_python({"type": "pi", "model": "openrouter/moonshotai/kimi-k3"})
    assert isinstance(cfg, PiAgentConfig)
