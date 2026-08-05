"""Tests for prompt file resolution and AgentConfig field validation."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from coder_eval.models import (
    AGENT_GID,
    AGENT_HIDDEN_TASK_FIELDS,
    AGENT_UID,
    AGENT_USERNAME,
    AgentConfig,
    AgentKind,
    SandboxConfig,
    TaskDefinition,
    TemplateDirSource,
    parse_agent_config,
)
from coder_eval.models.tasks import _AGENT_HIDDEN_FIELD_EMPTIES
from coder_eval.orchestration.experiment import resolve_task_files
from coder_eval.orchestration.task_loader import (
    load_task,
    parse_task_dict,
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
        resolve_agent_system_prompt(agent, tmp_path)
        assert agent.system_prompt == "System prompt content"
        assert agent.system_prompt_file is None

    def test_missing_file_raises(self, tmp_path):
        agent = parse_agent_config(type=AgentKind.CLAUDE_CODE, system_prompt_file="missing.md")
        with pytest.raises(FileNotFoundError):
            resolve_agent_system_prompt(agent, tmp_path)

    def test_no_file_field_is_noop(self, tmp_path):
        agent = parse_agent_config(type=AgentKind.CLAUDE_CODE, system_prompt="inline")
        resolve_agent_system_prompt(agent, tmp_path)
        assert agent.system_prompt == "inline"

    def test_none_agent_is_noop(self, tmp_path):
        """Passing None should not raise."""
        resolve_agent_system_prompt(None, tmp_path)

    def test_multiline_content_stripped(self, tmp_path):
        prompt_file = tmp_path / "system.md"
        prompt_file.write_text("\n  Line 1\n  Line 2\n\n")
        agent = parse_agent_config(type=AgentKind.CLAUDE_CODE, system_prompt_file="system.md")
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


class TestAgentSafeDump:
    """TaskDefinition.agent_safe_dump strips grading material for the docker barrier."""

    def test_strips_hidden_fields_leaves_rest_identical(self):
        task = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="go",
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[
                {"type": "file_contains", "path": "f.py", "includes": ["SECRET-ANSWER"], "description": "d"}
            ],
            reference={"code": "print('ref')"},
        )
        full = task.model_dump(mode="json")
        safe = task.agent_safe_dump()

        assert safe["success_criteria"] == []
        assert safe["reference"] is None
        # Every other key is byte-identical to the full dump.
        for key in full:
            if key in ("success_criteria", "reference"):
                continue
            assert safe[key] == full[key], key
        assert set(safe) == set(full)

    def test_idempotent_on_reference_none_task(self):
        # reference already None; a single criterion (min the validator allows).
        task = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="go",
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[{"type": "file_exists", "path": "f.py", "description": "d"}],
        )
        safe = task.agent_safe_dump()
        assert safe["success_criteria"] == []
        assert safe["reference"] is None
        # Applying agent_safe_dump semantics again over the same object is stable.
        assert task.agent_safe_dump() == safe

    def test_merged_back_projection_reparses(self, tmp_path):
        """The docker entrypoint strips the agent-readable task.yaml, then merges the
        full criteria back from the root-only channel before parsing. Assert that
        merge-back dict re-parses via parse_task_dict — the required-field edge case.

        (The bare stripped projection with success_criteria == [] deliberately does
        NOT re-parse: TaskDefinition requires >= 1 criterion. Production never parses
        the empty projection standalone.)
        """
        criteria = [{"type": "file_exists", "path": "f.py", "description": "d"}]
        task = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="go",
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=criteria,
            reference={"code": "print('ref')"},
        )
        stripped = task.agent_safe_dump()
        assert stripped["success_criteria"] == []
        # Merge the full criteria + reference back (what the root entrypoint does).
        merged = {**stripped, "success_criteria": criteria, "reference": {"code": "print('ref')"}}
        reparsed = parse_task_dict(merged, tmp_path)
        assert isinstance(reparsed, TaskDefinition)
        assert len(reparsed.success_criteria) == 1
        assert reparsed.reference is not None

    def test_hidden_fields_ssot_derivation(self):
        assert frozenset(_AGENT_HIDDEN_FIELD_EMPTIES) == AGENT_HIDDEN_TASK_FIELDS

    def test_agent_uid_constants_importable(self):
        assert (AGENT_UID, AGENT_GID, AGENT_USERNAME) == (2000, 2000, "agent")


class TestParseTaskDict:
    """parse_task_dict runs the same construction + four resolve_* steps as load_task."""

    def test_matches_load_task(self, tmp_path):
        # A task exercising system_prompt_file + a relative template_sources dir so
        # the resolve_* steps have real work to do.
        (tmp_path / "templates").mkdir()
        (tmp_path / "sysprompt.md").write_text("Be terse.\n", encoding="utf-8")
        task_file = tmp_path / "task.yaml"
        task_file.write_text(
            "task_id: t\n"
            "description: d\n"
            "initial_prompt: go\n"
            "agent:\n"
            "  type: claude-code\n"
            "  system_prompt_file: sysprompt.md\n"
            "sandbox:\n"
            "  driver: tempdir\n"
            "  template_sources:\n"
            "    - type: template_dir\n"
            "      path: templates\n"
            "success_criteria:\n"
            "  - type: file_exists\n"
            "    path: f.py\n"
            "    description: d\n",
            encoding="utf-8",
        )
        from_load, _ = load_task(task_file)
        raw = yaml.safe_load(task_file.read_text(encoding="utf-8"))
        from_parse = parse_task_dict(raw, task_file.parent)

        # Both resolved system_prompt inline (proves resolve_system_prompt_files ran)
        assert from_parse.agent.system_prompt == "Be terse."
        assert from_parse.agent.system_prompt_file is None
        # Both resolved the template path to absolute (proves resolve_template_paths ran)
        expected_template = str((tmp_path / "templates").resolve())
        assert from_parse.sandbox.template_sources[0].path == expected_template
        assert from_load.model_dump(mode="json") == from_parse.model_dump(mode="json")
