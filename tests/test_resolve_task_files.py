"""Tests for prompt file resolution and AgentConfig field validation."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from coder_eval.models import (
    AgentConfig,
    AgentKind,
    ClaudeCodeAgentConfig,
    SandboxConfig,
    TaskDefinition,
    TemplateDirSource,
    parse_agent_config,
)
from coder_eval.orchestration.experiment import resolve_task_files
from coder_eval.orchestration.task_loader import (
    load_task,
    resolve_agent_system_prompt,
    resolve_initial_prompt_file,
)


class TestAgentConfigValidation:
    """Test AgentConfig system_prompt / system_prompt_file validators."""

    def test_system_prompt_alone_valid(self):
        c = parse_agent_config(type=AgentKind.CLAUDE_CODE, system_prompt="You are a test agent.")
        assert c.system_prompt == "You are a test agent."
        assert c.system_prompt_file is None

    def test_system_prompt_file_alone_valid(self):
        c = parse_agent_config(type=AgentKind.CLAUDE_CODE, system_prompt_file="prompts/agent.md")
        assert c.system_prompt_file == "prompts/agent.md"
        assert c.system_prompt is None

    def test_both_raises(self):
        with pytest.raises(ValidationError, match="Only one of"):
            parse_agent_config(
                type=AgentKind.CLAUDE_CODE,
                system_prompt="inline",
                system_prompt_file="file.md",
            )

    def test_neither_valid(self):
        """Both None is fine — system prompt is optional."""
        c = parse_agent_config(type=AgentKind.CLAUDE_CODE)
        assert c.system_prompt is None
        assert c.system_prompt_file is None

    def test_setting_sources_none_default(self):
        c = parse_agent_config(type=AgentKind.CLAUDE_CODE)
        assert c.setting_sources is None

    def test_setting_sources_empty_list(self):
        c = parse_agent_config(type=AgentKind.CLAUDE_CODE, setting_sources=[])
        assert c.setting_sources == []

    def test_setting_sources_custom(self):
        c = parse_agent_config(type=AgentKind.CLAUDE_CODE, setting_sources=["project", "user"])
        assert c.setting_sources == ["project", "user"]

    def test_claude_settings_default_none(self):
        c = parse_agent_config(type=AgentKind.CLAUDE_CODE)
        assert c.claude_settings is None

    def test_claude_settings_dict(self):
        settings = {"permissions": {"deny": ["Read(/Users/you/src/**)"]}}
        c = parse_agent_config(type=AgentKind.CLAUDE_CODE, claude_settings=settings)
        assert c.claude_settings == settings

    def test_claude_settings_string_path(self):
        c = parse_agent_config(type=AgentKind.CLAUDE_CODE, claude_settings="/path/to/settings.json")
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
        agent = parse_agent_config(type=AgentKind.CLAUDE_CODE, system_prompt_file="system.md")
        resolved = resolve_agent_system_prompt(agent, tmp_path)
        assert resolved.system_prompt == "System prompt content"
        assert resolved.system_prompt_file is None

    def test_missing_file_raises(self, tmp_path):
        agent = parse_agent_config(type=AgentKind.CLAUDE_CODE, system_prompt_file="missing.md")
        with pytest.raises(FileNotFoundError):
            resolve_agent_system_prompt(agent, tmp_path)

    def test_no_file_field_is_noop(self, tmp_path):
        agent = parse_agent_config(type=AgentKind.CLAUDE_CODE, system_prompt="inline")
        assert resolve_agent_system_prompt(agent, tmp_path) is agent

    def test_none_agent_is_noop(self, tmp_path):
        """Passing None should not raise."""
        assert resolve_agent_system_prompt(None, tmp_path) is None

    def test_multiline_content_stripped(self, tmp_path):
        prompt_file = tmp_path / "system.md"
        prompt_file.write_text("\n  Line 1\n  Line 2\n\n")
        agent = parse_agent_config(type=AgentKind.CLAUDE_CODE, system_prompt_file="system.md")
        resolved = resolve_agent_system_prompt(agent, tmp_path)
        assert resolved.system_prompt == "Line 1\n  Line 2"

    def test_preserves_fields_set_for_the_merge_layer(self, tmp_path):
        """The resolved copy must keep __pydantic_fields_set__: experiment.py builds the
        task's merge layer with model_dump(exclude_unset=True), so a config that marked
        every field as set would override variant and CLI layers with its defaults."""
        prompt_file = tmp_path / "system.md"
        prompt_file.write_text("content")
        agent = parse_agent_config(type=AgentKind.CLAUDE_CODE, system_prompt_file="system.md")
        resolved = resolve_agent_system_prompt(agent, tmp_path)
        assert "model" not in resolved.model_fields_set
        assert resolved.model_dump(exclude_unset=True).keys() <= {
            "type",
            "system_prompt",
            "system_prompt_file",
        }

    @pytest.mark.parametrize("mode", ["append", "replace"])
    def test_replace_mode_with_prompt_file_resolves(self, tmp_path, mode: str):
        """Regression: the two prompt fields have no valid sequential assignment order
        under validate_assignment, so a non-atomic swap raised on the intermediate
        state — for 'replace' via check_replace_mode_has_prompt (both fields momentarily
        None) and for either mode via check_prompt_exclusivity (both momentarily set)."""
        prompt_file = tmp_path / "system.md"
        prompt_file.write_text("You are a judge.")
        agent = parse_agent_config(type=AgentKind.CLAUDE_CODE, system_prompt_file="system.md", system_prompt_mode=mode)
        resolved = resolve_agent_system_prompt(agent, tmp_path)
        assert resolved.system_prompt == "You are a judge."
        assert resolved.system_prompt_file is None
        assert resolved.system_prompt_mode == mode

    def test_blank_file_resolves_to_no_prompt(self, tmp_path):
        """A whitespace-only file is no prompt at all — same normalization inline
        prompts get, so 'replace' can never send an empty entire system prompt."""
        prompt_file = tmp_path / "system.md"
        prompt_file.write_text("   \n\t\n")
        agent = parse_agent_config(type=AgentKind.CLAUDE_CODE, system_prompt_file="system.md")
        assert resolve_agent_system_prompt(agent, tmp_path).system_prompt is None


def _write_task_with_agent(path: Path, agent: dict) -> Path:
    """Write a minimal task YAML carrying the given ``agent:`` block."""
    task_file = path / "t.yaml"
    task_file.write_text(
        yaml.dump(
            {
                "task_id": "t",
                "description": "Test task",
                "initial_prompt": "Do something",
                "sandbox": {"driver": "tempdir"},
                "success_criteria": [{"type": "file_exists", "path": "x.py", "description": "exists"}],
                "agent": agent,
            }
        )
    )
    return task_file


class TestSystemPromptMatrixThroughLoadTask:
    """The system_prompt x system_prompt_file x system_prompt_mode matrix, exercised
    through the real ``load_task`` entry point.

    Construction-only coverage is what let a load-time crash ship green: the config
    validated fine and only the loader's field swap blew up. These go through the
    loader so the resolved end state is what gets asserted.
    """

    @pytest.mark.parametrize("mode", ["append", "replace"])
    def test_prompt_file_is_inlined(self, tmp_path: Path, mode: str):
        (tmp_path / "sp.md").write_text("You are a careful engineer.")
        task_file = _write_task_with_agent(
            tmp_path, {"type": "claude-code", "system_prompt_file": "sp.md", "system_prompt_mode": mode}
        )

        task, _ = load_task(task_file)

        assert isinstance(task.agent, ClaudeCodeAgentConfig)
        assert task.agent.system_prompt == "You are a careful engineer."
        assert task.agent.system_prompt_file is None
        assert task.agent.system_prompt_mode == mode

    @pytest.mark.parametrize("mode", ["append", "replace"])
    def test_inline_prompt_survives(self, tmp_path: Path, mode: str):
        task_file = _write_task_with_agent(
            tmp_path, {"type": "claude-code", "system_prompt": "Be terse.", "system_prompt_mode": mode}
        )

        task, _ = load_task(task_file)

        assert isinstance(task.agent, ClaudeCodeAgentConfig)
        assert task.agent.system_prompt == "Be terse."
        assert task.agent.system_prompt_mode == mode

    def test_replace_without_any_prompt_is_rejected(self, tmp_path: Path):
        task_file = _write_task_with_agent(tmp_path, {"type": "claude-code", "system_prompt_mode": "replace"})

        with pytest.raises(ValueError, match="system_prompt_mode='replace' requires"):
            load_task(task_file)

    def test_both_prompt_fields_is_rejected(self, tmp_path: Path):
        (tmp_path / "sp.md").write_text("prompt")
        task_file = _write_task_with_agent(
            tmp_path, {"type": "claude-code", "system_prompt": "inline", "system_prompt_file": "sp.md"}
        )

        with pytest.raises(ValueError, match="Only one of"):
            load_task(task_file)

    def test_neither_prompt_field_leaves_defaults(self, tmp_path: Path):
        task_file = _write_task_with_agent(tmp_path, {"type": "claude-code"})

        task, _ = load_task(task_file)

        assert isinstance(task.agent, ClaudeCodeAgentConfig)
        assert task.agent.system_prompt is None
        assert task.agent.system_prompt_mode == "append"

    def test_missing_prompt_file_names_the_path(self, tmp_path: Path):
        task_file = _write_task_with_agent(tmp_path, {"type": "claude-code", "system_prompt_file": "nope.md"})

        with pytest.raises(ValueError, match="system_prompt_file not found"):
            load_task(task_file)


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

        agent = parse_agent_config(type=AgentKind.CLAUDE_CODE, system_prompt_file="agent_prompt.md")
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

        agent = parse_agent_config(type=AgentKind.CLAUDE_CODE, system_prompt_file=str(prompt))
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

        agent = parse_agent_config(type=AgentKind.CLAUDE_CODE, system_prompt_file="local_prompt.md")
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
