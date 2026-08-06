"""Tests for prompt file resolution and AgentConfig field validation."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from coder_eval.models import (
    AGENT_HIDDEN_TASK_FIELDS,
    PLUGIN_AGENT_ALLOWED_SUBDIRS,
    AgentConfig,
    AgentKind,
    FileExistsCriterion,
    ReferenceSource,
    SandboxConfig,
    TaskDefinition,
    TemplateDirSource,
    parse_agent_config,
    project_plugin_for_agent,
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
    """agent_safe_dump strips only the grading-material fields, leaves the rest intact."""

    def _task_with_criteria(self) -> TaskDefinition:
        return TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="do the thing",
            sandbox=SandboxConfig(),
            reference=ReferenceSource(code="the reference solution"),
            success_criteria=[FileExistsCriterion(description="c", path="out.txt")],
        )

    def test_strips_exactly_the_hidden_fields(self):
        task = self._task_with_criteria()
        full = task.model_dump(mode="json")
        safe = task.agent_safe_dump()
        # The two hidden fields are emptied...
        assert safe["success_criteria"] == []
        assert safe["reference"] is None
        # ...and every OTHER key is byte-identical to the full dump.
        for key in full:
            if key in AGENT_HIDDEN_TASK_FIELDS:
                continue
            assert safe[key] == full[key], f"non-hidden field {key} was altered"

    def test_ssot_hidden_fields_derived_from_empties_map(self):
        assert frozenset(_AGENT_HIDDEN_FIELD_EMPTIES) == AGENT_HIDDEN_TASK_FIELDS

    def test_idempotent_on_already_empty_task(self):
        task = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="p",
            sandbox=SandboxConfig(),
            success_criteria=[],
        )
        safe = task.agent_safe_dump()
        assert safe["success_criteria"] == []
        assert safe["reference"] is None

    def test_stripped_dump_reparses(self):
        task = self._task_with_criteria()
        # The stripped dump has success_criteria: [] — only the container staging
        # re-parse loads it, via allow_empty_criteria=True (authored [] is rejected).
        reparsed = parse_task_dict(task.agent_safe_dump(), Path.cwd(), allow_empty_criteria=True)
        assert isinstance(reparsed, TaskDefinition)
        assert reparsed.success_criteria == []
        assert reparsed.reference is None

    def test_type_none_task(self):
        """The docstring claims safety for type: none tasks — only the two hidden
        fields are touched; agent.type survives."""
        # A type: none task runs no agent, so it must NOT set initial_prompt.
        task = TaskDefinition(
            task_id="t",
            description="d",
            sandbox=SandboxConfig(),
            agent={"type": "none"},
            success_criteria=[FileExistsCriterion(description="c", path="o.txt")],
        )
        safe = task.agent_safe_dump()
        assert safe["agent"]["type"] == "none"
        assert safe["success_criteria"] == []
        assert safe["reference"] is None

    def test_double_apply_is_byte_identical(self):
        """True idempotency: strip → re-parse (container path) → strip again yields a
        byte-identical dump (no field drift across the round-trip)."""
        task = self._task_with_criteria()
        safe1 = task.agent_safe_dump()
        reparsed = parse_task_dict(safe1, Path.cwd(), allow_empty_criteria=True)
        safe2 = reparsed.agent_safe_dump()
        assert safe1 == safe2


class TestParseTaskDict:
    """parse_task_dict runs all four resolve_* steps and matches load_task."""

    def test_roundtrip_agent_safe_dump(self, tmp_path):
        task = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="p",
            sandbox=SandboxConfig(),
            success_criteria=[FileExistsCriterion(description="c", path="o.txt")],
        )
        # agent_safe_dump strips success_criteria to []; only the container
        # staging re-parse loads that, via allow_empty_criteria=True.
        reparsed = parse_task_dict(task.agent_safe_dump(), tmp_path, allow_empty_criteria=True)
        assert reparsed.task_id == "t"

    def test_equals_load_task_for_system_prompt_file(self, tmp_path):
        """Proves the resolve_* steps run: a relative system_prompt_file is inlined."""
        prompt_file = tmp_path / "sys.txt"
        prompt_file.write_text("SYSTEM PROMPT BODY", encoding="utf-8")
        task_yaml = tmp_path / "task.yaml"
        raw = {
            "task_id": "t",
            "description": "d",
            "initial_prompt": "p",
            "agent": {"type": "claude-code", "system_prompt_file": "sys.txt"},
            "success_criteria": [{"type": "file_exists", "description": "c", "path": "o.txt"}],
        }
        task_yaml.write_text(yaml.safe_dump(raw), encoding="utf-8")

        loaded, _ = load_task(task_yaml)
        parsed = parse_task_dict(yaml.safe_load(task_yaml.read_text(encoding="utf-8")), task_yaml.parent)

        # The resolve step inlined the file (read the expected value from the fixture).
        expected = prompt_file.read_text(encoding="utf-8").strip()
        assert loaded.agent is not None and loaded.agent.system_prompt == expected
        assert parsed.agent is not None and parsed.agent.system_prompt == expected
        assert loaded.agent.system_prompt_file is None
        assert parsed.agent.system_prompt_file is None


class TestProjectPluginForAgent:
    """project_plugin_for_agent copies only the allowlisted subtrees."""

    def test_copies_allowed_and_omits_grader_material(self, tmp_path):
        src = tmp_path / "plugin"
        (src / "skills").mkdir(parents=True)
        (src / "skills" / "SKILL.md").write_text("docs", encoding="utf-8")
        (src / "tests").mkdir()
        (src / "tests" / "check_x.py").write_text("assert True", encoding="utf-8")
        (src / "RESOLUTION.md").write_text("the answer", encoding="utf-8")

        dst = tmp_path / "bundle"
        project_plugin_for_agent(src, dst)

        assert (dst / "skills" / "SKILL.md").is_file()
        assert not (dst / "tests").exists()
        assert not (dst / "RESOLUTION.md").exists()

    def test_empty_when_no_allowed_subdirs(self, tmp_path):
        src = tmp_path / "plugin"
        (src / "tests").mkdir(parents=True)
        (src / "tests" / "check_x.py").write_text("x", encoding="utf-8")
        dst = tmp_path / "bundle"
        project_plugin_for_agent(src, dst)
        assert dst.is_dir()
        assert list(dst.iterdir()) == []

    def test_allowlist_membership(self):
        # Grader/reference trees are never in the allowlist.
        assert "tests" not in PLUGIN_AGENT_ALLOWED_SUBDIRS
        assert "reference_agents" not in PLUGIN_AGENT_ALLOWED_SUBDIRS
        assert "skills" in PLUGIN_AGENT_ALLOWED_SUBDIRS

    def test_symlink_into_grader_tree_does_not_materialize_content(self, tmp_path):
        """Security regression: a symlink under an allowed subdir that points OUT of
        the bundle (into the grader tree) must be copied as a VERBATIM (dangling)
        symlink, never dereferenced — otherwise grader/answer content would land
        inside the agent-readable bundle. Guards the copytree(symlinks=True) choice:
        a flip to symlinks=False would silently re-leak with no other failing test."""
        src = tmp_path / "plugin"
        (src / "skills").mkdir(parents=True)
        (src / "tests").mkdir()
        (src / "tests" / "check_answer.py").write_text("EXPECTED = 'GRADER-SENTINEL-9f'", encoding="utf-8")
        # relative symlink escaping the allowed subdir into the grader tree
        (src / "skills" / "leak").symlink_to(Path("..") / "tests" / "check_answer.py")

        dst = tmp_path / "bundle"
        project_plugin_for_agent(src, dst)

        projected = dst / "skills" / "leak"
        assert projected.is_symlink(), "must be copied as a verbatim symlink, not dereferenced"
        # The grader sentinel must appear NOWHERE as real content under the bundle.
        blob = "".join(
            p.read_text(encoding="utf-8", errors="ignore") for p in dst.rglob("*") if p.is_file() and not p.is_symlink()
        )
        assert "GRADER-SENTINEL-9f" not in blob, "grader content materialized into the agent bundle"


class TestAuthoredEmptyCriteriaGuard:
    """MED-4: an authored task with no gradable criterion must NOT load (it would
    grade vacuously as SUCCESS against nothing). Both field omission and an explicit
    `success_criteria: []` raise at the authored-load path. The in-container staging
    re-parse bypasses the guard via allow_empty_criteria=True (the host holds the
    real criteria and grades after the container exits)."""

    def _valid_task_dict(self, criteria):
        return {
            "task_id": "t",
            "description": "d",
            "initial_prompt": "p",
            "agent": {"type": "claude-code"},
            "success_criteria": criteria,
        }

    def test_authored_explicit_empty_list_raises(self, tmp_path):
        with pytest.raises(ValueError, match="at least one criterion"):
            parse_task_dict(self._valid_task_dict([]), tmp_path)

    def test_authored_omitted_criteria_raises(self, tmp_path):
        # success_criteria is a REQUIRED model field, so omission raises a Pydantic
        # ValidationError (a ValueError subclass) before our guard even runs. Either
        # way, an authored task without criteria never loads.
        raw = self._valid_task_dict([])
        del raw["success_criteria"]  # field omitted entirely
        with pytest.raises(ValueError):
            parse_task_dict(raw, tmp_path)

    def test_load_task_authored_empty_raises(self, tmp_path):
        task_file = tmp_path / "task.yaml"
        task_file.write_text(yaml.safe_dump(self._valid_task_dict([])), encoding="utf-8")
        with pytest.raises(ValueError):
            load_task(task_file)

    def test_container_bypass_parses_empty(self, tmp_path):
        """The container staging re-parse (allow_empty_criteria=True) accepts []."""
        task = parse_task_dict(self._valid_task_dict([]), tmp_path, allow_empty_criteria=True)
        assert task.success_criteria == []

    def test_load_task_container_bypass_parses_empty(self, tmp_path):
        task_file = tmp_path / "task.yaml"
        task_file.write_text(yaml.safe_dump(self._valid_task_dict([])), encoding="utf-8")
        task, _raw = load_task(task_file, allow_empty_criteria=True)
        assert task.success_criteria == []

    def test_authored_with_criteria_still_loads(self, tmp_path):
        raw = self._valid_task_dict([{"type": "file_exists", "description": "c", "path": "app.py"}])
        task = parse_task_dict(raw, tmp_path)
        assert len(task.success_criteria) == 1
