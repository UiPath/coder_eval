"""``coder_eval.harbor.packager`` — the C2 export packager.

Builds real task YAML files on disk (via ``load_task``, exactly the path a
user's ``coder-eval export`` invocation takes) rather than constructing
``TaskDefinition`` objects directly, so these tests exercise the same load
path C2 actually runs through.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

from coder_eval.harbor.packager import (
    DEFAULT_WORKDIR,
    CriteriaNotExportableError,
    TaskNotExportableError,
    export_task,
)
from coder_eval.models import TaskDefinition


_BASE_TASK: dict[str, object] = {
    "task_id": "greet",
    "description": "Write a greeting to greeting.txt.",
    "tags": ["smoke"],
    "agent": {"type": "claude-code"},
    "initial_prompt": "Write 'hello' to greeting.txt.",
    "sandbox": {
        "driver": "docker",
        "docker": {"image": "byod-custom-image:0.1.0", "network": "none"},
        "limits": {"max_memory_mb": 2048, "max_cpus": 2},
    },
    "success_criteria": [
        {"type": "file_exists", "path": "greeting.txt", "description": "exists"},
        {"type": "file_contains", "path": "greeting.txt", "includes": ["hello"], "description": "content"},
    ],
}


def _write_task(tmp_path: Path, overrides: dict[str, object] | None = None, name: str = "task.yaml") -> Path:
    payload = {**_BASE_TASK, **(overrides or {})}
    task_file = tmp_path / name
    task_file.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return task_file


class TestStructuralRefusals:
    def test_tempdir_driver_is_refused(self, tmp_path: Path) -> None:
        task_file = _write_task(tmp_path, {"sandbox": {"driver": "tempdir"}})
        with pytest.raises(TaskNotExportableError):
            export_task(task_file, tmp_path / "out")

    def test_unsupported_criteria_are_refused_before_any_file_is_written(self, tmp_path: Path) -> None:
        task_file = _write_task(
            tmp_path,
            {
                "success_criteria": [
                    {"type": "skill_triggered", "expected_skill": "s", "skill_name": "s", "description": "d"},
                ]
            },
        )
        out_dir = tmp_path / "out"
        with pytest.raises(CriteriaNotExportableError):
            export_task(task_file, out_dir)
        assert not out_dir.exists(), "a refused export must not leave a partial directory behind"

    def test_credentials_criteria_can_be_allowed_explicitly(self, tmp_path: Path) -> None:
        task_file = _write_task(
            tmp_path,
            {"success_criteria": [{"type": "llm_judge", "prompt": "grade it", "description": "d"}]},
        )
        result = export_task(task_file, tmp_path / "out", allow_credentials=True)
        assert result.out_dir.exists()


class TestEmittedDirectoryStructure:
    def test_full_export_produces_the_documented_layout(self, tmp_path: Path) -> None:
        task_file = _write_task(tmp_path)
        out_dir = tmp_path / "out"

        result = export_task(task_file, out_dir)

        assert result.out_dir == out_dir
        assert (out_dir / "task.toml").is_file()
        assert (out_dir / "instruction.md").is_file()
        assert (out_dir / "environment").is_dir()
        assert (out_dir / "tests" / "test.sh").is_file()
        assert (out_dir / "tests" / "task.yaml").is_file()

    def test_instruction_md_carries_the_initial_prompt(self, tmp_path: Path) -> None:
        task_file = _write_task(tmp_path)
        out_dir = tmp_path / "out"
        export_task(task_file, out_dir)
        assert (out_dir / "instruction.md").read_text(encoding="utf-8") == "Write 'hello' to greeting.txt.\n"

    def test_test_sh_is_executable_and_references_the_resolved_workdir(self, tmp_path: Path) -> None:
        task_file = _write_task(tmp_path)
        out_dir = tmp_path / "out"

        result = export_task(task_file, out_dir)

        test_sh = out_dir / "tests" / "test.sh"
        assert test_sh.stat().st_mode & 0o111, "test.sh must be executable"
        content = test_sh.read_text(encoding="utf-8")
        assert f'coder-eval evaluate /tests/task.yaml "{result.workdir}"' in content
        assert "coder-eval harbor reward /logs/verifier --out /logs/verifier/reward.json" in content

    def test_task_toml_parses_and_carries_the_mapped_fields(self, tmp_path: Path) -> None:
        task_file = _write_task(tmp_path)
        out_dir = tmp_path / "out"

        export_task(task_file, out_dir)

        doc = tomllib.loads((out_dir / "task.toml").read_text(encoding="utf-8"))
        assert doc["task"]["name"] == "coder-eval/greet"
        assert doc["task"]["keywords"] == ["smoke"]
        assert doc["environment"]["docker_image"] == "byod-custom-image:0.1.0"
        assert doc["environment"]["memory_mb"] == 2048
        assert doc["environment"]["cpus"] == 2
        assert doc["environment"]["network_mode"] == "no-network"
        assert doc["environment"]["workdir"] == DEFAULT_WORKDIR

    def test_network_bridge_maps_to_public(self, tmp_path: Path) -> None:
        task_file = _write_task(tmp_path, {"sandbox": {"driver": "docker", "docker": {"network": "bridge"}}})
        out_dir = tmp_path / "out"
        export_task(task_file, out_dir)
        doc = tomllib.loads((out_dir / "task.toml").read_text(encoding="utf-8"))
        assert doc["environment"]["network_mode"] == "public"


class TestVerifierTaskYaml:
    """tests/task.yaml must never set agent: {type: none} — see packager.py's module docstring."""

    def test_never_sets_agent_type_none(self, tmp_path: Path) -> None:
        task_file = _write_task(tmp_path)
        out_dir = tmp_path / "out"
        export_task(task_file, out_dir)

        emitted = yaml.safe_load((out_dir / "tests" / "task.yaml").read_text(encoding="utf-8"))
        assert emitted["agent"]["type"] != "none"

    def test_reloads_as_a_valid_task_definition(self, tmp_path: Path) -> None:
        task_file = _write_task(tmp_path)
        out_dir = tmp_path / "out"
        export_task(task_file, out_dir)

        emitted = yaml.safe_load((out_dir / "tests" / "task.yaml").read_text(encoding="utf-8"))
        reloaded = TaskDefinition.model_validate(emitted)
        assert reloaded.task_id == "greet"
        assert len(reloaded.success_criteria) == 2

    def test_reference_comparison_survives_the_none_agent_trap(self, tmp_path: Path) -> None:
        """The corrected design: a placeholder real agent type unblocks reference_comparison.

        If tests/task.yaml set agent: {type: none} instead, this would raise at
        TaskDefinition.model_validate — see the module docstring for why.
        """
        task_file = _write_task(
            tmp_path,
            {
                "reference": {"directory": "reference"},
                "success_criteria": [
                    {
                        "type": "reference_comparison",
                        "agent_file": "greeting.txt",
                        "reference_file": "greeting.txt",
                        "description": "matches reference",
                    }
                ],
            },
        )
        (tmp_path / "reference").mkdir()
        (tmp_path / "reference" / "greeting.txt").write_text("hello", encoding="utf-8")
        out_dir = tmp_path / "out"

        export_task(task_file, out_dir)

        emitted = yaml.safe_load((out_dir / "tests" / "task.yaml").read_text(encoding="utf-8"))
        reloaded = TaskDefinition.model_validate(emitted)  # must not raise
        assert reloaded.reference is not None and reloaded.reference.directory == "reference"
        assert (out_dir / "tests" / "reference" / "greeting.txt").read_text(encoding="utf-8") == "hello"


