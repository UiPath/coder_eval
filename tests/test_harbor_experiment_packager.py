"""``coder_eval.harbor.experiment_packager`` — task.yaml + experiment.yaml -> Harbor directories.

Builds real task/experiment YAML files on disk and drives them through
``export_experiment`` exactly as `coder-eval export ... -e ...` does, so these
tests exercise the same resolution path (``orchestration.experiment.resolve_all_tasks``)
a real invocation runs through.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coder_eval.harbor import packager
from coder_eval.harbor.experiment_packager import export_experiment


_BASE_TASK: dict[str, object] = {
    "task_id": "greet",
    "description": "Write a greeting to greeting.txt.",
    "agent": {"type": "claude-code"},
    "initial_prompt": "Write 'hello' to greeting.txt.",
    "sandbox": {
        "driver": "docker",
        "docker": {"image": "byod-custom-image:0.1.0", "network": "none"},
    },
    "success_criteria": [
        {"type": "file_exists", "path": "greeting.txt", "description": "exists"},
    ],
}


@pytest.fixture(autouse=True)
def _no_real_docker_inspection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same hermeticity guard as test_harbor_packager.py -- never shell out to real docker."""
    monkeypatch.setattr(packager, "_inspect_image_workdir", lambda image: None)


def _write_task(tmp_path: Path, overrides: dict[str, object] | None = None, name: str = "task.yaml") -> Path:
    payload = {**_BASE_TASK, **(overrides or {})}
    task_file = tmp_path / name
    task_file.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return task_file


def _write_experiment(tmp_path: Path, definition: dict[str, object], name: str = "experiment.yaml") -> Path:
    exp_file = tmp_path / name
    exp_file.write_text(yaml.safe_dump(definition, sort_keys=False), encoding="utf-8")
    return exp_file


class TestRunLimitOnlyVariants:
    """Variants that only touch run_limits/sandbox/prompt -- fully honorable, should export."""

    def test_two_variants_export_two_directories_with_distinct_timeouts(self, tmp_path: Path) -> None:
        task_file = _write_task(tmp_path)
        exp_file = _write_experiment(
            tmp_path,
            {
                "experiment_id": "run-limit-ab",
                "variants": [
                    {"variant_id": "fast", "run_limits": {"task_timeout": 60}},
                    {"variant_id": "slow", "run_limits": {"task_timeout": 900}},
                ],
            },
        )
        out_dir = tmp_path / "out"

        result = export_experiment([task_file], exp_file, out_dir)

        assert len(result.exported) == 2
        assert result.skipped == []
        assert result.load_skipped == []
        fast_dir = out_dir / "fast" / "greet"
        slow_dir = out_dir / "slow" / "greet"
        assert (fast_dir / "task.toml").exists()
        assert (slow_dir / "task.toml").exists()
        assert "timeout_sec = 60" in (fast_dir / "task.toml").read_text(encoding="utf-8")
        assert "timeout_sec = 900" in (slow_dir / "task.toml").read_text(encoding="utf-8")

    def test_variant_prompt_override_lands_in_environment_task_yaml(self, tmp_path: Path) -> None:
        task_file = _write_task(tmp_path)
        exp_file = _write_experiment(
            tmp_path,
            {
                "experiment_id": "prompt-ab",
                "variants": [
                    {"variant_id": "rewritten", "initial_prompt": "Write 'howdy' to greeting.txt."},
                ],
            },
        )
        out_dir = tmp_path / "out"

        result = export_experiment([task_file], exp_file, out_dir)

        assert len(result.exported) == 1
        # instruction.md is a fixed placeholder now -- the real (overridden) prompt
        # lands in environment/task.yaml instead.
        instruction = (out_dir / "rewritten" / "greet" / "instruction.md").read_text(encoding="utf-8")
        assert "howdy" not in instruction
        emitted = yaml.safe_load(
            (out_dir / "rewritten" / "greet" / "environment" / "task.yaml").read_text(encoding="utf-8")
        )
        assert emitted["initial_prompt"] == "Write 'howdy' to greeting.txt."


