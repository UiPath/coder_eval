"""``agent.context_window``: one harness-neutral cap, applied per harness or rejected at load."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from coder_eval.agents.claude_code_agent import AUTO_COMPACT_WINDOW_ENV, ClaudeCodeAgent
from coder_eval.models import AgentKind, BaseAgentConfig, TaskDefinition, parse_agent_config
from coder_eval.orchestration.config_merge import Layer, resolve_root, validate_paths
from coder_eval.orchestration.overrides import OverrideError, apply_overrides


def _claude_env(**config_kwargs) -> dict[str, str]:
    agent = ClaudeCodeAgent(parse_agent_config(type=AgentKind.CLAUDE_CODE, **config_kwargs))
    agent.working_directory = Path(".")
    options, _transport, _model = agent._build_claude_query("hi", None, None, lambda _line: None)
    return options.env


def _task(agent: dict) -> TaskDefinition:
    return TaskDefinition.model_validate(
        {"task_id": "cw", "description": "d", "initial_prompt": "p", "agent": agent, "success_criteria": []}
    )


class TestClaudeCode:
    def test_a_capped_variant_sends_the_window_to_the_cli(self):
        assert _claude_env(context_window=200_000)[AUTO_COMPACT_WINDOW_ENV] == "200000"

    def test_an_uncapped_variant_leaves_the_cli_default(self):
        assert AUTO_COMPACT_WINDOW_ENV not in _claude_env()

    def test_an_uncapped_variant_ignores_a_window_set_on_the_host(self, monkeypatch):
        monkeypatch.setenv(AUTO_COMPACT_WINDOW_ENV, "150000")
        assert _claude_env()[AUTO_COMPACT_WINDOW_ENV] == ""
        assert _claude_env(context_window=300_000)[AUTO_COMPACT_WINDOW_ENV] == "300000"

    @pytest.mark.parametrize("window", [99_999, 1_000_001])
    def test_a_window_outside_the_cli_range_fails_at_load(self, window):
        with pytest.raises(ValidationError, match="out of range for agent type claude-code"):
            parse_agent_config(type=AgentKind.CLAUDE_CODE, context_window=window)

    def test_a_second_auto_compact_source_fails_at_load(self):
        with pytest.raises(ValidationError, match=r"claude_settings.autoCompactWindow"):
            parse_agent_config(
                type=AgentKind.CLAUDE_CODE,
                context_window=200_000,
                claude_settings={"autoCompactWindow": 400_000},
            )


class TestCodex:
    def test_a_capped_variant_sets_the_model_context_window(self):
        pytest.importorskip("openai_codex")
        from coder_eval.agents.codex_agent import CodexAgent

        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, context_window=400_000))
        assert agent._build_thread_options()["config"]["model_context_window"] == 400_000

    def test_an_uncapped_variant_leaves_the_codex_default(self):
        pytest.importorskip("openai_codex")
        from coder_eval.agents.codex_agent import CodexAgent

        options = CodexAgent(parse_agent_config(type=AgentKind.CODEX))._build_thread_options()
        assert "model_context_window" not in options.get("config", {})


class TestUnsupportedHarnesses:
    @pytest.mark.parametrize(
        "kind", [AgentKind.ANTIGRAVITY, AgentKind.OPENCODE, AgentKind.PI, AgentKind.DELEGATE, AgentKind.NONE]
    )
    def test_a_harness_without_the_knob_fails_at_load(self, kind):
        with pytest.raises(ValidationError, match="supported agent types: claude-code, codex"):
            parse_agent_config(type=kind, context_window=200_000)

    def test_a_non_positive_window_fails_even_before_the_type_is_known(self):
        with pytest.raises(ValidationError):
            parse_agent_config(context_window=0)


class TestConfigLayers:
    def test_a_type_less_task_defers_the_check_to_the_resolved_type(self):
        assert isinstance(parse_agent_config(context_window=50_000), BaseAgentConfig)

    def test_a_variant_caps_the_task_it_runs_and_records_it(self):
        lineage: dict = {}
        resolved = resolve_root(
            "agent",
            [
                Layer(source="task", patch={"type": "claude-code"}),
                Layer(source="variant", patch={"context_window": 200_000}, detail="variant cap-200k"),
            ],
            lineage=lineage,
        )
        assert resolved is not None
        assert resolved.model_dump()["context_window"] == 200_000
        assert lineage["agent.context_window"].source == "variant"

    def test_a_variant_switching_to_an_unsupported_type_fails_at_load(self):
        with pytest.raises(ValidationError, match="not supported by agent type opencode"):
            resolve_root(
                "agent",
                [
                    Layer(source="experiment-defaults", patch={"context_window": 200_000}),
                    Layer(source="variant", patch={"type": "opencode"}),
                ],
            )

    def test_a_cli_override_sets_the_cap(self):
        validate_paths(["agent.context_window"])
        task = _task({"type": "codex"})
        apply_overrides(task, {"agent.context_window": 400_000})
        assert task.agent is not None and task.agent.context_window == 400_000

    def test_a_cli_override_out_of_range_reads_as_a_clean_error(self):
        task = _task({"type": "claude-code"})
        with pytest.raises(OverrideError, match=r"-D agent: .*out of range"):
            apply_overrides(task, {"agent.context_window": 50_000})
