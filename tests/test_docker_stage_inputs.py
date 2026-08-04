"""Tests for what the docker driver puts inside the container.

Platform-neutral on purpose: ``tests/test_docker_runner_mounts.py`` is skipped
wholesale on Windows (its ``~`` / ``$VAR`` expansion and ``:`` mount-spec
assertions are POSIX-shaped), which means the containment behavior asserted here
would never execute on a Windows developer machine. ``_build_argv`` and
``_stage_inputs`` are pure — argv formatting and file I/O — so they run anywhere.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from coder_eval.isolation.docker_runner import DockerRunner
from coder_eval.models import (
    AgentKind,
    ConfigLineageEntry,
    EvaluationResult,
    FileExistsCriterion,
    FinalStatus,
    PreservationMode,
    SandboxConfig,
    TaskConfigRecord,
    TaskDefinition,
)


def _task(**kwargs) -> TaskDefinition:
    base = {
        "task_id": "test",
        "description": "test task",
        "initial_prompt": "test",
        "sandbox": SandboxConfig(),
        "success_criteria": [FileExistsCriterion(description="c", path="t.txt")],
    }
    base.update(kwargs)
    return TaskDefinition(**base)


def _runner(task: TaskDefinition, *, task_file: Path | None = None, source_yaml: str = "") -> DockerRunner:
    rt = MagicMock()
    rt.task = task
    rt.run_dir = Path(tempfile.gettempdir()) / "test_run"
    rt.task_file = task_file
    rt.variant_id = "default"
    rt.replicate_index = 0
    rt.config_lineage = {}
    rt.source_yaml = source_yaml
    return DockerRunner(rt)


def _mounts(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "-v" and i + 1 < len(argv)]


class TestAutoMountNarrowsFilesToFiles:
    """A single declared file must not drag its whole directory into the container.

    ``reference.file`` lives beside the rest of a scenario: its RESOLUTION.md, its
    ``check_*.py`` graders, its fixtures. Mounting the parent directory to satisfy
    a request for one file hands the agent all of it.
    """

    def test_reference_file_is_mounted_as_a_file(self, tmp_path):
        scenario = tmp_path / "scenario"
        scenario.mkdir()
        reference = scenario / "solution.py"
        reference.write_text("print('answer')\n", encoding="utf-8")
        (scenario / "RESOLUTION.md").write_text("the root cause is X\n", encoding="utf-8")

        runner = _runner(_task(reference={"file": str(reference)}))
        argv = runner._build_argv(tmp_path / "in", tmp_path / "out", container_name="c")
        mounts = _mounts(argv)

        assert f"{reference.resolve()}:{reference.resolve()}:ro" in mounts
        assert f"{scenario.resolve()}:{scenario.resolve()}:ro" not in mounts

    def test_the_sibling_answer_key_is_not_reachable_through_the_mount(self, tmp_path):
        scenario = tmp_path / "scenario"
        scenario.mkdir()
        reference = scenario / "solution.py"
        reference.write_text("x\n", encoding="utf-8")

        runner = _runner(_task(reference={"file": str(reference)}))
        mounts = _mounts(runner._build_argv(tmp_path / "in", tmp_path / "out", container_name="c"))

        # No mount source is an ancestor of the scenario dir, so nothing else in it
        # is exposed by this reference.
        for mount in mounts:
            source = mount.rsplit(":", 1)[0].rsplit(":", 1)[0] if mount.count(":") > 2 else mount.split(":")[0]
            assert Path(source) != scenario.resolve()

    def test_reference_directory_is_still_mounted_as_a_directory(self, tmp_path):
        ref_dir = tmp_path / "_reference"
        ref_dir.mkdir()
        (ref_dir / "a.py").write_text("x\n", encoding="utf-8")

        runner = _runner(_task(reference={"directory": str(ref_dir)}))
        mounts = _mounts(runner._build_argv(tmp_path / "in", tmp_path / "out", container_name="c"))

        assert f"{ref_dir.resolve()}:{ref_dir.resolve()}:ro" in mounts

    def test_system_prompt_file_is_mounted_as_a_file(self, tmp_path):
        prompt = tmp_path / "prompts" / "system.md"
        prompt.parent.mkdir()
        prompt.write_text("be helpful\n", encoding="utf-8")
        (prompt.parent / "secret.md").write_text("nope\n", encoding="utf-8")

        runner = _runner(_task(agent={"type": "claude-code", "system_prompt_file": str(prompt)}))
        mounts = _mounts(runner._build_argv(tmp_path / "in", tmp_path / "out", container_name="c"))

        assert f"{prompt.resolve()}:{prompt.resolve()}:ro" in mounts
        assert f"{prompt.parent.resolve()}:{prompt.parent.resolve()}:ro" not in mounts

    def test_a_missing_file_is_not_mounted(self, tmp_path):
        runner = _runner(_task(reference={"file": str(tmp_path / "absent.py")}))
        mounts = _mounts(runner._build_argv(tmp_path / "in", tmp_path / "out", container_name="c"))
        assert not any("absent.py" in m for m in mounts)

    def test_plugin_directories_are_still_mounted(self, tmp_path):
        plugin = tmp_path / "skills-repo"
        plugin.mkdir()

        runner = _runner(_task(agent={"type": "claude-code", "plugins": [{"type": "local", "path": str(plugin)}]}))
        mounts = _mounts(runner._build_argv(tmp_path / "in", tmp_path / "out", container_name="c"))

        assert f"{plugin.resolve()}:{plugin.resolve()}:ro" in mounts

    def test_a_file_is_not_mounted_twice(self, tmp_path):
        reference = tmp_path / "solution.py"
        reference.write_text("x\n", encoding="utf-8")

        runner = _runner(
            _task(
                reference={"file": str(reference)},
                agent={"type": "claude-code", "system_prompt_file": str(reference)},
            )
        )
        mounts = _mounts(runner._build_argv(tmp_path / "in", tmp_path / "out", container_name="c"))

        spec = f"{reference.resolve()}:{reference.resolve()}:ro"
        assert mounts.count(spec) == 1


class TestStageInputs:
    """What lands in the host-side staging dir that becomes ``/work/input``."""

    def _stage(self, runner: DockerRunner, input_dir: Path) -> None:
        input_dir.mkdir(parents=True, exist_ok=True)
        asyncio.run(runner._stage_inputs(input_dir))

    def test_task_yaml_carries_the_post_override_definition(self, tmp_path):
        runner = _runner(_task(task_id="after-overrides"))
        self._stage(runner, tmp_path / "in")

        staged = yaml.safe_load((tmp_path / "in" / "task.yaml").read_text(encoding="utf-8"))
        assert staged["task_id"] == "after-overrides"

    def test_context_json_carries_the_run_context(self, tmp_path):
        runner = _runner(_task())
        runner.rt.variant_id = "arm-b"
        runner.rt.replicate_index = 2
        runner.rt.config_lineage = {"agent.model": ConfigLineageEntry(value="m", source="task")}
        runner.preservation_mode = PreservationMode.DIRECT_WRITE
        self._stage(runner, tmp_path / "in")

        context = json.loads((tmp_path / "in" / "context.json").read_text(encoding="utf-8"))
        assert context["variant_id"] == "arm-b"
        assert context["replicate_index"] == 2
        assert context["preservation_mode"] == "DIRECT_WRITE"
        assert "agent.model" in context["config_lineage"]

    @pytest.mark.parametrize("key", ["variant_id", "replicate_index", "config_lineage", "preservation_mode"])
    def test_context_keys_the_container_reads_are_present(self, tmp_path, key):
        runner = _runner(_task())
        self._stage(runner, tmp_path / "in")
        context = json.loads((tmp_path / "in" / "context.json").read_text(encoding="utf-8"))
        assert key in context

    def test_source_yaml_is_not_staged_into_the_container(self, tmp_path):
        """The raw task YAML is a second verbatim copy of the answer key, read by nothing.

        It only ever fed ``task.json.task_config.source_yaml`` -- an audit field the
        HOST fills in from its own copy once the container returns. Staging it put
        the full success_criteria text inside the sandbox for no functional gain.
        """
        raw = "task_id: test\nsuccess_criteria:\n  - type: file_exists\n    path: the-answer.txt\n"
        runner = _runner(_task(), source_yaml=raw)
        self._stage(runner, tmp_path / "in")

        context = json.loads((tmp_path / "in" / "context.json").read_text(encoding="utf-8"))
        assert "source_yaml" not in context
        assert b"the-answer.txt" not in (tmp_path / "in" / "context.json").read_bytes()


class TestRestoreSourceYaml:
    """The audit field must survive not being staged.

    Dropping ``source_yaml`` from the staged context is only safe if the host puts
    its own copy back, otherwise ``task.json.task_config.source_yaml`` silently
    becomes the post-override dump instead of the raw on-disk text.
    """

    RAW = "task_id: test\ndescription: from disk\n"

    def _result(self, staged_yaml: str) -> EvaluationResult:
        return EvaluationResult(
            task_id="test",
            task_description="d",
            agent_type=AgentKind.CLAUDE_CODE,
            started_at=datetime.now(),
            final_status=FinalStatus.SUCCESS,
            iteration_count=1,
            task_config=TaskConfigRecord(resolved={}, source_yaml=staged_yaml),
        )

    def test_host_raw_yaml_replaces_the_staged_dump(self, tmp_path):
        runner = _runner(_task(), source_yaml=self.RAW)
        result = self._result("task_id: test\ndescription: post-override dump\n")
        task_json = tmp_path / "task.json"
        task_json.write_text(result.model_dump_json(), encoding="utf-8")

        asyncio.run(runner._restore_source_yaml(result, task_json))

        assert result.task_config is not None
        assert result.task_config.source_yaml == self.RAW

    def test_the_artifact_on_disk_is_rewritten_too(self, tmp_path):
        """task.json is what downstream consumers read, not the in-memory record."""
        runner = _runner(_task(), source_yaml=self.RAW)
        result = self._result("task_id: test\ndescription: post-override dump\n")
        task_json = tmp_path / "task.json"
        task_json.write_text(result.model_dump_json(), encoding="utf-8")

        asyncio.run(runner._restore_source_yaml(result, task_json))

        reloaded = EvaluationResult.model_validate_json(task_json.read_text(encoding="utf-8"))
        assert reloaded.task_config is not None
        assert reloaded.task_config.source_yaml == self.RAW

    def test_no_rewrite_when_the_host_has_no_source_yaml(self, tmp_path):
        runner = _runner(_task(), source_yaml="")
        result = self._result("staged")
        task_json = tmp_path / "task.json"
        task_json.write_text("{}", encoding="utf-8")

        asyncio.run(runner._restore_source_yaml(result, task_json))

        assert result.task_config is not None
        assert result.task_config.source_yaml == "staged"
        assert task_json.read_text(encoding="utf-8") == "{}"

    def test_no_rewrite_when_already_equal(self, tmp_path):
        runner = _runner(_task(), source_yaml=self.RAW)
        result = self._result(self.RAW)
        task_json = tmp_path / "task.json"
        task_json.write_text("{}", encoding="utf-8")

        asyncio.run(runner._restore_source_yaml(result, task_json))

        assert task_json.read_text(encoding="utf-8") == "{}"

    def test_an_unwritable_artifact_does_not_fail_the_task(self, tmp_path):
        runner = _runner(_task(), source_yaml=self.RAW)
        result = self._result("staged")

        asyncio.run(runner._restore_source_yaml(result, tmp_path / "missing-dir" / "task.json"))

        assert result.task_config is not None
        assert result.task_config.source_yaml == self.RAW
