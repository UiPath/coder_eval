"""``agent.tool_output_token_limit``: an explicit Codex tool-output cap, not one from Codex's model catalog."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from coder_eval.models import AgentKind, TaskDefinition, parse_agent_config
from coder_eval.orchestration.config_merge import Layer, resolve_root
from coder_eval.orchestration.overrides import apply_overrides, parse_override


def _thread_config(**config_kwargs: Any) -> dict[str, Any]:
    pytest.importorskip("openai_codex")
    from coder_eval.agents.codex_agent import CodexAgent

    agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="example-model", **config_kwargs))
    return agent._build_thread_options().get("config", {})


def _task(agent: dict[str, Any]) -> TaskDefinition:
    return TaskDefinition.model_validate(
        {"task_id": "cap", "description": "d", "initial_prompt": "p", "agent": agent, "success_criteria": []}
    )


class TestThreadOptions:
    def test_the_default_cap_reaches_codex(self):
        assert _thread_config()["tool_output_token_limit"] == 10_000

    def test_an_override_reaches_codex(self):
        assert _thread_config(tool_output_token_limit=25_000)["tool_output_token_limit"] == 25_000

    def test_none_leaves_the_codex_default(self):
        assert "tool_output_token_limit" not in _thread_config(tool_output_token_limit=None)


class TestConfig:
    @pytest.mark.parametrize("limit", [0, -1])
    def test_a_non_positive_cap_fails_at_load(self, limit):
        with pytest.raises(ValidationError):
            parse_agent_config(type=AgentKind.CODEX, tool_output_token_limit=limit)

    def test_another_harness_rejects_the_field(self):
        with pytest.raises(ValidationError):
            parse_agent_config(type=AgentKind.CLAUDE_CODE, tool_output_token_limit=10_000)

    def test_the_run_records_the_default(self):
        assert parse_agent_config(type=AgentKind.CODEX).model_dump()["tool_output_token_limit"] == 10_000

    def test_yaml_null_survives_the_merge(self):
        resolved = resolve_root(
            "agent",
            [
                Layer(source="experiment-defaults", patch={"type": "codex"}),
                Layer(source="variant", patch={"tool_output_token_limit": None}),
            ],
        )
        assert resolved is not None
        assert resolved.model_dump()["tool_output_token_limit"] is None

    @pytest.mark.parametrize(("raw", "expected"), [("20000", 20_000), ("null", None)])
    def test_a_cli_override_sets_the_cap(self, raw, expected):
        task = _task({"type": "codex"})
        apply_overrides(task, dict([parse_override(f"agent.tool_output_token_limit={raw}")]))
        assert task.agent is not None
        assert task.agent.model_dump()["tool_output_token_limit"] == expected
