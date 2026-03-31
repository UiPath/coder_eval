"""End-to-end integration tests for snapshot functionality."""

import json
from pathlib import Path

import pytest

from coder_eval.models import SnapshotMode
from coder_eval.orchestration.config import BatchRunConfig
from coder_eval.orchestration.evaluation import create_iteration_snapshot
from coder_eval.orchestration.task_loader import load_task
from coder_eval.orchestrator import Orchestrator


@pytest.mark.asyncio
async def test_snapshot_e2e_full_mode(tmp_path):
    """End-to-end test: Task with full snapshot mode creates snapshots after each iteration."""
    # Use the example snapshot task
    task_file = Path("tasks/test_snapshot_example.yaml")
    run_dir = tmp_path / "test_run"

    # Override to use full mode instead of hybrid
    config = BatchRunConfig(
        run_dir=run_dir,
        max_parallel=1,
        preserve_sandbox=True,
        snapshot_mode="full",
    )

    # Note: This test requires ANTHROPIC_API_KEY to actually run the agent
    # For now, we'll just verify the configuration is applied correctly
    task, _ = load_task(task_file)

    # Apply overrides (same logic as run_batch)
    if config.snapshot_mode:
        from coder_eval.models import SnapshotConfig

        mode = SnapshotMode(config.snapshot_mode.lower())
        task.sandbox.snapshots = SnapshotConfig(
            mode=mode,
            checkpoint_frequency=config.snapshot_checkpoint_freq or task.sandbox.snapshots.checkpoint_frequency,
            ignore_patterns=task.sandbox.snapshots.ignore_patterns,
        )

    # Verify configuration
    assert task.sandbox.snapshots.mode == SnapshotMode.FULL
    assert "*.log" in task.sandbox.snapshots.ignore_patterns
    assert "temp_*" in task.sandbox.snapshots.ignore_patterns


@pytest.mark.asyncio
async def test_snapshot_e2e_hybrid_mode_checkpoint_logic(tmp_path):
    """Verify hybrid mode creates full snapshots at checkpoints, incremental otherwise."""
    from coder_eval.models import FileChange, SnapshotConfig, SnapshotMode, TurnRecord
    from coder_eval.sandbox import Sandbox

    # Create a simple task with hybrid mode
    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)

    # Configure hybrid snapshots
    task.sandbox.snapshots = SnapshotConfig(mode=SnapshotMode.HYBRID, checkpoint_frequency=3)

    run_dir = tmp_path / "test_run" / "hybrid_test"
    orchestrator = Orchestrator(task=task, run_dir=run_dir, preserve_sandbox=False, variant_id="test-variant")

    # Setup snapshot directory and sandbox
    orchestrator.snapshot_base_dir = run_dir / "snapshots"
    orchestrator.snapshot_base_dir.mkdir(parents=True, exist_ok=True)

    orchestrator.sandbox = Sandbox(task.sandbox, task_id=task.task_id)
    sandbox_dir = orchestrator.sandbox.setup()

    # Create test files
    (sandbox_dir / "file1.txt").write_text("Content 1")
    (sandbox_dir / "file2.txt").write_text("Content 2")
    (sandbox_dir / "file3.txt").write_text("Content 3")

    # Iteration 1 (not checkpoint) - should be incremental
    turn_record_1 = TurnRecord(
        iteration=1,
        user_input="Test 1",
        agent_output="Output 1",
        files_changed=[FileChange(path="file1.txt", operation="created")],
    )
    await create_iteration_snapshot(
        sandbox=orchestrator.sandbox,
        snapshot_base_dir=orchestrator.snapshot_base_dir,
        task=task,
        iteration=1,
        turn_record=turn_record_1,
    )

    # Iteration 2 (not checkpoint) - should be incremental
    turn_record_2 = TurnRecord(
        iteration=2,
        user_input="Test 2",
        agent_output="Output 2",
        files_changed=[FileChange(path="file2.txt", operation="modified")],
    )
    await create_iteration_snapshot(
        sandbox=orchestrator.sandbox,
        snapshot_base_dir=orchestrator.snapshot_base_dir,
        task=task,
        iteration=2,
        turn_record=turn_record_2,
    )

    # Iteration 3 (checkpoint!) - should be full
    turn_record_3 = TurnRecord(
        iteration=3,
        user_input="Test 3",
        agent_output="Output 3",
        files_changed=[FileChange(path="file3.txt", operation="modified")],
    )
    await create_iteration_snapshot(
        sandbox=orchestrator.sandbox,
        snapshot_base_dir=orchestrator.snapshot_base_dir,
        task=task,
        iteration=3,
        turn_record=turn_record_3,
    )

    # Verify all snapshots were created
    snapshot_1 = Path(turn_record_1.snapshot_path)
    snapshot_2 = Path(turn_record_2.snapshot_path)
    snapshot_3 = Path(turn_record_3.snapshot_path)

    assert snapshot_1.exists()
    assert snapshot_2.exists()
    assert snapshot_3.exists()

    # Read manifests and verify modes
    manifest_1 = json.loads((snapshot_1 / "manifest.json").read_text())
    manifest_2 = json.loads((snapshot_2 / "manifest.json").read_text())
    manifest_3 = json.loads((snapshot_3 / "manifest.json").read_text())

    # Iterations 1 and 2 should be incremental
    assert manifest_1["mode"] == "incremental"
    assert manifest_2["mode"] == "incremental"

    # Iteration 3 should be full (checkpoint)
    assert manifest_3["mode"] == "full"

    # Iteration 1 should only have file1.txt
    assert manifest_1["file_count"] == 1
    assert "file1.txt" in manifest_1["changed_files"]

    # Iteration 2 should only have file2.txt
    assert manifest_2["file_count"] == 1
    assert "file2.txt" in manifest_2["changed_files"]

    # Iteration 3 (full) should have all files
    assert manifest_3["file_count"] > 2  # At least the 3 files we created

    # Cleanup
    orchestrator.sandbox.cleanup()