class TestDockerfileWorkdirResolution:
    def test_dockerfile_with_no_workdir_gets_one_appended_and_warned(self, tmp_path: Path) -> None:
        env_dir = tmp_path / "environment"
        env_dir.mkdir()
        (env_dir / "Dockerfile").write_text("FROM ubuntu:24.04\nRUN apt-get update\n", encoding="utf-8")
        task_file = _write_task(
            tmp_path,
            {"sandbox": {"driver": "docker", "docker": {"dockerfile_path": "environment/Dockerfile"}}},
        )
        out_dir = tmp_path / "out"

        result = export_task(task_file, out_dir)

        assert result.workdir == DEFAULT_WORKDIR
        dockerfile_text = (out_dir / "environment" / "Dockerfile").read_text(encoding="utf-8")
        assert f"WORKDIR {DEFAULT_WORKDIR}" in dockerfile_text
        assert any("declared no WORKDIR" in w for w in result.warnings)

    def test_dockerfile_with_an_existing_workdir_is_respected_and_not_touched(self, tmp_path: Path) -> None:
        env_dir = tmp_path / "environment"
        env_dir.mkdir()
        original = "FROM ubuntu:24.04\nWORKDIR /workspace\nRUN apt-get update\n"
        (env_dir / "Dockerfile").write_text(original, encoding="utf-8")
        task_file = _write_task(
            tmp_path,
            {"sandbox": {"driver": "docker", "docker": {"dockerfile_path": "environment/Dockerfile"}}},
        )
        out_dir = tmp_path / "out"

        result = export_task(task_file, out_dir)

        assert result.workdir == "/workspace"
        assert (out_dir / "environment" / "Dockerfile").read_text(encoding="utf-8") == original
        assert not any("declared no WORKDIR" in w for w in result.warnings)
        # test.sh and task.toml must agree with the same resolved workdir.
        assert '"/workspace"' in (out_dir / "tests" / "test.sh").read_text(encoding="utf-8")
        doc = tomllib.loads((out_dir / "task.toml").read_text(encoding="utf-8"))
        assert doc["environment"]["workdir"] == "/workspace"

    def test_docker_working_dir_override_wins_over_the_dockerfile(self, tmp_path: Path) -> None:
        env_dir = tmp_path / "environment"
        env_dir.mkdir()
        (env_dir / "Dockerfile").write_text("FROM ubuntu:24.04\nWORKDIR /workspace\n", encoding="utf-8")
        task_file = _write_task(
            tmp_path,
            {
                "sandbox": {
                    "driver": "docker",
                    "docker": {"dockerfile_path": "environment/Dockerfile", "working_dir": "/custom"},
                }
            },
        )
        out_dir = tmp_path / "out"

        result = export_task(task_file, out_dir)

        assert result.workdir == "/custom"


