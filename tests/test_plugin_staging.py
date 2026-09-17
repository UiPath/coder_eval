"""``stage_plugins``: every authored plugin layout becomes one canonical staged root."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coder_eval.models import AgentKind, FileExistsCriterion, SandboxConfig, TaskDefinition, parse_agent_config
from coder_eval.orchestration.plugin_staging import (
    PluginStagingError,
    link_or_copy,
    scan_plugin_roots,
    scan_plugin_skills,
    stage_plugins,
    staged_plugin_dirs,
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


def _named(root: Path, name: str) -> None:
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")


def _refuse_symlinks(self: Path, *args: object, **kwargs: object) -> None:
    raise OSError("symlinks unavailable")


def _stage(tmp_path: Path, *roots: Path) -> Path:
    return stage_plugins([_local(root) for root in roots], tmp_path / "run" / "plugin_root").root


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

    def test_two_plugins_with_one_name_are_ambiguous(self, tmp_path: Path) -> None:
        _skill(tmp_path / "a" / ".claude" / "skills", "alpha")
        _skill(tmp_path / "b" / ".claude" / "skills", "beta")
        one, two = (tmp_path / "a" / ".claude").resolve(), (tmp_path / "b" / ".claude").resolve()
        with pytest.raises(PluginStagingError, match=r"ambiguous plugin name '\.claude'") as exc:
            scan_plugin_roots([_local(one), _local(two)])
        assert str(one) in str(exc.value)
        assert str(two) in str(exc.value)
        assert ".claude-plugin/plugin.json" in str(exc.value)

    def test_the_same_plugin_root_listed_twice_is_not_ambiguous(self, tmp_path: Path) -> None:
        _skill(tmp_path / "plugin" / "skills", "alpha")
        assert list(scan_plugin_roots([_local(tmp_path / "plugin"), _local(tmp_path / "plugin")])) == ["plugin"]

    def test_two_manifests_with_one_name_are_ambiguous(self, tmp_path: Path) -> None:
        for directory, skill in (("one", "alpha"), ("two", "beta")):
            _skill(tmp_path / directory / "skills", skill)
            _named(tmp_path / directory, "shared")
        with pytest.raises(PluginStagingError, match="ambiguous plugin name 'shared'"):
            scan_plugin_roots([_local(tmp_path / "one"), _local(tmp_path / "two")])

    def test_a_declared_skills_path_outside_the_root_is_refused(self, tmp_path: Path) -> None:
        _skill(tmp_path / "shared", "alpha")
        (tmp_path / "plugin").mkdir()
        _manifest(tmp_path / "plugin", ["../shared"])
        with pytest.raises(PluginStagingError, match=r"manifest skills path '\.\./shared' leaves the plugin root"):
            scan_plugin_skills([_local(tmp_path / "plugin")])

    @pytest.mark.parametrize("name", ["a/b", "..", ".", "a\\b"])
    def test_a_manifest_name_that_is_not_one_path_segment_is_refused(self, tmp_path: Path, name: str) -> None:
        _skill(tmp_path / "plugin" / "skills", "alpha")
        _named(tmp_path / "plugin", name)
        with pytest.raises(PluginStagingError, match="is not one path segment"):
            scan_plugin_roots([_local(tmp_path / "plugin")])

    def test_plugin_names_that_differ_only_in_case_are_ambiguous(self, tmp_path: Path) -> None:
        """On a case-folding filesystem the second link would land inside the first plugin's source."""
        _skill(tmp_path / "Foo" / "skills", "alpha")
        _skill(tmp_path / "other" / "foo" / "skills", "beta")
        with pytest.raises(PluginStagingError, match="ambiguous plugin name 'foo'"):
            scan_plugin_roots([_local(tmp_path / "Foo"), _local(tmp_path / "other" / "foo")])

    def test_skill_names_that_differ_only_in_case_are_ambiguous(self, tmp_path: Path) -> None:
        _skill(tmp_path / "one" / "skills", "Alpha")
        _skill(tmp_path / "two" / "skills", "alpha")
        with pytest.raises(PluginStagingError, match="ambiguous skill name 'alpha'"):
            scan_plugin_skills([_local(tmp_path / "one"), _local(tmp_path / "two")])

    @pytest.mark.parametrize("name", ["../../escaped", "/abs/path", "a/b", ".."])
    def test_a_skill_name_that_is_not_one_path_segment_is_refused(self, tmp_path: Path, name: str) -> None:
        _skill(tmp_path / "plugin" / "skills", "alpha", frontmatter_name=f'"{name}"')
        with pytest.raises(PluginStagingError, match=r"skill name .* is not one path segment"):
            scan_plugin_skills([_local(tmp_path / "plugin")])

    def test_a_manifest_name_holding_a_nul_is_refused(self, tmp_path: Path) -> None:
        _skill(tmp_path / "plugin" / "skills", "alpha")
        _named(tmp_path / "plugin", "a\x00b")
        with pytest.raises(PluginStagingError, match="is not one path segment"):
            scan_plugin_roots([_local(tmp_path / "plugin")])

    def test_an_empty_manifest_name_falls_back_to_the_directory_name(self, tmp_path: Path) -> None:
        _skill(tmp_path / "plugin" / "skills", "alpha")
        _named(tmp_path / "plugin", "")
        assert list(scan_plugin_roots([_local(tmp_path / "plugin")])) == ["plugin"]

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

    def test_an_ambiguous_plugin_name_fails_resolution(self, tmp_path: Path) -> None:
        _skill(tmp_path / "a" / "plugin" / "skills", "alpha")
        _skill(tmp_path / "b" / "plugin" / "skills", "beta")
        with pytest.raises(PluginStagingError, match="ambiguous plugin name 'plugin'"):
            validate_plugins(self._task([_local(tmp_path / "a" / "plugin"), _local(tmp_path / "b" / "plugin")]))

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
    def test_the_staged_root_has_no_merged_manifest(self, tmp_path: Path) -> None:
        _skill(tmp_path / "plugin" / "skills", "alpha")
        assert not (_stage(tmp_path, tmp_path / "plugin") / ".claude-plugin").exists()

    def test_a_plugin_root_is_linked_whole(self, tmp_path: Path) -> None:
        root = tmp_path / "plugin"
        _skill(root / "skills", "alpha")
        _named(root, "p")
        files = ["agents/a.md", "commands/c.md", "hooks/hooks.json", ".mcp.json", "scripts/x.sh"]
        for relative in files:
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text("x", encoding="utf-8")
        link = _stage(tmp_path, root) / "plugins" / "p"
        assert link.is_symlink()
        assert link.resolve() == root.resolve()
        for relative in [*files, "skills/alpha/SKILL.md", ".claude-plugin/plugin.json"]:
            assert (link / relative).is_file(), relative

    def test_the_plugin_name_is_the_manifest_name_else_the_directory_name(self, tmp_path: Path) -> None:
        _skill(tmp_path / "renamed-dir" / "skills", "baz")
        _named(tmp_path / "renamed-dir", "realname")
        _skill(tmp_path / "nomanifest" / "skills", "bar")
        staged = _stage(tmp_path, tmp_path / "renamed-dir", tmp_path / "nomanifest")
        assert sorted(p.name for p in (staged / "plugins").iterdir()) == ["nomanifest", "realname"]

    def test_a_single_skill_root_is_linked_whole(self, tmp_path: Path) -> None:
        root = tmp_path / "solo"
        root.mkdir()
        (root / "SKILL.md").write_text("---\nname: solo\ndescription: d\n---\n", encoding="utf-8")
        link = _stage(tmp_path, root) / "plugins" / "solo"
        assert link.is_symlink()
        assert link.resolve() == root.resolve()

    def test_a_manifest_plugin_with_skills_only_under_the_root_is_wrapped_under_its_manifest_name(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "mroot"
        _skill(root, "zed")
        _named(root, "manifroot")
        wrapper = _stage(tmp_path, root) / "plugins" / "manifroot"
        assert not wrapper.is_symlink()
        assert (wrapper / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8") == '{"name": "manifroot"}'
        assert (wrapper / "skills").resolve() == root.resolve()

    def test_a_bare_skills_directory_is_wrapped_with_a_name_only_manifest(self, tmp_path: Path) -> None:
        """A manifest `skills` key made the Claude CLI load no skill at all (spike, 2026-09-17)."""
        bare = tmp_path / "bare"
        _skill(bare, "foo")
        wrapper = _stage(tmp_path, bare) / "plugins" / "bare"
        assert (wrapper / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8") == '{"name": "bare"}'
        assert (wrapper / "skills").is_symlink()
        assert (wrapper / "skills").resolve() == bare.resolve()

    def test_a_root_with_no_skills_is_linked_whole_not_wrapped(self, tmp_path: Path) -> None:
        agents_only = tmp_path / "agentsonly"
        (agents_only / "agents").mkdir(parents=True)
        (agents_only / "agents" / "a.md").write_text("x", encoding="utf-8")
        _skill(tmp_path / "plugin" / "skills", "alpha")
        link = _stage(tmp_path, agents_only, tmp_path / "plugin") / "plugins" / "agentsonly"
        assert link.is_symlink()
        assert link.resolve() == agents_only.resolve()

    def test_an_unreadable_manifest_names_the_plugin_by_its_directory(self, tmp_path: Path) -> None:
        root = tmp_path / "broken"
        _skill(root / "skills", "alpha")
        (root / ".claude-plugin").mkdir()
        (root / ".claude-plugin" / "plugin.json").write_text("{", encoding="utf-8")
        assert [p.name for p in (_stage(tmp_path, root) / "plugins").iterdir()] == ["broken"]

    def test_the_copy_fallback_does_not_copy_the_run_dir_into_itself(self, tmp_path: Path, monkeypatch) -> None:
        root = tmp_path / "project"
        _skill(root / "skills", "alpha")
        monkeypatch.setattr(Path, "symlink_to", _refuse_symlinks)
        staged = stage_plugins([_local(root)], root / "run" / "plugin_root")
        copied = staged.root / "plugins" / "project"
        assert (copied / "skills" / "alpha" / "SKILL.md").is_file()
        assert not (copied / "run").exists()

    def test_the_copy_fallback_follows_links_and_skips_a_link_loop(self, tmp_path: Path, monkeypatch) -> None:
        """The fallback runs where no symlink can be made, so the copy must not make one either."""
        import os

        root = tmp_path / "plugin"
        _skill(root / "skills", "alpha")
        _skill(tmp_path / "outside", "shared")
        (root / "loop").symlink_to(root, target_is_directory=True)
        (root / "linked").symlink_to(tmp_path / "outside", target_is_directory=True)
        monkeypatch.setattr(Path, "symlink_to", _refuse_symlinks)
        monkeypatch.setattr(os, "symlink", _refuse_symlinks)
        copied = _stage(tmp_path, root) / "plugins" / "plugin"
        assert not (copied / "loop").exists()
        assert not (copied / "linked").is_symlink()
        assert (copied / "linked" / "shared" / "SKILL.md").is_file()
        assert (copied / "skills" / "alpha" / "SKILL.md").is_file()

    def test_the_copy_fallback_wraps_a_bare_skills_directory(self, tmp_path: Path, monkeypatch) -> None:
        _skill(tmp_path / "bare", "foo")
        monkeypatch.setattr(Path, "symlink_to", _refuse_symlinks)
        wrapper = _stage(tmp_path, tmp_path / "bare") / "plugins" / "bare"
        assert (wrapper / ".claude-plugin" / "plugin.json").is_file()
        assert (wrapper / "skills" / "foo" / "SKILL.md").is_file()

    def test_link_or_copy_does_not_copy_into_an_existing_target(self, tmp_path: Path) -> None:
        source = _skill(tmp_path / "skills", "alpha")
        target = tmp_path / "target"
        target.mkdir()
        with pytest.raises(FileExistsError):
            link_or_copy(source, target)
        assert list(target.iterdir()) == []

    def test_staged_plugin_dirs_lists_every_plugin_in_name_order(self, tmp_path: Path) -> None:
        _skill(tmp_path / "zeta" / "skills", "alpha")
        _skill(tmp_path / "bare", "beta")
        staged = _stage(tmp_path, tmp_path / "zeta", tmp_path / "bare")
        assert staged_plugin_dirs(staged) == [staged / "plugins" / "bare", staged / "plugins" / "zeta"]

    def test_staged_plugin_dirs_is_empty_without_a_plugins_dir(self, tmp_path: Path) -> None:
        assert staged_plugin_dirs(tmp_path) == []

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
