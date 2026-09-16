"""``stage_plugins``: every authored plugin layout becomes one canonical staged root."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coder_eval.models import AgentKind, FileExistsCriterion, SandboxConfig, TaskDefinition, parse_agent_config
from coder_eval.orchestration.plugin_staging import (
    PluginStagingError,
    link_or_copy,
    scan_plugin_skills,
    stage_plugins,
    validate_plugins,
)


def _skill(parent: Path, name: str, *, frontmatter_name: str | None = None) -> Path:
    skill_dir = parent / name
    skill_dir.mkdir(parents=True)
    declared = frontmatter_name if frontmatter_name is not None else name
    (skill_dir / "SKILL.md").write_text(f"---\nname: {declared}\ndescription: d\n---\nbody\n", encoding="utf-8")
    return skill_dir


def _manifest(root: Path, skills: object) -> None:
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "p", "skills": skills}), encoding="utf-8")


def _local(path: Path | str) -> dict[str, str]:
    return {"type": "local", "path": str(path)}


class TestLayouts:
    def test_a_plugin_root_with_the_default_skills_dir(self, tmp_path: Path) -> None:
        _skill(tmp_path / "plugin" / "skills", "alpha")
        assert set(scan_plugin_skills([_local(tmp_path / "plugin")])) == {"alpha"}

    @pytest.mark.parametrize("declared", [["./custom"], "./custom"])
    def test_manifest_paths_add_to_the_default_skills_dir(self, tmp_path: Path, declared: object) -> None:
        """Claude Code scans the default skills/ AND each declared path (plugins-reference, spike 2026-09-16)."""
        root = tmp_path / "plugin"
        _skill(root / "custom", "beta")
        _skill(root / "skills", "alpha")
        _manifest(root, declared)
        assert set(scan_plugin_skills([_local(root)])) == {"alpha", "beta"}

    def test_a_declared_path_may_name_one_skill(self, tmp_path: Path) -> None:
        root = tmp_path / "plugin"
        _skill(root / "skills", "alpha")
        _skill(root / "custom", "beta")
        _skill(root / "custom", "not-declared")
        _manifest(root, ["./custom/beta"])
        assert set(scan_plugin_skills([_local(root)])) == {"alpha", "beta"}

    def test_a_root_holding_skill_md_is_a_single_skill_plugin(self, tmp_path: Path) -> None:
        root = tmp_path / "solo"
        root.mkdir()
        (root / "SKILL.md").write_text("---\nname: solo-skill\ndescription: d\n---\n", encoding="utf-8")
        _skill(root, "nested-helper")
        assert set(scan_plugin_skills([_local(root)])) == {"solo-skill"}

    def test_the_frontmatter_name_names_the_skill(self, tmp_path: Path) -> None:
        _skill(tmp_path / "plugin" / "skills", "dir-name", frontmatter_name="invoked-name")
        assert set(scan_plugin_skills([_local(tmp_path / "plugin")])) == {"invoked-name"}

    def test_the_directory_name_is_the_fallback(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "plugin" / "skills" / "dir-name"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("no frontmatter here\n", encoding="utf-8")
        assert set(scan_plugin_skills([_local(tmp_path / "plugin")])) == {"dir-name"}

    def test_a_bare_skills_directory(self, tmp_path: Path) -> None:
        _skill(tmp_path / "bare", "alpha")
        assert set(scan_plugin_skills([_local(tmp_path / "bare")])) == {"alpha"}

    def test_a_declared_dir_wins_over_skills_directly_under_the_root(self, tmp_path: Path) -> None:
        root = tmp_path / "plugin"
        _skill(root / "skills", "alpha")
        _skill(root, "stray")
        assert set(scan_plugin_skills([_local(root)])) == {"alpha"}

    def test_two_roots_merge(self, tmp_path: Path) -> None:
        _skill(tmp_path / "one" / "skills", "alpha")
        _skill(tmp_path / "two", "beta")
        assert set(scan_plugin_skills([_local(tmp_path / "one"), _local(tmp_path / "two")])) == {"alpha", "beta"}

    def test_a_relative_path_resolves_against_the_process_cwd(self, tmp_path: Path, monkeypatch) -> None:
        _skill(tmp_path / "plugin" / "skills", "alpha")
        monkeypatch.chdir(tmp_path)
        assert scan_plugin_skills([_local("plugin")])["alpha"] == (tmp_path / "plugin" / "skills" / "alpha").resolve()

    def test_an_env_var_expands(self, tmp_path: Path, monkeypatch) -> None:
        _skill(tmp_path / "plugin" / "skills", "alpha")
        monkeypatch.setenv("PROBE_PLUGIN_ROOT", str(tmp_path / "plugin"))
        assert set(scan_plugin_skills([_local("$PROBE_PLUGIN_ROOT")])) == {"alpha"}


class TestRefusals:
    def test_a_duplicate_skill_name_is_ambiguous(self, tmp_path: Path) -> None:
        _skill(tmp_path / "one" / "skills", "alpha")
        _skill(tmp_path / "two" / "skills", "alpha")
        with pytest.raises(PluginStagingError, match="ambiguous skill name 'alpha'"):
            scan_plugin_skills([_local(tmp_path / "one"), _local(tmp_path / "two")])

    def test_the_same_source_listed_twice_is_not_ambiguous(self, tmp_path: Path) -> None:
        _skill(tmp_path / "one" / "skills", "alpha")
        assert set(scan_plugin_skills([_local(tmp_path / "one"), _local(tmp_path / "one")])) == {"alpha"}

    def test_zero_skills_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "empty" / "skills").mkdir(parents=True)
        with pytest.raises(PluginStagingError, match="offers no skill"):
            scan_plugin_skills([_local(tmp_path / "empty")])

    def test_a_missing_directory_is_refused_with_the_hint(self, tmp_path: Path) -> None:
        with pytest.raises(PluginStagingError, match="path does not exist"):
            scan_plugin_skills([_local(tmp_path / "nowhere")])

    def test_an_unset_env_var_is_refused_with_the_hint(self, monkeypatch) -> None:
        monkeypatch.delenv("PROBE_UNSET_PLUGIN_ROOT", raising=False)
        with pytest.raises(PluginStagingError, match="env var likely unset"):
            scan_plugin_skills([_local("$PROBE_UNSET_PLUGIN_ROOT")])

    def test_the_refusal_is_a_task_resolution_error(self) -> None:
        from coder_eval.orchestration.harness_contract import TaskResolutionError

        assert issubclass(PluginStagingError, TaskResolutionError)


class TestValidatePlugins:
    @staticmethod
    def _task(plugins: list[dict[str, str]] | None) -> TaskDefinition:
        return TaskDefinition(
            task_id="plugins",
            description="d",
            initial_prompt="p",
            agent=parse_agent_config(type=AgentKind.CLAUDE_CODE, plugins=plugins),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(path="x", description="x")],
        )

    @pytest.mark.parametrize("plugins", [None, []])
    def test_no_plugins_is_a_no_op(self, plugins: list[dict[str, str]] | None) -> None:
        validate_plugins(self._task(plugins))

    def test_a_path_with_no_skill_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(PluginStagingError):
            validate_plugins(self._task([_local(tmp_path)]))

    @staticmethod
    def _skill_task(plugins: list[dict[str, str]], skill_name: str) -> TaskDefinition:
        from coder_eval.models import SkillTriggeredCriterion

        return TaskDefinition(
            task_id="plugins",
            description="d",
            initial_prompt="p",
            agent=parse_agent_config(type=AgentKind.CLAUDE_CODE, plugins=plugins),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[
                SkillTriggeredCriterion(
                    type="skill_triggered", description="d", skill_name=skill_name, expected_skill=skill_name
                )
            ],
        )

    def test_a_skill_triggered_target_the_plugins_offer_passes(self, tmp_path: Path) -> None:
        _skill(tmp_path / "plugin" / "skills", "alpha")
        validate_plugins(self._skill_task([_local(tmp_path / "plugin")], "alpha"))

    def test_a_skill_triggered_target_the_plugins_do_not_offer_is_refused_before_the_run(self, tmp_path: Path) -> None:
        _skill(tmp_path / "plugin" / "skills", "alpha")
        with pytest.raises(PluginStagingError, match=r"skill_triggered names skill\(s\) \['absent'\]"):
            validate_plugins(self._skill_task([_local(tmp_path / "plugin")], "absent"))

    def test_an_unexpanded_row_placeholder_is_left_to_its_row(self, tmp_path: Path) -> None:
        _skill(tmp_path / "plugin" / "skills", "alpha")
        validate_plugins(self._skill_task([_local(tmp_path / "plugin")], "${row.skill}"))

    def test_without_plugins_the_target_is_not_checked(self) -> None:
        from coder_eval.models import SkillTriggeredCriterion

        task = self._task(None).model_copy(
            update={
                "success_criteria": [
                    SkillTriggeredCriterion(type="skill_triggered", description="d", skill_name="x", expected_skill="x")
                ]
            }
        )
        validate_plugins(task)


class TestStaging:
    def test_staged_manifest_names_the_plugin_and_declares_no_skills_key(self, tmp_path: Path) -> None:
        """A manifest `skills` key made the Claude CLI load no skill at all (spike, 2026-09-17)."""
        _skill(tmp_path / "plugin" / "skills", "alpha")
        staged = stage_plugins([_local(tmp_path / "plugin")], tmp_path / "run" / "plugin_root")
        manifest = staged.root / ".claude-plugin" / "plugin.json"
        assert manifest.read_text(encoding="utf-8") == '{"name": "coder-eval-plugins"}'

    def test_each_skill_is_a_symlink_to_its_source(self, tmp_path: Path) -> None:
        source = _skill(tmp_path / "plugin" / "skills", "alpha")
        _skill(tmp_path / "bare", "beta")
        staged = stage_plugins(
            [_local(tmp_path / "plugin"), _local(tmp_path / "bare")], tmp_path / "run" / "plugin_root"
        )
        assert staged.skills_offered == ("alpha", "beta")
        link = staged.root / "skills" / "alpha"
        assert link.is_symlink()
        assert link.resolve() == source.resolve()
        assert (link / "SKILL.md").is_file()

    def test_both_authored_layouts_stage_the_same_root(self, tmp_path: Path) -> None:
        _skill(tmp_path / "plugin" / "skills", "alpha")
        _skill(tmp_path / "bare", "alpha")
        one = stage_plugins([_local(tmp_path / "plugin")], tmp_path / "one")
        two = stage_plugins([_local(tmp_path / "bare")], tmp_path / "two")
        assert one.skills_offered == two.skills_offered
        assert sorted(p.name for p in (one.root / "skills").iterdir()) == sorted(
            p.name for p in (two.root / "skills").iterdir()
        )

    def test_the_staged_root_is_absolute_for_a_relative_staging_dir(self, tmp_path: Path, monkeypatch) -> None:
        """Every harness runs with the sandbox as cwd, so a relative root would name nothing there."""
        _skill(tmp_path / "plugin" / "skills", "alpha")
        monkeypatch.chdir(tmp_path)
        staged = stage_plugins([_local("plugin")], Path("runs") / "t" / "plugin_root")
        assert staged.root.is_absolute()
        assert staged.root == (tmp_path / "runs" / "t" / "plugin_root").resolve()

    def test_re_staging_replaces_an_existing_root(self, tmp_path: Path) -> None:
        _skill(tmp_path / "plugin" / "skills", "alpha")
        staging_dir = tmp_path / "run" / "plugin_root"
        (staging_dir / "skills" / "stale").mkdir(parents=True)
        staged = stage_plugins([_local(tmp_path / "plugin")], staging_dir)
        assert sorted(p.name for p in (staged.root / "skills").iterdir()) == ["alpha"]

    def test_link_or_copy_falls_back_to_a_copy(self, tmp_path: Path, monkeypatch) -> None:
        source = _skill(tmp_path / "skills", "alpha")

        def _refuse(self: Path, *args: object, **kwargs: object) -> None:
            raise OSError("symlinks unavailable")

        monkeypatch.setattr(Path, "symlink_to", _refuse)
        target = tmp_path / "target"
        link_or_copy(source, target)
        assert not target.is_symlink()
        assert (target / "SKILL.md").read_text(encoding="utf-8") == (source / "SKILL.md").read_text(encoding="utf-8")