class TestCoderEvalAgentBaseImageWarning:
    """v1 assumes the exported image already has coder-eval installed (`FROM coder-eval-agent:<tag>`)."""

    def test_warns_when_dockerfile_does_not_from_coder_eval_agent(self, tmp_path: Path) -> None:
        env_dir = tmp_path / "environment"
        env_dir.mkdir()
        (env_dir / "Dockerfile").write_text("FROM ubuntu:24.04\n", encoding="utf-8")
        task_file = _write_task(
            tmp_path,
            {"sandbox": {"driver": "docker", "docker": {"dockerfile_path": "environment/Dockerfile"}}},
        )

        result = export_task(task_file, tmp_path / "out")

        assert any("coder-eval-agent" in w for w in result.warnings)

    def test_no_warning_when_dockerfile_froms_coder_eval_agent(self, tmp_path: Path) -> None:
        env_dir = tmp_path / "environment"
        env_dir.mkdir()
        (env_dir / "Dockerfile").write_text("FROM coder-eval-agent:latest\n", encoding="utf-8")
        task_file = _write_task(
            tmp_path,
            {"sandbox": {"driver": "docker", "docker": {"dockerfile_path": "environment/Dockerfile"}}},
        )

        result = export_task(task_file, tmp_path / "out")

        assert not any("coder-eval-agent" in w for w in result.warnings)

    def test_warns_when_prebuilt_image_does_not_name_coder_eval_agent(self, tmp_path: Path) -> None:
        task_file = _write_task(tmp_path)  # _BASE_TASK's image is byod-custom-image:0.1.0
        result = export_task(task_file, tmp_path / "out")
        assert any("coder-eval-agent" in w for w in result.warnings)

    def test_no_warning_when_prebuilt_image_names_coder_eval_agent(self, tmp_path: Path) -> None:
        task_file = _write_task(
            tmp_path, {"sandbox": {"driver": "docker", "docker": {"image": "coder-eval-agent:0.12.0"}}}
        )
        result = export_task(task_file, tmp_path / "out")
        assert not any("coder-eval-agent" in w for w in result.warnings)


class TestPrePostRunWarnings:
    def test_pre_run_and_post_run_are_warned_not_silently_dropped(self, tmp_path: Path) -> None:
        task_file = _write_task(
            tmp_path,
            {
                "pre_run": [{"command": "echo setup"}],
                "post_run": [{"command": "echo cleanup"}],
            },
        )
        out_dir = tmp_path / "out"

        result = export_task(task_file, out_dir)

        assert any("pre_run" in w for w in result.warnings)
        assert any("post_run" in w for w in result.warnings)

    def test_no_pre_or_post_run_produces_no_such_warnings(self, tmp_path: Path) -> None:
        task_file = _write_task(tmp_path)
        result = export_task(task_file, tmp_path / "out")
        assert not any("pre_run" in w or "post_run" in w for w in result.warnings)
