"""Tests for prompt file resolution and AgentConfig field validation."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from coder_eval.models import AgentConfig, AgentKind, SandboxConfig, TaskDefinition, TemplateDirSource
from coder_eval.orchestration.experiment import _merge_agent_dicts, resolve_task_files
from coder_eval.orchestration.task_loader import (
    resolve_agent_system_prompt,
    resolve_initial_prompt_file,
)


class TestAgentConfigValidation:
    """Test AgentConfig system_prompt / system_prompt_file validators."""

    def test_system_prompt_alone_valid(self):
        c = AgentConfig(type=AgentKind.CLAUDE_CODE, system_prompt="You are a test agent.")
        assert c.system_prompt == "You are a test agent."
        assert c.system_prompt_file is None

    def test_system_prompt_file_alone_valid(self):
        c = AgentConfig(type=AgentKind.CLAUDE_CODE, system_prompt_file="prompts/agent.md")
        assert c.system_prompt_file == "prompts/agent.md"
        assert c.system_prompt is None

    def test_both_raises(self):
        with pytest.raises(ValidationError, match="Only one of"):
            AgentConfig(
                type=AgentKind.CLAUDE_CODE,
                system_prompt="inline",
                system_prompt_file="file.md",
            )

    def test_neither_valid(self):
        """Both None is fine — system prompt is optional."""
        c = AgentConfig(type=AgentKind.CLAUDE_CODE)
        assert c.system_prompt is None
        assert c.system_prompt_file is None

    def test_setting_sources_none_default(self):
        c = AgentConfig(type=AgentKind.CLAUDE_CODE)
        assert c.setting_sources is None

    def test_setting_sources_empty_list(self):
        c = AgentConfig(type=AgentKind.CLAUDE_CODE, setting_sources=[])
        assert c.setting_sources == []

    def test_setting_sources_custom(self):
        c = AgentConfig(type=AgentKind.CLAUDE_CODE, setting_sources=["project", "user"])
        assert c.setting_sources == ["project", "user"]

    def test_claude_settings_default_none(self):
        c = AgentConfig(type=AgentKind.CLAUDE_CODE)
        assert c.claude_settings is None

    def test_claude_settings_dict(self):
        settings = {"permissions": {"deny": ["Read(/Users/religa/src/**)"]}}
        c = AgentConfig(type=AgentKind.CLAUDE_CODE, claude_settings=settings)
        assert c.claude_settings == settings

    def test_claude_settings_string_path(self):
        c = AgentConfig(type=AgentKind.CLAUDE_CODE, claude_settings="/path/to/settings.json")
        assert c.claude_settings == "/path/to/settings.json"


class TestTaskDefinitionPromptValidation:
    """Test TaskDefinition initial_prompt / initial_prompt_file validators."""

    def test_initial_prompt_alone_valid(self):
        t = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="hello",
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[{"type": "file_exists", "path": "f.py", "description": "d"}],
        )
        assert t.initial_prompt == "hello"

    def test_initial_prompt_file_alone_valid(self):
        t = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt_file="prompts/task.md",
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[{"type": "file_exists", "path": "f.py", "description": "d"}],
        )
        assert t.initial_prompt_file == "prompts/task.md"

    def test_both_raises(self):
        with pytest.raises(ValidationError, match="Only one of"):
            TaskDefinition(
                task_id="t",
                description="d",
                initial_prompt="inline",
                initial_prompt_file="file.md",
                sandbox=SandboxConfig(driver="tempdir"),
                success_criteria=[{"type": "file_exists", "path": "f.py", "description": "d"}],
            )

    def test_neither_raises(self):
        with pytest.raises(ValidationError, match="Either"):
            TaskDefinition(
                task_id="t",
                description="d",
                sandbox=SandboxConfig(driver="tempdir"),
                success_criteria=[{"type": "file_exists", "path": "f.py", "description": "d"}],
            )


class TestResolveInitialPromptFile:
    """Test resolve_initial_prompt_file from task_loader."""

    def test_resolves_relative_path(self, tmp_path):
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Hello from file\n")
        task = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt_file="prompt.md",
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[{"type": "file_exists", "path": "f.py", "description": "d"}],
        )
        resolve_initial_prompt_file(task, tmp_path)
        assert task.initial_prompt == "Hello from file"
        assert task.initial_prompt_file is None

    def test_missing_file_raises(self, tmp_path):
        task = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt_file="missing.md",
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[{"type": "file_exists", "path": "f.py", "description": "d"}],
        )
        with pytest.raises(FileNotFoundError):
            resolve_initial_prompt_file(task, tmp_path)

    def test_inline_prompt_untouched(self, tmp_path):
        task = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="inline text",
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[{"type": "file_exists", "path": "f.py", "description": "d"}],
        )
        resolve_initial_prompt_file(task, tmp_path)
        assert task.initial_prompt == "inline text"


class TestResolveAgentSystemPrompt:
    """Test resolve_agent_system_prompt from task_loader."""

    def test_resolves_relative_path(self, tmp_path):
        prompt_file = tmp_path / "system.md"
        prompt_file.write_text("System prompt content\n")
        agent = AgentConfig(type=AgentKind.CLAUDE_CODE, system_prompt_file="system.md")
        resolve_agent_system_prompt(agent, tmp_path)
        assert agent.system_prompt == "System prompt content"
        assert agent.system_prompt_file is None

    def test_missing_file_raises(self, tmp_path):
        agent = AgentConfig(type=AgentKind.CLAUDE_CODE, system_prompt_file="missing.md")
        with pytest.raises(FileNotFoundError):
            resolve_agent_system_prompt(agent, tmp_path)

    def test_no_file_field_is_noop(self, tmp_path):
        agent = AgentConfig(type=AgentKind.CLAUDE_CODE, system_prompt="inline")
        resolve_agent_system_prompt(agent, tmp_path)
        assert agent.system_prompt == "inline"

    def test_none_agent_is_noop(self, tmp_path):
        """Passing None should not raise."""
        resolve_agent_system_prompt(None, tmp_path)

    def test_multiline_content_stripped(self, tmp_path):
        prompt_file = tmp_path / "system.md"
        prompt_file.write_text("\n  Line 1\n  Line 2\n\n")
        agent = AgentConfig(type=AgentKind.CLAUDE_CODE, system_prompt_file="system.md")
        resolve_agent_system_prompt(agent, tmp_path)
        assert agent.system_prompt == "Line 1\n  Line 2"


def _make_task(agent: AgentConfig | None = None, template_sources: list | None = None) -> TaskDefinition:
    """Helper to build a minimal TaskDefinition."""
    return TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="go",
        agent=agent,
        sandbox=SandboxConfig(driver="tempdir", template_sources=template_sources),
        success_criteria=[{"type": "file_exists", "path": "f.py", "description": "d"}],
    )


class TestResolveTaskFiles:
    """Integration tests for resolve_task_files (post-variant resolution)."""

    def test_resolves_system_prompt_file_relative_to_experiment(self, tmp_path):
        """Variant-injected system_prompt_file resolves against experiment dir."""
        exp_dir = tmp_path / "experiments"
        exp_dir.mkdir()
        prompt = exp_dir / "agent_prompt.md"
        prompt.write_text("Be concise.\n")

        task_dir = tmp_path / "tasks"
        task_dir.mkdir()
        task_file = task_dir / "task.yaml"
        task_file.touch()
        experiment_file = exp_dir / "experiment.yaml"
        experiment_file.touch()

        agent = AgentConfig(type=AgentKind.CLAUDE_CODE, system_prompt_file="agent_prompt.md")
        task = _make_task(agent=agent)

        resolve_task_files(task, task_file, experiment_file)

        assert task.agent.system_prompt == "Be concise."
        assert task.agent.system_prompt_file is None

    def test_resolves_system_prompt_file_absolute_path(self, tmp_path):
        """Absolute system_prompt_file is resolved regardless of base dirs."""
        prompt = tmp_path / "abs_prompt.md"
        prompt.write_text("Absolute prompt\n")

        task_file = tmp_path / "tasks" / "task.yaml"
        task_file.parent.mkdir()
        task_file.touch()

        agent = AgentConfig(type=AgentKind.CLAUDE_CODE, system_prompt_file=str(prompt))
        task = _make_task(agent=agent)

        resolve_task_files(task, task_file)

        assert task.agent.system_prompt == "Absolute prompt"
        assert task.agent.system_prompt_file is None

    def test_falls_back_to_task_dir_when_no_experiment_file(self, tmp_path):
        """Without experiment_file, relative paths resolve against task dir."""
        prompt = tmp_path / "tasks" / "local_prompt.md"
        prompt.parent.mkdir()
        prompt.write_text("Local prompt\n")
        task_file = tmp_path / "tasks" / "task.yaml"
        task_file.touch()

        agent = AgentConfig(type=AgentKind.CLAUDE_CODE, system_prompt_file="local_prompt.md")
        task = _make_task(agent=agent)

        resolve_task_files(task, task_file)

        assert task.agent.system_prompt == "Local prompt"

    def test_resolves_template_source_paths_relative_to_experiment(self, tmp_path):
        """Variant-injected template paths resolve against experiment dir."""
        exp_dir = tmp_path / "experiments"
        exp_dir.mkdir()
        tpl_dir = exp_dir / "tpl"
        tpl_dir.mkdir()
        experiment_file = exp_dir / "experiment.yaml"
        experiment_file.touch()

        task_dir = tmp_path / "tasks"
        task_dir.mkdir()
        task_file = task_dir / "task.yaml"
        task_file.touch()

        source = TemplateDirSource(path="tpl")
        task = _make_task(template_sources=[source])

        resolve_task_files(task, task_file, experiment_file)

        assert Path(source.path).is_absolute()
        assert Path(source.path) == tpl_dir.resolve()

    def test_skips_already_absolute_template_paths(self, tmp_path):
        """Already-absolute template paths are left unchanged."""
        abs_path = str(tmp_path / "absolute" / "templates")
        source = TemplateDirSource(path=abs_path)
        task = _make_task(template_sources=[source])
        task_file = tmp_path / "task.yaml"
        task_file.touch()

        resolve_task_files(task, task_file)

        assert source.path == abs_path

    def test_noop_when_no_agent_and_no_templates(self, tmp_path):
        """No-op when task has no agent and no template sources."""
        task = _make_task()
        task_file = tmp_path / "task.yaml"
        task_file.touch()

        resolve_task_files(task, task_file)

        assert task.agent is None


class TestMergeAgentDictsPromptExclusivity:
    """Test that _merge_agent_dicts handles mutually exclusive prompt fields."""

    def test_variant_system_prompt_file_overrides_resolved_system_prompt(self):
        """When task has resolved system_prompt (from file), variant system_prompt_file wins."""
        # Simulates what model_dump(exclude_unset=True) produces after load_task resolves the file
        task_agent = {"type": "claude-code", "system_prompt": "resolved content", "system_prompt_file": None}
        variant_agent = {"system_prompt_file": "variant_prompt.md"}

        merged = _merge_agent_dicts(task_agent, variant_agent)
        # Should not crash AgentConfig validation
        config = AgentConfig(**merged)
        assert config.system_prompt_file == "variant_prompt.md"
        assert config.system_prompt is None

    def test_variant_system_prompt_overrides_task_system_prompt_file(self):
        """When task has system_prompt_file, variant inline system_prompt wins."""
        task_agent = {"type": "claude-code", "system_prompt_file": "task.md"}
        variant_agent = {"system_prompt": "inline from variant"}

        merged = _merge_agent_dicts(task_agent, variant_agent)
        config = AgentConfig(**merged)
        assert config.system_prompt == "inline from variant"
        assert config.system_prompt_file is None

    def test_no_prompt_fields_unaffected(self):
        """Merge without prompt fields works as before."""
        layer1 = {"type": "claude-code", "model": "opus"}
        layer2 = {"model": "sonnet", "max_turns": 5}

        merged = _merge_agent_dicts(layer1, layer2)
        assert merged == {"type": "claude-code", "model": "sonnet", "max_turns": 5}