class TestUnhonorableOverridesAreSkipped:
    """Agent/simulation overrides an exported Harbor directory cannot express."""

    def test_variant_agent_model_override_is_skipped_not_exported(self, tmp_path: Path) -> None:
        task_file = _write_task(tmp_path)
        exp_file = _write_experiment(
            tmp_path,
            {
                "experiment_id": "model-ab",
                "variants": [
                    {"variant_id": "sonnet", "agent": {"model": "claude-sonnet-5"}},
                    {"variant_id": "opus", "agent": {"model": "claude-opus-5"}},
                ],
            },
        )
        out_dir = tmp_path / "out"

        result = export_experiment([task_file], exp_file, out_dir)

        assert result.exported == []
        assert {s.variant_id for s in result.skipped} == {"sonnet", "opus"}
        assert all("agent override" in s.reason for s in result.skipped)

    def test_experiment_defaults_agent_override_is_skipped(self, tmp_path: Path) -> None:
        task_file = _write_task(tmp_path)
        exp_file = _write_experiment(
            tmp_path,
            {
                "experiment_id": "defaults-agent",
                "defaults": {"agent": {"permission_mode": "bypassPermissions"}},
                "variants": [{"variant_id": "only"}],
            },
        )
        out_dir = tmp_path / "out"

        result = export_experiment([task_file], exp_file, out_dir)

        assert result.exported == []
        assert "experiment-defaults" in result.skipped[0].reason

    def test_variant_with_no_agent_override_still_exports_alongside_a_skipped_one(self, tmp_path: Path) -> None:
        task_file = _write_task(tmp_path)
        exp_file = _write_experiment(
            tmp_path,
            {
                "experiment_id": "mixed",
                "variants": [
                    {"variant_id": "unmodified"},
                    {"variant_id": "sonnet", "agent": {"model": "claude-sonnet-5"}},
                ],
            },
        )
        out_dir = tmp_path / "out"

        result = export_experiment([task_file], exp_file, out_dir)

        assert len(result.exported) == 1
        assert result.exported[0].out_dir == out_dir / "unmodified" / "greet"
        assert [s.variant_id for s in result.skipped] == ["sonnet"]

    def test_task_level_agent_config_alone_does_not_trip_the_skip(self, tmp_path: Path) -> None:
        """The task's OWN agent.type (source='task') must not be mistaken for an experiment override."""
        task_file = _write_task(tmp_path)  # agent: {type: claude-code} is the task's own, not the experiment's
        exp_file = _write_experiment(
            tmp_path,
            {
                "experiment_id": "no-override",
                "variants": [{"variant_id": "only"}],
            },
        )
        out_dir = tmp_path / "out"

        result = export_experiment([task_file], exp_file, out_dir)

        assert len(result.exported) == 1
        assert result.skipped == []


class TestReplicateFanOut:
    def test_repeats_produce_rep_subdirectories(self, tmp_path: Path) -> None:
        task_file = _write_task(tmp_path)
        exp_file = _write_experiment(
            tmp_path,
            {
                "experiment_id": "reps",
                "variants": [{"variant_id": "baseline", "repeats": 2}],
            },
        )
        out_dir = tmp_path / "out"

        result = export_experiment([task_file], exp_file, out_dir)

        assert len(result.exported) == 2
        dests = sorted(str(r.out_dir.relative_to(out_dir)) for r in result.exported)
        assert dests == [
            str(Path("baseline") / "greet" / "rep00"),
            str(Path("baseline") / "greet" / "rep01"),
        ]

    def test_single_replicate_omits_the_rep_segment(self, tmp_path: Path) -> None:
        task_file = _write_task(tmp_path)
        exp_file = _write_experiment(
            tmp_path,
            {"experiment_id": "no-reps", "variants": [{"variant_id": "only"}]},
        )
        out_dir = tmp_path / "out"

        result = export_experiment([task_file], exp_file, out_dir)

        assert result.exported[0].out_dir == out_dir / "only" / "greet"


class TestStructuralRefusalsPerVariant:
    def test_non_docker_driver_variant_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        task_file = _write_task(tmp_path, {"sandbox": {"driver": "tempdir"}})
        exp_file = _write_experiment(
            tmp_path,
            {"experiment_id": "bad-driver", "variants": [{"variant_id": "only"}]},
        )
        out_dir = tmp_path / "out"

        result = export_experiment([task_file], exp_file, out_dir)

        assert result.exported == []
        assert len(result.skipped) == 1
        assert "sandbox.driver" in result.skipped[0].reason
