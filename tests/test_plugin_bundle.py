"""Security tests for manifest-verified agent plugin projections."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from coder_eval.isolation.docker_runner import DockerRunner
from coder_eval.models import (
    CONTAINER_AGENT_SKILLS_DIR,
    CONTAINER_PRIVATE_PLUGIN_DIR,
    AgentKind,
    ClaudeCodeAgentConfig,
    DockerDriverConfig,
    FileExistsCriterion,
    RunCommandCriterion,
    SandboxConfig,
    TaskDefinition,
)
from coder_eval.plugin_bundle import PluginBundleError, build_manifest, stage_bundle, verify_bundle


def _write(path: Path, text: str = "data") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _symlink_or_skip(target: str, link: Path, *, is_dir: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


# Windows stores a relative reparse-point target verbatim and resolves it with
# backslash-only NT parsing, so a POSIX-separated target like "../shared/x.md"
# dangles there. Plugin bundles are only consumed by the POSIX-only DockerRunner.
posix_relative_symlink = pytest.mark.skipif(
    sys.platform == "win32",
    reason="relative POSIX symlink targets do not resolve on Windows; plugin bundles are POSIX-only",
)


def test_bundle_includes_only_plugin_discovery_subtrees(tmp_path: Path) -> None:
    source = tmp_path / "skills-repo"
    _write(source / "skills" / "demo" / "SKILL.md", "public")
    _write(source / "commands" / "run.md", "public command")
    _write(source / "graders" / "secret.py", "answer")
    _write(source / "tests" / "fixtures" / "golden.json", "answer")

    bundle = tmp_path / "bundle"
    manifest = stage_bundle(source, bundle)

    assert (bundle / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8") == "public"
    assert (bundle / "commands" / "run.md").is_file()
    assert not (bundle / "graders").exists()
    assert not (bundle / "tests").exists()
    assert set(manifest.files) == {"commands/run.md", "skills/demo/SKILL.md"}


@pytest.mark.parametrize("name", ["RESOLUTION.md", "check_answer.py", "CHECK_X.PY"])
def test_bundle_fails_closed_on_answer_key_name_inside_allowed_tree(tmp_path: Path, name: str) -> None:
    source = tmp_path / "plugin"
    _write(source / "skills" / "demo" / name, "hidden")

    with pytest.raises(PluginBundleError, match="hidden grading material"):
        build_manifest(source)


def test_bundle_rejects_absolute_symlink_back_to_source(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    secret = _write(source / "skills" / "demo" / "secret.txt")
    link = source / "skills" / "demo" / "alias.txt"
    _symlink_or_skip(str(secret.resolve()), link)

    with pytest.raises(PluginBundleError, match="absolute plugin symlink"):
        build_manifest(source)


@posix_relative_symlink
def test_bundle_rejects_relative_symlink_to_excluded_tree(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    _write(source / "fixtures" / "answer.json", "hidden")
    link = source / "skills" / "answer.json"
    link.parent.mkdir(parents=True)
    _symlink_or_skip("../fixtures/answer.json", link)

    with pytest.raises(PluginBundleError, match="excluded content"):
        build_manifest(source)


@posix_relative_symlink
def test_bundle_preserves_safe_relative_symlink(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    _write(source / "skills" / "shared" / "guide.md", "public")
    link = source / "skills" / "demo" / "guide.md"
    link.parent.mkdir(parents=True)
    _symlink_or_skip("../shared/guide.md", link)

    bundle = tmp_path / "bundle"
    stage_bundle(source, bundle)

    staged_link = bundle / "skills" / "demo" / "guide.md"
    assert staged_link.is_symlink()
    assert os.readlink(staged_link) == "../shared/guide.md"
    assert staged_link.read_text(encoding="utf-8") == "public"


@pytest.mark.parametrize("mutation", ["change", "add", "delete"])
def test_bundle_verification_detects_drift(tmp_path: Path, mutation: str) -> None:
    source = tmp_path / "plugin"
    _write(source / "skills" / "demo" / "SKILL.md", "public")
    bundle = tmp_path / "bundle"
    stage_bundle(source, bundle)

    staged = bundle / "skills" / "demo" / "SKILL.md"
    # stage_bundle hardens the projection (dirs 0o555, files 0o444). Adding or
    # removing an entry needs write permission on the directory, not just the
    # file, so relax both before simulating drift.
    staged.parent.chmod(0o755)
    staged.chmod(0o644)
    if mutation == "change":
        staged.write_text("tampered", encoding="utf-8")
    elif mutation == "add":
        _write(bundle / "skills" / "demo" / "undeclared.txt")
    else:
        staged.unlink()

    with pytest.raises(PluginBundleError, match="differs from its manifest"):
        verify_bundle(bundle)


def test_docker_runner_rewrites_plugin_and_never_mounts_raw_at_original_path(tmp_path: Path) -> None:
    plugin = tmp_path / "skills-repo"
    _write(plugin / "skills" / "demo" / "SKILL.md", "public")
    _write(plugin / "tests" / "fixtures" / "golden.json", "hidden")
    task_dir = tmp_path / "task"
    task_dir.mkdir()

    task = TaskDefinition(
        task_id="isolation",
        description="test",
        initial_prompt="use the demo skill",
        agent=ClaudeCodeAgentConfig(
            type=AgentKind.CLAUDE_CODE,
            plugins=[{"type": "local", "path": str(plugin)}],
        ),
        sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig(agent_isolation=True)),
        success_criteria=[FileExistsCriterion(description="done", path="done.txt")],
    )
    rt = MagicMock()
    rt.task = task
    rt.task_file = task_dir / "task.yaml"
    rt.run_dir = tmp_path / "run"
    runner = DockerRunner(rt)

    staging = tmp_path / "staging"
    staging.mkdir()
    runner._prepare_isolated_sources(staging)
    payload = runner._rewrite_task_paths(task.model_dump(mode="json"))

    agent = payload["agent"]
    assert isinstance(agent, dict)
    plugins = agent["plugins"]
    assert isinstance(plugins, list)
    assert plugins[0]["path"] == f"{CONTAINER_AGENT_SKILLS_DIR}/plugin-0"
    assert str(plugin) not in json.dumps(payload)

    input_dir = staging / "input"
    output_dir = staging / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    argv = runner._build_argv(input_dir, output_dir, container_name="isolation", image="test-image")
    mounts = [argv[index + 1] for index, value in enumerate(argv) if value == "-v"]

    assert "--init" in argv
    pids_index = argv.index("--pids-limit")
    assert argv[pids_index + 1] == "512"
    assert f"{plugin.resolve()}:{CONTAINER_PRIVATE_PLUGIN_DIR}/plugin-0:ro" in mounts
    assert not any(mount.startswith(f"{plugin.resolve()}:{plugin.resolve()}") for mount in mounts)
    assert any(mount.endswith(f":{CONTAINER_AGENT_SKILLS_DIR}/plugin-0:ro") for mount in mounts)


def test_isolation_rejects_dynamic_privileged_criterion(tmp_path: Path) -> None:
    task = TaskDefinition(
        task_id="unsafe-grader",
        description="test",
        initial_prompt="work",
        agent=ClaudeCodeAgentConfig(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(driver="docker", docker=DockerDriverConfig(agent_isolation=True)),
        success_criteria=[RunCommandCriterion(description="unsafe", command="python check.py")],
    )
    rt = MagicMock(task=task, task_file=tmp_path / "task.yaml", run_dir=tmp_path / "run")
    runner = DockerRunner(rt)

    with pytest.raises(RuntimeError, match="dynamic criteria"):
        runner._validate_agent_isolation_compatibility()
