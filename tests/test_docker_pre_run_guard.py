"""Interim guard: docker tasks whose ``pre_run`` must run inside the container.

Under ``--driver docker`` pre_run runs on the HOST. A ``uv sync`` / ``uip
codedagent setup`` pre_run must run inside the container instead (non-portable
venv, live-tenant provisioning), so it is rejected at resolution time with a
redirect to ``--driver tempdir``. The guard reads the RESOLVED driver, so a CLI
``--driver docker`` is honored, and anchors the match to a command position so a
quoted/argument mention (``echo 'uv sync'``) does not false-positive-gate.
"""

from __future__ import annotations

import pytest

from coder_eval.models import PreRunCommand, SandboxConfig, TaskDefinition
from coder_eval.orchestration.docker_guard import (
    DockerPreRunHostUnsafeError,
    validate_docker_pre_run_host_safety,
)


def _task(driver: str, pre_run: list[PreRunCommand]) -> TaskDefinition:
    return TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox=SandboxConfig(driver=driver),
        agent={"type": "claude-code"},
        success_criteria=[{"type": "file_exists", "description": "c", "path": "x.txt"}],
        pre_run=pre_run,
    )


def test_raises_for_docker_uv_sync():
    task = _task(
        "docker",
        [PreRunCommand(command="bash -c 'cd proj && uv sync && source .venv/bin/activate'")],
    )
    with pytest.raises(DockerPreRunHostUnsafeError, match="tempdir"):
        validate_docker_pre_run_host_safety(task)


def test_raises_for_docker_uip_codedagent_setup():
    task = _task("docker", [PreRunCommand(command="uip codedagent setup --force")])
    with pytest.raises(DockerPreRunHostUnsafeError):
        validate_docker_pre_run_host_safety(task)


def test_raises_for_leading_uv_sync():
    """A bare leading ``uv sync`` (start of the command) is gated."""
    task = _task("docker", [PreRunCommand(command="uv sync --frozen")])
    with pytest.raises(DockerPreRunHostUnsafeError):
        validate_docker_pre_run_host_safety(task)


def test_raises_for_chained_uip_codedagent_setup():
    """A ``&&``-chained ``uip codedagent setup`` (after a shell separator) is gated."""
    task = _task("docker", [PreRunCommand(command="cd proj && uip codedagent setup --force")])
    with pytest.raises(DockerPreRunHostUnsafeError):
        validate_docker_pre_run_host_safety(task)


def test_no_raise_for_echo_mentioning_uv_sync():
    """A quoted mention in an echo is NOT a command-position match — not gated."""
    task = _task("docker", [PreRunCommand(command="echo 'run uv sync to build the venv'")])
    validate_docker_pre_run_host_safety(task)  # no raise


def test_no_raise_for_grep_mentioning_uv_sync():
    """``grep 'uv sync'`` is an argument, not a leading command — not gated."""
    task = _task("docker", [PreRunCommand(command="grep -r 'uv sync' .")])
    validate_docker_pre_run_host_safety(task)  # no raise


def test_no_raise_for_tempdir_with_uv_sync():
    """Tempdir runs everything in the one sandbox — never gated."""
    task = _task("tempdir", [PreRunCommand(command="uv sync")])
    validate_docker_pre_run_host_safety(task)  # no raise


def test_no_raise_for_docker_host_safe_pre_run():
    """A host-safe docker pre_run (seed / cp -r) is fine."""
    task = _task(
        "docker",
        [
            PreRunCommand(command="python seed.py"),
            PreRunCommand(command="cp -r $SKILLS_REPO_PATH/tests/.../_fixtures/proj ."),
        ],
    )
    validate_docker_pre_run_host_safety(task)  # no raise


def test_no_raise_for_docker_without_pre_run():
    task = _task("docker", [])
    validate_docker_pre_run_host_safety(task)  # no raise


def _write_docker_task_yaml(dir_path, task_id: str, pre_run_cmd: str):
    """Write a minimal docker task YAML with a single pre_run command."""
    task_file = dir_path / f"{task_id}.yaml"
    task_file.write_text(
        f"task_id: {task_id}\n"
        + "description: d\n"
        + "initial_prompt: p\n"
        + "agent:\n"
        + "  type: claude-code\n"
        + "sandbox:\n"
        + "  driver: docker\n"
        + "pre_run:\n"
        + f"  - command: {pre_run_cmd}\n"
        + "success_criteria:\n"
        + "  - type: file_exists\n"
        + "    description: c\n"
        + "    path: x.txt\n"
    )
    return task_file


def test_guard_skips_offending_task_not_whole_batch(tmp_path):
    """One ``uv sync`` docker task is quarantined as a skip; the rest of the
    batch still resolves. The guard error must NOT abort resolution for all."""
    from coder_eval.models import ExperimentDefinition, ExperimentVariant
    from coder_eval.orchestration.config import BatchRunConfig
    from coder_eval.orchestration.experiment import resolve_all_tasks

    bad = _write_docker_task_yaml(tmp_path, "needs_container", "uv sync --frozen")
    good = _write_docker_task_yaml(tmp_path, "host_safe", "python seed.py")

    single_variant = [ExperimentVariant(variant_id="default")]
    resolved, skipped = resolve_all_tasks(
        task_files=[bad, good],
        experiment=ExperimentDefinition(experiment_id="exp", variants=single_variant),
        default_experiment=ExperimentDefinition(experiment_id="default", variants=single_variant),
        config=BatchRunConfig(run_dir=tmp_path / "runs"),
    )

    # The host-safe docker task resolves and runs; the batch is NOT aborted.
    assert [rt.task.task_id for rt in resolved] == ["host_safe"]
    # The uv-sync task is quarantined with the redirect message.
    assert len(skipped) == 1
    assert str(bad) == skipped[0].path
    assert "tempdir" in skipped[0].reason
    assert "DockerPreRunHostUnsafeError" in skipped[0].reason


def test_fires_on_resolved_driver_via_cli_override():
    """A YAML tempdir task flipped to docker by CLI ``--driver docker`` is gated
    on the RESOLVED driver — verified through resolve_task_for_variant +
    _apply_cli_overrides."""
    from coder_eval.models import ExperimentDefinition, ExperimentVariant
    from coder_eval.orchestration.config import BatchRunConfig
    from coder_eval.orchestration.experiment import _apply_cli_overrides, resolve_task_for_variant

    # YAML says tempdir; the pre_run is uv sync (would be fine under tempdir).
    task = _task("tempdir", [PreRunCommand(command="uv sync")])
    default_exp = ExperimentDefinition(experiment_id="default", variants=[ExperimentVariant(variant_id="default")])
    exp = ExperimentDefinition(experiment_id="e", variants=[ExperimentVariant(variant_id="default")])
    variant = ExperimentVariant(variant_id="default")

    # Layers 1-4, then layer 5: --driver docker lands in overrides as
    # sandbox.driver=docker.
    config = BatchRunConfig(run_dir="runs", overrides={"sandbox.driver": "docker"})
    resolved, lineage, _ = resolve_task_for_variant(default_exp, task, exp, variant, config)
    _apply_cli_overrides(resolved, config, lineage)

    assert resolved.sandbox.driver == "docker"
    with pytest.raises(DockerPreRunHostUnsafeError):
        validate_docker_pre_run_host_safety(resolved)
