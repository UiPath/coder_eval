"""``agent.sdk_options.effort`` on Codex: the reasoning-effort tier, passed as ``model_reasoning_effort``.

These tests stop at the thread ``config`` coder_eval builds, as the tool output cap's do: the
openai_codex SDK passes that dict through unchecked, so a Codex release that renamed the key would
go unnoticed here.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from coder_eval.models import AgentKind, parse_agent_config
from coder_eval.models.agent_config import _CODEX_EFFORT_TIERS


def _thread_config(**config_kwargs: Any) -> dict[str, Any]:
    pytest.importorskip("openai_codex")
    from coder_eval.agents.codex_agent import CodexAgent

    agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="example-model", **config_kwargs))
    return agent._build_thread_options().get("config", {})


class TestThreadOptions:
    def test_an_effort_reaches_codex(self):
        assert _thread_config(sdk_options={"effort": "high"})["model_reasoning_effort"] == "high"

    def test_no_effort_leaves_the_codex_default(self):
        assert "model_reasoning_effort" not in _thread_config()


class TestValidation:
    def test_the_tiers_match_the_sdk(self):
        api = pytest.importorskip("openai_codex.api")
        assert {tier.value for tier in api.ReasoningEffort} == _CODEX_EFFORT_TIERS

    def test_a_key_codex_is_not_sent_is_rejected(self):
        with pytest.raises(ValidationError, match="are not Codex options"):
            parse_agent_config(type=AgentKind.CODEX, sdk_options={"max_thinking_tokens": 1024})

    @pytest.mark.parametrize("effort", ["extreme", 3, None])
    def test_an_effort_codex_does_not_know_is_rejected(self, effort):
        with pytest.raises(ValidationError, match=r"sdk_options\.effort must be one of"):
            parse_agent_config(type=AgentKind.CODEX, sdk_options={"effort": effort})