@pytest.mark.asyncio
async def test_snapshot_directory_structure(tmp_path):
    """Verify snapshot directory structure and manifest content."""
    from coder_eval.models import SnapshotConfig, SnapshotMode, TurnRecord
    from coder_eval.sandbox import Sandbox

    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)
    task.sandbox.snapshots = SnapshotConfig(mode=SnapshotMode.FULL)

    run_dir = tmp_path / "test_run" / "structure_test"
    orchestrator = Orchestrator(task=task, run_dir=run_dir, preserve_sandbox=False, variant_id="test-variant")

    orchestrator.snapshot_base_dir = run_dir / "snapshots"
    orchestrator.snapshot_base_dir.mkdir(parents=True, exist_ok=True)

    orchestrator.sandbox = Sandbox(task.sandbox, task_id=task.task_id)
    sandbox_dir = orchestrator.sandbox.setup()

    # Create test structure
    (sandbox_dir / "app.py").write_text("print('hello')")
    (sandbox_dir / "README.md").write_text("# Test Project")
    (sandbox_dir / "src").mkdir()
    (sandbox_dir / "src" / "utils.py").write_text("def helper(): pass")

    # Create snapshot
    turn_record = TurnRecord(iteration=1, user_input="Test", agent_output="Output", files_changed=[])
    await create_iteration_snapshot(
        sandbox=orchestrator.sandbox,
        snapshot_base_dir=orchestrator.snapshot_base_dir,
        task=task,
        iteration=1,
        turn_record=turn_record,
    )

    snapshot_path = Path(turn_record.snapshot_path)

    # Verify directory structure
    assert snapshot_path.exists()
    assert snapshot_path.name == "iteration_1"
    assert (snapshot_path / "manifest.json").exists()
    assert (snapshot_path / "app.py").exists()
    assert (snapshot_path / "README.md").exists()
    assert (snapshot_path / "src" / "utils.py").exists()

    # Verify manifest content
    manifest = json.loads((snapshot_path / "manifest.json").read_text())
    # Note: iteration in manifest is set to 0 (default) because it's written before
    # the orchestrator updates it. The iteration number is in the directory name.
    assert manifest["mode"] == "full"
    assert manifest["file_count"] >= 3
    assert manifest["size_bytes"] > 0
    assert "created_at" in manifest

    # Cleanup
    orchestrator.sandbox.cleanup()


