"""Tests for ``sandbox.docker.isolated_paths``, the declared agent-invisible set.

The field is a contract rather than a mount instruction: the docker runner proves
that no agent-visible surface (an agent-readable mount, or a sanitized plugin
bundle projection) carries content from a declared path, and fails closed before
a container starts. Private grader mounts are exempt -- keeping a raw source
below the root-only parent is the isolation working as intended.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from coder_eval.isolation.docker_runner import DockerRunError, DockerRunner
from coder_eval.models import (
    AgentKind,
    ClaudeCodeAgentConfig,
    DockerDriverConfig,
    ExperimentDefaults,
    ExperimentDefinition,
    ExperimentVariant,
    FileExistsCriterion,
    SandboxConfig,
    TaskDefinition,
    TemplateDirSource,
)
from coder_eval.orchestration.experiment import resolve_task_for_variant
from coder_eval.orchestration.overrides import apply_overrides


def _write(path: Path, text: str = "data") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _runner(
    tmp_path: Path,
    isolated_paths: list[str],
    *,
    plugin: Path | None = None,
    template_dir: Path | None = None,
) -> DockerRunner:
    template_sources = [TemplateDirSource(path=str(template_dir))] if template_dir is not None else None
    task = TaskDefinition(
        task_id="isolated-paths",
        description="test",
        initial_prompt="work",
        agent=ClaudeCodeAgentConfig(
            type=AgentKind.CLAUDE_CODE,
            plugins=[{"type": "local", "path": str(plugin)}] if plugin is not None else None,
        ),
        sandbox=SandboxConfig(
            driver="docker",
            docker=DockerDriverConfig(agent_isolation=True, isolated_paths=isolated_paths),
            template_sources=template_sources,
        ),
        success_criteria=[FileExistsCriterion(description="done", path="done.txt")],
    )
    rt = MagicMock(task=task, task_file=tmp_path / "task" / "task.yaml", run_dir=tmp_path / "run")
    return DockerRunner(rt)


class TestAgentVisibleMounts:
    def test_declared_path_inside_an_agent_visible_mount_fails(self, tmp_path: Path) -> None:
        bundle = tmp_path / "bundle"
        _write(bundle / "skills" / "demo" / "SKILL.md", "public")
        declared = bundle / "skills"

        runner = _runner(tmp_path, [str(declared)])
        runner._agent_plugin_mounts = [(bundle, "/opt/coder-eval/agent-skills/plugin-0")]

        with pytest.raises(DockerRunError) as excinfo:
            runner._enforce_isolated_paths()

        message = str(excinfo.value)
        assert str(declared) in message
        assert str(bundle) in message
        assert "/opt/coder-eval/agent-skills/plugin-0" in message

    def test_declared_parent_of_an_agent_visible_mount_fails(self, tmp_path: Path) -> None:
        """Containment counts in both directions: the mount sits inside the declaration."""

        bundle = tmp_path / "workspace" / "bundle"
        bundle.mkdir(parents=True)
        runner = _runner(tmp_path, [str(tmp_path / "workspace")])
        runner._agent_plugin_mounts = [(bundle, "/opt/coder-eval/agent-skills/plugin-0")]

        with pytest.raises(DockerRunError, match="overlaps the agent-visible"):
            runner._enforce_isolated_paths()

    def test_agent_home_mount_is_agent_visible(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``~`` expands, and the ~/.claude copy mounted at the agent's HOME is agent-visible."""

        home = tmp_path / "home"
        _write(home / ".claude" / "plugins" / "repo" / "skills" / "demo" / "SKILL.md")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))

        runner = _runner(tmp_path, ["~/.claude/plugins/repo"])
        runner._claude_mount_src = tmp_path / "staging" / "claude-home"

        with pytest.raises(DockerRunError, match="agent home mount"):
            runner._enforce_isolated_paths()

    def test_env_var_in_declaration_is_expanded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        monkeypatch.setenv("ISOLATED_TEST_DIR", str(bundle))

        runner = _runner(tmp_path, ["$ISOLATED_TEST_DIR"])
        runner._agent_plugin_mounts = [(bundle, "/opt/coder-eval/agent-skills/plugin-0")]

        with pytest.raises(DockerRunError, match=r"\$ISOLATED_TEST_DIR"):
            runner._enforce_isolated_paths()

    def test_unrelated_path_passes(self, tmp_path: Path) -> None:
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        runner = _runner(tmp_path, [str(elsewhere)])
        runner._agent_plugin_mounts = [(bundle, "/opt/coder-eval/agent-skills/plugin-0")]

        runner._enforce_isolated_paths()

    def test_missing_declared_path_passes(self, tmp_path: Path) -> None:
        """Nothing on disk means nothing to expose; declarations resolve non-strictly."""

        runner = _runner(tmp_path, [str(tmp_path / "never" / "created")])

        runner._enforce_isolated_paths()


