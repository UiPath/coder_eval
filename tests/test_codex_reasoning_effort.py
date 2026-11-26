"""``agent.sdk_options.effort`` on Codex: the reasoning-effort tier, sent as the effort of every turn."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from coder_eval.models import AgentKind, parse_agent_config
from coder_eval.models.agent_config import _CODEX_EFFORT_TIERS
from coder_eval.reports.markdown import collect_agent_settings_rows


async def _turn_options(working_dir: Path, **config_kwargs: Any) -> dict[str, Any]:
    """The keyword options one ``communicate()`` passes to ``thread.turn``."""
    pytest.importorskip("openai_codex")
    from coder_eval.agents.codex_agent import CodexAgent
    from tests._fixtures.golden_streams.codex_fixtures import CODEX_SCENARIOS, _FakeThread

    seen: list[dict[str, Any]] = []

    class _RecordingThread(_FakeThread):
        def turn(self, user_input: str, **options: Any) -> Any:
            seen.append(options)
            return super().turn(user_input)

    scenario = next(s for s in CODEX_SCENARIOS if s.name == "a_agent_message_only")
    agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="example-model", **config_kwargs))
    agent.working_directory = working_dir
    agent.codex_client = SimpleNamespace(close=lambda: None)
    agent.thread = _RecordingThread(scenario.notifications)
    with patch.dict(os.environ, {"CODEX_HOME": str(working_dir)}):
        await agent.communicate("do it")
    assert len(seen) == 1
    return seen[0]


class TestTurnOptions:
    async def test_an_effort_reaches_the_turn(self, tmp_path):
        api = pytest.importorskip("openai_codex.api")
        options = await _turn_options(tmp_path, sdk_options={"effort": "high"})
        assert options == {"effort": api.ReasoningEffort.high}

    async def test_no_effort_leaves_the_codex_default(self, tmp_path):
        assert await _turn_options(tmp_path) == {}


class TestValidation:
    def test_the_tiers_match_the_sdk(self):
        api = pytest.importorskip("openai_codex.api")
        assert {tier.value for tier in api.ReasoningEffort} == _CODEX_EFFORT_TIERS

    def test_a_key_codex_is_not_sent_is_rejected(self):
        with pytest.raises(ValidationError, match="are not Codex options"):
            parse_agent_config(type=AgentKind.CODEX, sdk_options={"max_thinking_tokens": 1024})

    @pytest.mark.parametrize("effort", ["extreme", 3, None, ["high"], {"tier": "high"}])
    def test_an_effort_codex_does_not_know_is_rejected(self, effort):
        with pytest.raises(ValidationError, match=r"sdk_options\.effort must be"):
            parse_agent_config(type=AgentKind.CODEX, sdk_options={"effort": effort})


def test_the_agent_settings_show_the_effort():
    config = parse_agent_config(type=AgentKind.CODEX, sdk_options={"effort": "high"})
    rows = dict(collect_agent_settings_rows(config.model_dump(), is_sdk=False))
    assert rows["Effort"] == "high"
