"""``agent.context_window``: one harness-neutral cap, applied per harness or rejected at load."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from coder_eval.agents.claude_code_agent import AUTO_COMPACT_WINDOW_ENV, ClaudeCodeAgent
from coder_eval.agents.pi_agent import _PI_MODELS_FILE as _PI_MODELS
from coder_eval.agents.pi_agent import PI_AGENT_DIR_ENV, PiAgent, _pi_token_count
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


PI_MODEL = "anthropic/claude-sonnet-4-5"


def _pi_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    host = tmp_path / "host-agent"
    (host / "extensions").mkdir(parents=True)
    (host / "auth.json").write_text('{"anthropic": {"type": "api_key"}}', encoding="utf-8")
    (host / "settings.json").write_text('{"compaction": {"reserveTokens": 16384}}', encoding="utf-8")
    (host / _PI_MODELS).write_text(
        json.dumps({"providers": {"anthropic": {"modelOverrides": {"claude-opus-4-5": {"maxTokens": 8000}}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv(PI_AGENT_DIR_ENV, str(host))
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/pi")
    return host


async def _started_pi(tmp_path: Path, listed: str | None, **config_kwargs) -> PiAgent:
    agent = PiAgent(parse_agent_config(type=AgentKind.PI, **config_kwargs))

    async def list_models(_provider: str, _model_id: str) -> str | None:
        return listed

    agent._listed_context = list_models  # type: ignore[method-assign]
    await agent.start(str(tmp_path))
    return agent


class TestPi:
    async def test_a_capped_variant_runs_pi_from_a_mirror_whose_models_json_caps_the_model(self, tmp_path, monkeypatch):
        host = _pi_host(tmp_path, monkeypatch)
        agent = await _started_pi(tmp_path, "200K", model=PI_MODEL, context_window=200_000)

        mirror = Path(agent._build_env()[PI_AGENT_DIR_ENV])
        assert mirror != host
        models = json.loads((mirror / _PI_MODELS).read_text(encoding="utf-8"))["providers"]["anthropic"]
        assert models["modelOverrides"]["claude-sonnet-4-5"] == {"contextWindow": 200_000}
        assert models["modelOverrides"]["claude-opus-4-5"] == {"maxTokens": 8000}
        assert (mirror / "auth.json").read_text(encoding="utf-8") == (host / "auth.json").read_text(encoding="utf-8")
        assert (mirror / "settings.json").exists() and (mirror / "extensions").is_dir()
        assert agent.get_environment_info()["pi_context_window"] == 200_000

        await agent.stop()
        assert not mirror.exists()
        assert json.loads((host / _PI_MODELS).read_text(encoding="utf-8"))["providers"]["anthropic"][
            "modelOverrides"
        ] == {"claude-opus-4-5": {"maxTokens": 8000}}
        assert (host / "auth.json").exists() and (host / "extensions").is_dir()

    async def test_a_cap_pi_does_not_apply_fails_the_start(self, tmp_path, monkeypatch):
        _pi_host(tmp_path, monkeypatch)
        agent = PiAgent(parse_agent_config(type=AgentKind.PI, model=PI_MODEL, context_window=200_000))

        async def unknown_model(_provider: str, _model_id: str) -> str | None:
            return None

        agent._listed_context = unknown_model  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="did not apply to anthropic/claude-sonnet-4-5"):
            await agent.start(str(tmp_path))
        assert agent._agent_dir is None

    async def test_an_uncapped_variant_keeps_the_host_agent_dir(self, tmp_path, monkeypatch):
        host = _pi_host(tmp_path, monkeypatch)
        agent = await _started_pi(tmp_path, None, model=PI_MODEL)
        assert agent._build_env()[PI_AGENT_DIR_ENV] == str(host)
        assert "pi_context_window" not in agent.get_environment_info()

    def test_a_window_needs_the_provider_and_model_it_caps(self):
        with pytest.raises(ValidationError, match="provider/model form"):
            parse_agent_config(type=AgentKind.PI, context_window=200_000)
        with pytest.raises(ValidationError, match="provider/model form"):
            parse_agent_config(type=AgentKind.PI, model="claude-sonnet-4-5", context_window=200_000)

    def test_a_window_under_the_compaction_reserve_fails_at_load(self):
        with pytest.raises(ValidationError, match="out of range for agent type pi"):
            parse_agent_config(type=AgentKind.PI, model=PI_MODEL, context_window=16_000)

    @pytest.mark.parametrize(("tokens", "printed"), [(200_000, "200K"), (1_000_000, "1M"), (32_768, "32.8K")])
    def test_the_check_reads_counts_as_pi_prints_them(self, tokens, printed):
        assert _pi_token_count(tokens) == printed


class TestUnsupportedHarnesses:
    @pytest.mark.parametrize("kind", [AgentKind.ANTIGRAVITY, AgentKind.OPENCODE, AgentKind.DELEGATE, AgentKind.NONE])
    def test_a_harness_without_the_knob_fails_at_load(self, kind):
        with pytest.raises(ValidationError, match="supported agent types: claude-code, codex, pi"):
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