class TestPluginBundleProjection:
    """The motivating shape: a skills checkout whose tests tree must stay private."""

    def _prepared_runner(self, tmp_path: Path, declared: str) -> DockerRunner:
        plugin = tmp_path / "skills-repo"
        _write(plugin / "skills" / "demo" / "SKILL.md", "public")
        _write(plugin / "tests" / "fixtures" / "golden.json", "expected answers")
        runner = _runner(tmp_path, [declared], plugin=plugin, template_dir=plugin / "tests")
        staging = tmp_path / "staging"
        staging.mkdir()
        runner._prepare_isolated_sources(staging)
        return runner

    def test_declared_tests_tree_outside_the_projection_passes(self, tmp_path: Path) -> None:
        runner = self._prepared_runner(tmp_path, str(tmp_path / "skills-repo" / "tests"))

        # The raw checkout is mounted, but only below the root-only grader parent.
        assert any(source == tmp_path / "skills-repo" for source, _ in runner._private_source_mounts)
        # A projection exists, so the declaration is checked against real entries.
        assert [relative for _, manifest in runner._plugin_bundle_manifests for relative in manifest.files]
        runner._enforce_isolated_paths()

    def test_declared_path_the_bundle_projects_fails(self, tmp_path: Path) -> None:
        runner = self._prepared_runner(tmp_path, str(tmp_path / "skills-repo" / "skills"))

        with pytest.raises(DockerRunError) as excinfo:
            runner._enforce_isolated_paths()

        message = str(excinfo.value)
        assert str(tmp_path / "skills-repo" / "skills") in message
        assert "skills/demo/SKILL.md" in message


class TestConfigValidation:
    def test_isolated_paths_requires_docker_driver(self) -> None:
        with pytest.raises(ValidationError, match="requires driver: docker"):
            SandboxConfig(driver="tempdir", docker=DockerDriverConfig(isolated_paths=["/some/dir"]))

    def test_isolated_paths_requires_agent_isolation(self) -> None:
        with pytest.raises(ValidationError, match=re.escape("requires docker.agent_isolation")):
            SandboxConfig(
                driver="docker",
                docker=DockerDriverConfig(agent_isolation=False, isolated_paths=["/some/dir"]),
            )

    def test_empty_declaration_is_unconstrained(self) -> None:
        SandboxConfig(driver="tempdir", docker=DockerDriverConfig(agent_isolation=False))


class TestConfigMerge:
    """The declaration must survive the layered resolver and the CLI override path."""

    def _task(self) -> TaskDefinition:
        return TaskDefinition(
            task_id="t",
            description="x",
            initial_prompt="hi",
            agent=ClaudeCodeAgentConfig(type=AgentKind.CLAUDE_CODE),
            sandbox=SandboxConfig(driver="docker"),
            success_criteria=[FileExistsCriterion(description="done", path="done.txt")],
        )

    def test_experiment_defaults_layer_and_cli_override_agree(self) -> None:
        base = ExperimentDefinition(
            experiment_id="default",
            variants=[ExperimentVariant(variant_id="default")],
        )
        experiment = ExperimentDefinition(
            experiment_id="test",
            defaults=ExperimentDefaults(
                sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig(isolated_paths=["/repo/tests"]))
            ),
            variants=[ExperimentVariant(variant_id="v")],
        )
        layered, _, _ = resolve_task_for_variant(base, self._task(), experiment, experiment.variants[0])

        overridden = self._task()
        apply_overrides(overridden, {"sandbox.docker.isolated_paths": ["/repo/tests"]})

        assert layered.sandbox.docker.isolated_paths == ["/repo/tests"]
        assert layered.sandbox.docker.model_dump() == overridden.sandbox.docker.model_dump()