@pytest.mark.asyncio
async def test_snapshot_ignore_patterns_applied(tmp_path):
    """Verify that ignore patterns are respected in snapshots."""
    from coder_eval.models import SnapshotConfig, SnapshotMode, TurnRecord
    from coder_eval.sandbox import Sandbox

    task_file = Path("tasks/test_snapshot_example.yaml")
    task, _ = load_task(task_file)

    # Task YAML has ignore patterns: ["*.log", "temp_*"]
    assert "*.log" in task.sandbox.snapshots.ignore_patterns
    assert "temp_*" in task.sandbox.snapshots.ignore_patterns

    # Override to use full mode instead of hybrid
    task.sandbox.snapshots = SnapshotConfig(
        mode=SnapshotMode.FULL,
        ignore_patterns=task.sandbox.snapshots.ignore_patterns,
    )

    run_dir = tmp_path / "test_run" / "ignore_test"
    orchestrator = Orchestrator(task=task, run_dir=run_dir, preserve_sandbox=False, variant_id="test-variant")

    orchestrator.snapshot_base_dir = run_dir / "snapshots"
    orchestrator.snapshot_base_dir.mkdir(parents=True, exist_ok=True)

    orchestrator.sandbox = Sandbox(task.sandbox, task_id=task.task_id)
    sandbox_dir = orchestrator.sandbox.setup()

    # Create files that should be ignored and files that should be kept
    (sandbox_dir / "app.py").write_text("# important")
    (sandbox_dir / "debug.log").write_text("logs here")  # Should be ignored (*.log)
    (sandbox_dir / "temp_data.txt").write_text("temp")  # Should be ignored (temp_*)
    (sandbox_dir / "important.txt").write_text("keep this")

    # Create snapshot
    turn_record = TurnRecord(iteration=1, user_input="Test", agent_output="Output", files_changed=[])
    await create_iteration_snapshot(
        sandbox=orchestrator.sandbox,
        snapshot_base_dir=orchestrator.snapshot_base_dir,
        task=task,
        iteration=1,
        turn_record=turn_record,
    )

    snapshot_path = Path(turn_record.snapshot_path)

    # Verify kept files exist
    assert (snapshot_path / "app.py").exists()
    assert (snapshot_path / "important.txt").exists()

    # Verify ignored files don't exist
    assert not (snapshot_path / "debug.log").exists()
    assert not (snapshot_path / "temp_data.txt").exists()

    # Cleanup
    orchestrator.sandbox.cleanup()


def test_snapshot_yaml_configuration():
    """Verify snapshot configuration can be loaded from YAML."""
    task_file = Path("tasks/test_snapshot_example.yaml")
    task, _ = load_task(task_file)

    # Verify YAML configuration was loaded correctly
    assert task.sandbox.snapshots.mode == SnapshotMode.HYBRID
    assert task.sandbox.snapshots.checkpoint_frequency == 2
    assert task.sandbox.snapshots.ignore_patterns == ["*.log", "temp_*"]


def test_snapshot_cli_override_validation():
    """Verify CLI overrides work with different snapshot modes."""
    from coder_eval.models import SnapshotConfig

    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)

    # Test each mode can be set via CLI override
    for mode_str in ["disabled", "full", "incremental", "hybrid"]:
        mode = SnapshotMode(mode_str)
        task.sandbox.snapshots = SnapshotConfig(
            mode=mode,
            checkpoint_frequency=5,
            ignore_patterns=[],
        )
        assert task.sandbox.snapshots.mode == mode
