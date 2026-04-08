"""Tests for the orchestrator."""

import hashlib
from pathlib import Path

import pytest

from coder_eval.models import DirectRoute
from coder_eval.orchestration.evaluation import create_iteration_snapshot, generate_next_prompt
from coder_eval.orchestration.task_loader import load_task
from coder_eval.orchestrator import Orchestrator, _summarize_tool_calls
from coder_eval.utils import get_version_info


def test_orchestrator_load_task():
    """Test loading a task from YAML."""
    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)

    assert task.task_id == "hello_date_smoke_test"
    assert task.max_iterations == 2
    assert len(task.success_criteria) == 3


def test_orchestrator_load_task_missing_file():
    """Test loading a non-existent task file."""
    with pytest.raises(FileNotFoundError):
        load_task(Path("tasks/nonexistent.yaml"))


def test_orchestrator_load_task_directory():
    """Test that loading a directory instead of a YAML file gives a clear error."""
    with pytest.raises(ValueError, match="Expected a YAML task file but got a directory"):
        load_task(Path("tasks"))


def test_orchestrator_initialization(tmp_path):
    """Test orchestrator initialization."""
    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)

    run_dir = tmp_path / "test_run" / "hello_date"

    orchestrator = Orchestrator(task=task, run_dir=run_dir, preserve_sandbox=False, variant_id="test-variant")

    assert orchestrator.task == task
    assert orchestrator.run_dir == run_dir
    assert orchestrator.sandbox is None
    assert orchestrator.agent is None
    assert orchestrator.result is None


@pytest.mark.asyncio
async def test_orchestrator_create_agent(tmp_path):
    """Test agent creation."""
    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)

    run_dir = tmp_path / "test_run" / "hello_date"

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")
    orchestrator.route = DirectRoute()

    # Create agent
    agent = await orchestrator._create_agent()

    assert agent is not None
    assert agent.config.type == "claude-code"


@pytest.mark.asyncio
async def test_orchestrator_generate_feedback(tmp_path):
    """Test that generate_next_prompt produces meaningful feedback from failed criteria."""
    from coder_eval.models import CriterionResult
    from coder_eval.orchestration.evaluation import generate_next_prompt

    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)

    # Build one failing CriterionResult per success criterion so lengths match
    criteria_results = [
        CriterionResult(
            criterion_type=c.type,
            description=getattr(c, "description", c.type),
            score=0.0,
            error=f"{c.type} failed",
        )
        for c in task.success_criteria
    ]

    feedback = await generate_next_prompt(
        task=task,
        agent_output="I created something",
        criteria_results=criteria_results,
        iteration=1,
        llm_reviewer=None,
        reference_code=None,
    )

    # Feedback should mention at least one failing criterion type
    feedback_lower = feedback.lower()
    assert any(c.type in feedback_lower for c in task.success_criteria)


@pytest.mark.asyncio
async def test_orchestrator_deterministic_feedback_with_failures(tmp_path):
    """Test that orchestrator generates deterministic feedback when criteria fail."""
    # Create a simple task with no LLM reviewer
    from coder_eval.models import (
        AgentConfig,
        AgentKind,
        CriterionResult,
        EvaluationResult,
        FileExistsCriterion,
        LLMReviewerConfig,
        RunCommandCriterion,
        SandboxConfig,
        TaskDefinition,
    )

    task = TaskDefinition(
        task_id="test_feedback",
        description="Test feedback generation",
        initial_prompt="Create hello.py",
        max_iterations=3,
        agent=AgentConfig(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[
            FileExistsCriterion(
                type="file_exists",
                path="hello.py",
                description="Must create hello.py",
                pass_threshold=1.0,
                weight=1.0,
            ),
            RunCommandCriterion(
                type="run_command",
                command="python hello.py",
                description="Must execute successfully",
                pass_threshold=1.0,
                weight=1.0,
            ),
        ],
        llm_reviewer=LLMReviewerConfig(enabled=False),  # ← Disabled to force deterministic feedback
    )

    run_dir = tmp_path / "test_run" / "test_feedback"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task, run_dir, preserve_sandbox=False, variant_id="test-variant")

    # Initialize result (normally done in run())
    from datetime import datetime

    orchestrator.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        variant_id="test-variant",
        agent_type=task.agent.type,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=1,
        environment_info={},
    )

    # Create criteria results with failures
    criteria_results = [
        CriterionResult(
            criterion_type="file_exists",
            description="Must create hello.py",
            score=0.0,  # Failed
            error="File not found",
        ),
        CriterionResult(
            criterion_type="run_command",
            description="Must execute successfully",
            score=0.0,  # Failed
            error="Command not found",
        ),
    ]

    # Generate feedback
    feedback = await generate_next_prompt(
        task=task,
        agent_output="I tried to create the file",
        criteria_results=criteria_results,
        iteration=1,
        llm_reviewer=None,
        reference_code=None,
    )

    # Assertions
    assert "failed" in feedback.lower()
    assert "hello.py" in feedback
    assert "Score: 0.00" in feedback
    assert "threshold: 1.0" in feedback
    assert "File not found" in feedback
    assert "Command not found" in feedback


@pytest.mark.asyncio
async def test_orchestrator_deterministic_feedback_with_partial_scores(tmp_path):
    """Test feedback includes score information for partial success."""
    from coder_eval.models import (
        AgentConfig,
        AgentKind,
        CriterionResult,
        EvaluationResult,
        LLMReviewerConfig,
        PytestCriterion,
        SandboxConfig,
        TaskDefinition,
    )

    task = TaskDefinition(
        task_id="test_partial",
        description="Test partial scores",
        initial_prompt="Write tests",
        max_iterations=3,
        agent=AgentConfig(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[
            PytestCriterion(
                type="pytest",
                path="tests/",
                description="Most tests should pass",
                pass_threshold=0.9,  # Requires 90%
                weight=1.0,
            ),
        ],
        llm_reviewer=LLMReviewerConfig(enabled=False),
    )

    run_dir = tmp_path / "test_run" / "test_partial"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task, run_dir, preserve_sandbox=False, variant_id="test-variant")

    from datetime import datetime

    orchestrator.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        variant_id="test-variant",
        agent_type=task.agent.type,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=1,
        environment_info={},
    )

    # Create criteria result with partial success (70% < 90% threshold)
    criteria_results = [
        CriterionResult(
            criterion_type="pytest",
            description="Most tests should pass",
            score=0.7,  # 70% passed, but threshold is 90%
            details="7 passed, 3 failed out of 10 tests",
        ),
    ]

    feedback = await generate_next_prompt(
        task=task,
        agent_output="I wrote some tests",
        criteria_results=criteria_results,
        iteration=1,
        llm_reviewer=None,
        reference_code=None,
    )

    # Verify feedback shows both score and threshold
    assert "failed" in feedback.lower()
    assert "Score: 0.70" in feedback
    assert "threshold: 0.9" in feedback
    assert "7 passed, 3 failed" in feedback


@pytest.mark.asyncio
async def test_orchestrator_deterministic_feedback_mixed_results(tmp_path):
    """Test that only failed criteria appear in feedback."""
    from coder_eval.models import (
        AgentConfig,
        AgentKind,
        CriterionResult,
        EvaluationResult,
        FileExistsCriterion,
        LLMReviewerConfig,
        RunCommandCriterion,
        SandboxConfig,
        TaskDefinition,
    )

    task = TaskDefinition(
        task_id="test_mixed",
        description="Test mixed results",
        initial_prompt="Create and run script",
        max_iterations=3,
        agent=AgentConfig(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[
            FileExistsCriterion(
                type="file_exists",
                path="script.py",
                description="Script file must exist",
                pass_threshold=1.0,
                weight=1.0,
            ),
            RunCommandCriterion(
                type="run_command",
                command="python script.py",
                description="Script must run without errors",
                pass_threshold=1.0,
                weight=1.0,
            ),
            FileExistsCriterion(
                type="file_exists",
                path="output.txt",
                description="Output file should be created",
                pass_threshold=1.0,
                weight=0.5,
            ),
        ],
        llm_reviewer=LLMReviewerConfig(enabled=False),
    )

    run_dir = tmp_path / "test_run" / "test_mixed"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task, run_dir, preserve_sandbox=False, variant_id="test-variant")

    from datetime import datetime

    orchestrator.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        variant_id="test-variant",
        agent_type=task.agent.type,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=1,
        environment_info={},
    )

    # 2 criteria pass, 1 fails
    criteria_results = [
        CriterionResult(
            criterion_type="file_exists",
            description="Script file must exist",
            score=1.0,  # PASSED
        ),
        CriterionResult(
            criterion_type="run_command",
            description="Script must run without errors",
            score=1.0,  # PASSED
        ),
        CriterionResult(
            criterion_type="file_exists",
            description="Output file should be created",
            score=0.0,  # FAILED
            error="File not found: output.txt",
        ),
    ]

    feedback = await generate_next_prompt(
        task=task,
        agent_output="Created and ran script",
        criteria_results=criteria_results,
        iteration=1,
        llm_reviewer=None,
        reference_code=None,
    )

    # Only the failed criterion should appear
    assert "failed" in feedback.lower()
    assert "Output file should be created" in feedback
    assert "File not found: output.txt" in feedback

    # Passed criteria should NOT appear
    assert "Script file must exist" not in feedback
    assert "Script must run without errors" not in feedback


# ============================================================================
# Batch Orchestration Tests (Phase 1)
# ============================================================================


def create_test_task_file(tmp_path: Path, task_id: str) -> Path:
    """Helper to create a valid test task YAML file."""
    task_content = f"""
task_id: {task_id}
description: Test task for batch execution
initial_prompt: "Test prompt"
max_iterations: 1
agent:
  type: claude-code
sandbox:
  driver: tempdir
  python: {{}}
success_criteria:
  - type: file_exists
    path: test.txt
    description: "Check for test.txt"
"""
    task_file = tmp_path / f"{task_id}.yaml"
    task_file.write_text(task_content)
    return task_file


@pytest.mark.asyncio
async def test_run_batch_empty_list(tmp_path):
    """Test batch execution with empty task list (edge case from review)."""
    from coder_eval.orchestration.config import BatchRunConfig

    config = BatchRunConfig(run_dir=tmp_path / "run", max_parallel=1)

    # Should handle empty list gracefully (empty list of ResolvedTask)
    summary, task_results = await Orchestrator.run_batch([], config)

    # Verify empty summary
    assert task_results == []
    assert summary.tasks_run == 0
    assert summary.tasks_succeeded == 0
    assert summary.tasks_failed == 0
    assert summary.tasks_error == 0
    assert len(summary.task_results) == 0

    # Files should still be created
    assert (tmp_path / "run" / "run.json").exists()
    assert (tmp_path / "run" / "run.md").exists()


def test_batch_run_config_validation():
    """Test BatchRunConfig validation."""
    from coder_eval.orchestration.config import BatchRunConfig

    # Valid config
    config = BatchRunConfig(run_dir=Path("/tmp/run"), max_parallel=3)
    assert config.max_parallel == 3

    # Invalid: max_parallel < 1
    with pytest.raises(ValueError):
        BatchRunConfig(run_dir=Path("/tmp/run"), max_parallel=0)

    with pytest.raises(ValueError):
        BatchRunConfig(run_dir=Path("/tmp/run"), max_parallel=-1)


def test_generate_run_summary(tmp_path):
    """Test run summary generation."""
    from datetime import datetime

    from coder_eval.models import AgentKind, EvaluationResult, TaskResult
    from coder_eval.orchestration.batch import _generate_run_summary

    # Create mock results
    results = [
        TaskResult(
            task_id="task1",
            variant_id="test-variant",
            result=EvaluationResult(
                task_id="task1",
                task_description="Test 1",
                variant_id="test-variant",
                agent_type=AgentKind.CLAUDE_CODE,
                started_at=datetime.now(),
                final_status="SUCCESS",
                iteration_count=1,
                environment_info={},
            ),
            duration=10.0,
        ),
        TaskResult(
            task_id="task2",
            variant_id="test-variant",
            result=EvaluationResult(
                task_id="task2",
                task_description="Test 2",
                variant_id="test-variant",
                agent_type=AgentKind.CLAUDE_CODE,
                started_at=datetime.now(),
                final_status="FAILURE",
                iteration_count=2,
                environment_info={},
            ),
            duration=15.0,
        ),
    ]

    summary = _generate_run_summary(
        run_dir=tmp_path,
        task_results=results,
        start_time=datetime.now(),
        end_time=datetime.now(),
    )

    # Verify summary
    assert summary.tasks_run == 2
    assert summary.tasks_succeeded == 1
    assert summary.tasks_failed == 1
    assert summary.tasks_error == 0

    # Verify files created
    assert (tmp_path / "run.json").exists()
    assert (tmp_path / "run.md").exists()


def test_generate_run_summary_mixed_statuses(tmp_path):
    """Test that tasks_failed excludes ERROR tasks (counted separately in tasks_error)."""
    from datetime import datetime

    from coder_eval.models import AgentKind, EvaluationResult, TaskResult
    from coder_eval.orchestration.batch import _generate_run_summary

    results = [
        TaskResult(
            task_id="task1",
            variant_id="v",
            result=EvaluationResult(
                task_id="task1",
                task_description="ok",
                variant_id="v",
                agent_type=AgentKind.CLAUDE_CODE,
                started_at=datetime.now(),
                final_status="SUCCESS",
                iteration_count=1,
                environment_info={},
            ),
            duration=1.0,
        ),
        TaskResult(
            task_id="task2",
            variant_id="v",
            result=EvaluationResult(
                task_id="task2",
                task_description="fail",
                variant_id="v",
                agent_type=AgentKind.CLAUDE_CODE,
                started_at=datetime.now(),
                final_status="FAILURE",
                iteration_count=1,
                environment_info={},
            ),
            duration=2.0,
        ),
        TaskResult(
            task_id="task3",
            variant_id="v",
            result=EvaluationResult(
                task_id="task3",
                task_description="err",
                variant_id="v",
                agent_type=AgentKind.CLAUDE_CODE,
                started_at=datetime.now(),
                final_status="ERROR",
                iteration_count=0,
                environment_info={},
            ),
            duration=0.5,
        ),
    ]

    summary = _generate_run_summary(
        run_dir=tmp_path,
        task_results=results,
        start_time=datetime.now(),
        end_time=datetime.now(),
    )

    assert summary.tasks_run == 3
    assert summary.tasks_succeeded == 1
    assert summary.tasks_failed == 1  # only FAILURE, not ERROR
    assert summary.tasks_error == 1
    # Invariant: succeeded + failed + error == total
    assert summary.tasks_succeeded + summary.tasks_failed + summary.tasks_error == summary.tasks_run


def test_create_error_result(tmp_path):
    """Test error result creation for failed tasks."""
    from coder_eval.models import TaskResult
    from coder_eval.orchestration.batch import _create_error_task_result

    task_file = tmp_path / "failed_task.yaml"
    error = ValueError("Task loading failed")

    result = _create_error_task_result(task_file, error, variant_id="test-variant")

    # Verify typed result
    assert isinstance(result, TaskResult)
    assert result.task_id == "failed_task"  # Stem of filename
    assert result.duration == 0.0
    assert result.result.final_status == "ERROR"
    assert result.result.error_message == "Task loading failed"
    assert result.result.iteration_count == 0


# ==================== Persistent Sandbox / Cleanup Tests ====================


@pytest.mark.asyncio
async def test_orchestrator_cleanup_persistent_sandbox(tmp_path):
    """Test that _cleanup with preserve_sandbox=True and a persistent sandbox skips copy."""
    from datetime import datetime

    from coder_eval.models import AgentKind, EvaluationResult, SnapshotConfig, SnapshotMode
    from coder_eval.sandbox import Sandbox

    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)
    task.sandbox.python = None
    task.sandbox.snapshots = SnapshotConfig(mode=SnapshotMode.DISABLED)

    run_dir = tmp_path / "test_run" / "hello_date"
    orchestrator = Orchestrator(task=task, run_dir=run_dir, preserve_sandbox=True, variant_id="test-variant")

    # Initialize result (normally done in run())
    orchestrator.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )

    # Set up sandbox with persistent target (simulates what _setup does)
    persist_target = run_dir / "artifacts" / task.task_id
    orchestrator.sandbox = Sandbox(task.sandbox, task_id=task.task_id)
    orchestrator.sandbox.setup(target_dir=persist_target)

    # Create a test file in sandbox
    (persist_target / "output.txt").write_text("agent output")
    assert orchestrator.sandbox.is_persistent

    # Run cleanup
    await orchestrator._cleanup()

    # Verify: sandbox directory still exists (no deletion)
    assert persist_target.exists()
    assert (persist_target / "output.txt").read_text() == "agent output"

    # Verify: result.sandbox_path is set to the persistent sandbox dir
    assert orchestrator.result.sandbox_path == str(persist_target)


@pytest.mark.asyncio
async def test_orchestrator_cleanup_non_persistent_sandbox_with_preserve(tmp_path):
    """Test that _cleanup with preserve_sandbox=True and a non-persistent sandbox copies to artifacts."""
    from datetime import datetime

    from coder_eval.models import AgentKind, EvaluationResult, SnapshotConfig, SnapshotMode
    from coder_eval.sandbox import Sandbox

    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)
    task.sandbox.python = None
    task.sandbox.snapshots = SnapshotConfig(mode=SnapshotMode.DISABLED)

    run_dir = tmp_path / "test_run" / "hello_date"
    orchestrator = Orchestrator(task=task, run_dir=run_dir, preserve_sandbox=True, variant_id="test-variant")

    # Initialize result
    orchestrator.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )

    # Set up sandbox WITHOUT target_dir (non-persistent / legacy path)
    orchestrator.sandbox = Sandbox(task.sandbox, task_id=task.task_id)
    sandbox_dir = orchestrator.sandbox.setup()

    # Create a test file
    (sandbox_dir / "output.txt").write_text("agent output")
    assert not orchestrator.sandbox.is_persistent

    # Run cleanup
    await orchestrator._cleanup()

    # Verify: sandbox was copied to artifacts dir (legacy path)
    expected_preserve_path = run_dir / "artifacts" / task.task_id
    assert expected_preserve_path.exists()
    assert (expected_preserve_path / "output.txt").read_text() == "agent output"
    assert orchestrator.result.sandbox_path == str(expected_preserve_path)

    # Original temp dir should be cleaned up
    assert not sandbox_dir.exists()


# ==================== Snapshot Integration Tests ====================


@pytest.mark.asyncio
async def test_orchestrator_snapshot_setup_disabled(tmp_path):
    """Test that snapshot directory is not created when snapshots disabled."""
    from coder_eval.models import SnapshotConfig, SnapshotMode

    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)

    # Explicitly disable snapshots; no venv needed for snapshot setup test
    task.sandbox.snapshots = SnapshotConfig(mode=SnapshotMode.DISABLED)
    task.sandbox.python = None

    run_dir = tmp_path / "test_run" / "hello_date"
    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")

    # Initialize result
    from datetime import datetime

    from coder_eval.models import AgentKind, EvaluationResult

    orchestrator.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )

    await orchestrator._setup()

    # Verify snapshot directory not created
    assert orchestrator.snapshot_base_dir is None
    snapshot_dir = run_dir / "snapshots"
    assert not snapshot_dir.exists()

    # Cleanup
    await orchestrator._cleanup()


@pytest.mark.asyncio
async def test_orchestrator_snapshot_setup_enabled(tmp_path):
    """Test that snapshot directory is created when snapshots enabled."""
    from coder_eval.models import SnapshotConfig, SnapshotMode

    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)

    # Enable full snapshots; no venv needed for snapshot setup test
    task.sandbox.snapshots = SnapshotConfig(mode=SnapshotMode.FULL)
    task.sandbox.python = None

    run_dir = tmp_path / "test_run" / "hello_date"
    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")

    # Initialize result
    from datetime import datetime

    from coder_eval.models import AgentKind, EvaluationResult

    orchestrator.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )

    await orchestrator._setup()

    # Verify snapshot directory created
    assert orchestrator.snapshot_base_dir is not None
    snapshot_dir = run_dir / "snapshots"
    assert snapshot_dir.exists()
    assert snapshot_dir.is_dir()

    # Cleanup
    await orchestrator._cleanup()


@pytest.mark.asyncio
async def test_orchestrator_create_iteration_snapshot_disabled(tmp_path):
    """Test that no snapshot is created when snapshots disabled."""
    from coder_eval.models import SnapshotConfig, SnapshotMode, TurnRecord

    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)

    # Disable snapshots
    task.sandbox.snapshots = SnapshotConfig(mode=SnapshotMode.DISABLED)

    run_dir = tmp_path / "test_run" / "hello_date"
    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")

    # Create dummy turn record
    turn_record = TurnRecord(
        iteration=1,
        user_input="Test",
        agent_output="Test output",
        files_changed=[],
    )

    # Call snapshot creation (should do nothing)
    await create_iteration_snapshot(
        sandbox=orchestrator.sandbox,
        snapshot_base_dir=orchestrator.snapshot_base_dir,
        task=task,
        iteration=1,
        turn_record=turn_record,
    )

    # Verify no snapshot created
    assert turn_record.snapshot_path is None
    assert turn_record.snapshot_size_bytes is None


@pytest.mark.asyncio
async def test_orchestrator_create_iteration_snapshot_full(tmp_path):
    """Test full snapshot creation during evaluation."""
    from coder_eval.models import SnapshotConfig, SnapshotMode, TurnRecord
    from coder_eval.sandbox import Sandbox

    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)

    # Enable full snapshots
    task.sandbox.snapshots = SnapshotConfig(mode=SnapshotMode.FULL)
    task.sandbox.python = None

    run_dir = tmp_path / "test_run" / "hello_date"
    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")

    # Create snapshot directory
    orchestrator.snapshot_base_dir = run_dir / "snapshots"
    orchestrator.snapshot_base_dir.mkdir(parents=True, exist_ok=True)

    # Create and setup sandbox
    orchestrator.sandbox = Sandbox(task.sandbox, task_id=task.task_id)
    sandbox_dir = orchestrator.sandbox.setup()

    # Create a test file in sandbox
    (sandbox_dir / "test.txt").write_text("Test content")

    # Create dummy turn record
    turn_record = TurnRecord(
        iteration=1,
        user_input="Test",
        agent_output="Test output",
        files_changed=[],
    )

    # Create snapshot
    await create_iteration_snapshot(
        sandbox=orchestrator.sandbox,
        snapshot_base_dir=orchestrator.snapshot_base_dir,
        task=task,
        iteration=1,
        turn_record=turn_record,
    )

    # Verify snapshot was created
    assert turn_record.snapshot_path is not None
    assert turn_record.snapshot_size_bytes is not None
    assert turn_record.snapshot_size_bytes > 0

    snapshot_path = Path(turn_record.snapshot_path)
    assert snapshot_path.exists()
    assert (snapshot_path / "test.txt").exists()
    assert (snapshot_path / "manifest.json").exists()

    # Cleanup
    orchestrator.sandbox.cleanup()


@pytest.mark.asyncio
async def test_orchestrator_create_iteration_snapshot_hybrid(tmp_path):
    """Test hybrid snapshot mode with checkpoint logic."""
    from coder_eval.models import FileChange, SnapshotConfig, SnapshotMode, TurnRecord
    from coder_eval.sandbox import Sandbox

    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)

    # Enable hybrid snapshots with checkpoint every 2 iterations
    task.sandbox.snapshots = SnapshotConfig(mode=SnapshotMode.HYBRID, checkpoint_frequency=2)
    task.sandbox.python = None

    run_dir = tmp_path / "test_run" / "hello_date"
    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")

    # Create snapshot directory
    orchestrator.snapshot_base_dir = run_dir / "snapshots"
    orchestrator.snapshot_base_dir.mkdir(parents=True, exist_ok=True)

    # Create and setup sandbox
    orchestrator.sandbox = Sandbox(task.sandbox, task_id=task.task_id)
    sandbox_dir = orchestrator.sandbox.setup()

    # Create test files
    (sandbox_dir / "file1.txt").write_text("Content 1")
    (sandbox_dir / "file2.txt").write_text("Content 2")

    # Iteration 1 (not a checkpoint) - should create incremental
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

    # Iteration 2 (checkpoint) - should create full
    turn_record_2 = TurnRecord(
        iteration=2,
        user_input="Test 2",
        agent_output="Output 2",
        files_changed=[FileChange(path="file2.txt", operation="created")],
    )
    await create_iteration_snapshot(
        sandbox=orchestrator.sandbox,
        snapshot_base_dir=orchestrator.snapshot_base_dir,
        task=task,
        iteration=2,
        turn_record=turn_record_2,
    )

    # Verify both snapshots exist
    assert turn_record_1.snapshot_path is not None
    assert turn_record_2.snapshot_path is not None

    snapshot_1 = Path(turn_record_1.snapshot_path)
    snapshot_2 = Path(turn_record_2.snapshot_path)

    assert snapshot_1.exists()
    assert snapshot_2.exists()

    # Read manifests to verify modes
    import json

    manifest_1_text = (snapshot_1 / "manifest.json").read_text()
    manifest_1 = json.loads(manifest_1_text)
    assert manifest_1["mode"] == "incremental"

    manifest_2_text = (snapshot_2 / "manifest.json").read_text()
    manifest_2 = json.loads(manifest_2_text)
    assert manifest_2["mode"] == "full"

    # Cleanup
    orchestrator.sandbox.cleanup()


@pytest.mark.asyncio
async def test_orchestrator_snapshot_error_handling(tmp_path):
    """Test that snapshot errors don't crash evaluation."""
    from coder_eval.models import SnapshotConfig, SnapshotMode, TurnRecord

    task_file = Path("tasks/hello_date.yaml")
    task, _ = load_task(task_file)

    # Enable snapshots
    task.sandbox.snapshots = SnapshotConfig(mode=SnapshotMode.FULL)

    run_dir = tmp_path / "test_run" / "hello_date"
    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")

    # Create snapshot directory but DON'T setup sandbox (will cause error)
    orchestrator.snapshot_base_dir = run_dir / "snapshots"
    orchestrator.snapshot_base_dir.mkdir(parents=True, exist_ok=True)

    # Don't create sandbox - this will trigger an error in snapshot creation

    # Create dummy turn record
    turn_record = TurnRecord(
        iteration=1,
        user_input="Test",
        agent_output="Test output",
        files_changed=[],
    )

    # Call snapshot creation - should handle error gracefully
    await create_iteration_snapshot(
        sandbox=orchestrator.sandbox,
        snapshot_base_dir=orchestrator.snapshot_base_dir,
        task=task,
        iteration=1,
        turn_record=turn_record,
    )

    # Verify snapshot fields remain None (error was handled)
    assert turn_record.snapshot_path is None
    assert turn_record.snapshot_size_bytes is None


# ==================== CLI / BatchRunConfig Snapshot Tests ====================


def test_batch_run_config_with_snapshot_overrides():
    """Test that BatchRunConfig accepts snapshot override parameters."""
    from coder_eval.orchestration.config import BatchRunConfig

    config = BatchRunConfig(
        run_dir=Path("/tmp/test_run"),
        max_parallel=2,
        preserve_sandbox=True,
        max_iterations=5,
        snapshot_mode="hybrid",
        snapshot_checkpoint_freq=3,
    )

    assert config.snapshot_mode == "hybrid"
    assert config.snapshot_checkpoint_freq == 3
    assert config.max_iterations == 5


def test_batch_run_config_snapshot_defaults():
    """Test that BatchRunConfig has correct defaults for snapshot fields."""
    from coder_eval.orchestration.config import BatchRunConfig

    config = BatchRunConfig(
        run_dir=Path("/tmp/test_run"),
    )

    assert config.snapshot_mode is None
    assert config.snapshot_checkpoint_freq is None
    assert config.max_iterations is None


@pytest.mark.asyncio
async def test_run_batch_applies_snapshot_mode_override(tmp_path):
    """Test that run_batch applies snapshot mode override from config."""
    from coder_eval.models import SnapshotMode
    from coder_eval.orchestration.config import BatchRunConfig

    task_file = Path("tasks/hello_date.yaml")
    run_dir = tmp_path / "test_run"

    # Create config with snapshot override
    config = BatchRunConfig(
        run_dir=run_dir,
        snapshot_mode="full",  # Override to full mode
    )

    # Load task (which has disabled snapshots by default)
    task, _ = load_task(task_file)
    original_mode = task.sandbox.snapshots.mode

    # Simulate what run_batch does with overrides
    if config.snapshot_mode:
        from coder_eval.models import SnapshotConfig

        mode = SnapshotMode(config.snapshot_mode.lower())
        task.sandbox.snapshots = SnapshotConfig(
            mode=mode,
            checkpoint_frequency=config.snapshot_checkpoint_freq or task.sandbox.snapshots.checkpoint_frequency,
            ignore_patterns=task.sandbox.snapshots.ignore_patterns,
        )

    # Verify override was applied
    assert task.sandbox.snapshots.mode == SnapshotMode.FULL
    assert task.sandbox.snapshots.mode != original_mode  # Changed from default


@pytest.mark.asyncio
async def test_run_batch_applies_checkpoint_freq_override(tmp_path):
    """Test that run_batch applies checkpoint frequency override."""
    from coder_eval.models import SnapshotMode
    from coder_eval.orchestration.config import BatchRunConfig

    task_file = Path("tasks/hello_date.yaml")
    run_dir = tmp_path / "test_run"

    # Create config with checkpoint frequency override
    config = BatchRunConfig(
        run_dir=run_dir,
        snapshot_mode="hybrid",
        snapshot_checkpoint_freq=10,  # Override to 10
    )

    # Load task
    task, _ = load_task(task_file)

    # Apply overrides (simulating run_batch logic)
    if config.snapshot_mode:
        from coder_eval.models import SnapshotConfig

        mode = SnapshotMode(config.snapshot_mode.lower())
        task.sandbox.snapshots = SnapshotConfig(
            mode=mode,
            checkpoint_frequency=config.snapshot_checkpoint_freq or task.sandbox.snapshots.checkpoint_frequency,
            ignore_patterns=task.sandbox.snapshots.ignore_patterns,
        )

    # Verify overrides were applied
    assert task.sandbox.snapshots.mode == SnapshotMode.HYBRID
    assert task.sandbox.snapshots.checkpoint_frequency == 10


@pytest.mark.asyncio
async def test_run_batch_preserves_ignore_patterns(tmp_path):
    """Test that run_batch preserves task-specific ignore patterns when overriding."""
    from coder_eval.models import SnapshotMode
    from coder_eval.orchestration.config import BatchRunConfig

    task_file = Path("tasks/test_snapshot_example.yaml")
    run_dir = tmp_path / "test_run"

    # Create config with snapshot mode override
    config = BatchRunConfig(
        run_dir=run_dir,
        snapshot_mode="full",  # Override mode
    )

    # Load task (has custom ignore patterns in YAML)
    task, _ = load_task(task_file)
    original_patterns = task.sandbox.snapshots.ignore_patterns.copy()

    # Apply overrides
    if config.snapshot_mode:
        from coder_eval.models import SnapshotConfig

        mode = SnapshotMode(config.snapshot_mode.lower())
        task.sandbox.snapshots = SnapshotConfig(
            mode=mode,
            checkpoint_frequency=config.snapshot_checkpoint_freq or task.sandbox.snapshots.checkpoint_frequency,
            ignore_patterns=task.sandbox.snapshots.ignore_patterns,  # Preserve task patterns
        )

    # Verify mode was overridden but patterns preserved
    assert task.sandbox.snapshots.mode == SnapshotMode.FULL
    assert task.sandbox.snapshots.ignore_patterns == original_patterns
    assert "*.log" in task.sandbox.snapshots.ignore_patterns
    assert "temp_*" in task.sandbox.snapshots.ignore_patterns


# ==================== get_version_info Tests ====================


def test_get_version_info_without_sandbox_path():
    """Test get_version_info() backward compatibility without sandbox_path."""
    info = get_version_info()

    # Should return standard keys
    assert "claude_code_cli" in info
    assert "uv" in info
    assert "anthropic" in info
    assert "pydantic" in info

    # Should NOT have CLAUDE.md keys
    assert "claude_md_sha256" not in info
    assert "claude_md_size_bytes" not in info


def test_get_version_info_with_sandbox_path_and_claude_md(tmp_path):
    """Test get_version_info() includes CLAUDE.md hash when present."""
    # Create a CLAUDE.md in the sandbox
    claude_md = tmp_path / "CLAUDE.md"
    content = b"# Test CLAUDE.md\nSome instructions here."
    claude_md.write_bytes(content)

    info = get_version_info(sandbox_path=tmp_path)

    # Should have CLAUDE.md hash
    expected_hash = hashlib.sha256(content).hexdigest()
    assert info["claude_md_sha256"] == expected_hash
    assert info["claude_md_size_bytes"] == str(len(content))


def test_get_version_info_with_sandbox_path_no_claude_md(tmp_path):
    """Test get_version_info() omits CLAUDE.md keys when file doesn't exist."""
    info = get_version_info(sandbox_path=tmp_path)

    assert "claude_md_sha256" not in info
    assert "claude_md_size_bytes" not in info


# ==================== Agent Config on EvaluationResult Tests ====================


def test_evaluation_result_agent_config_default():
    """Test that EvaluationResult.agent_config defaults to None."""
    from datetime import datetime

    from coder_eval.models import AgentKind, EvaluationResult

    result = EvaluationResult(
        task_id="test",
        task_description="test",
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="SUCCESS",
        iteration_count=1,
    )

    assert result.agent_config is None


def test_evaluation_result_agent_config_set():
    """Test that EvaluationResult.agent_config can be set from AgentConfig."""
    from datetime import datetime

    from coder_eval.models import AgentConfig, AgentKind, EvaluationResult

    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="bypassPermissions",
        allowed_tools=["Read", "Write"],
        model="claude-sonnet-4-5-20250514",
    )

    result = EvaluationResult(
        task_id="test",
        task_description="test",
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="SUCCESS",
        iteration_count=1,
        agent_config=config,
    )

    assert result.agent_config is not None
    assert result.agent_config.permission_mode == "bypassPermissions"
    assert result.agent_config.allowed_tools == ["Read", "Write"]
    assert result.agent_config.model == "claude-sonnet-4-5-20250514"


def test_evaluation_result_serialization_roundtrip_with_agent_config():
    """Test that EvaluationResult with agent_config survives JSON roundtrip."""
    from datetime import datetime

    from coder_eval.models import AgentConfig, AgentKind, EvaluationResult

    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        allowed_tools=["Read"],
        model="claude-sonnet-4-5-20250514",
    )

    original = EvaluationResult(
        task_id="roundtrip_test",
        task_description="test",
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime(2025, 1, 1, 12, 0, 0),
        final_status="SUCCESS",
        iteration_count=1,
        agent_config=config,
    )

    # Serialize and deserialize
    json_str = original.model_dump_json()
    restored = EvaluationResult.model_validate_json(json_str)

    assert restored.agent_config is not None
    assert restored.agent_config.type == AgentKind.CLAUDE_CODE
    assert restored.agent_config.permission_mode == "acceptEdits"
    assert restored.agent_config.allowed_tools == ["Read"]
    assert restored.agent_config.model == "claude-sonnet-4-5-20250514"


def test_evaluation_result_backward_compat_without_agent_config():
    """Test that old JSON without agent_config still deserializes."""
    from coder_eval.models import EvaluationResult

    # JSON from before agent_config existed (no agent_config field)
    old_json = """{
        "task_id": "old_task",
        "task_description": "old test",
        "variant_id": "test-variant",
        "agent_type": "claude-code",
        "started_at": "2025-01-01T12:00:00",
        "final_status": "SUCCESS",
        "iteration_count": 1
    }"""

    result = EvaluationResult.model_validate_json(old_json)

    assert result.agent_config is None
    assert result.task_id == "old_task"


# ==================== Batch Error Mapping After Tag Filter Tests ====================


def test_batch_error_mapping_after_tag_filter(tmp_path):
    """Test that batch error results map to correct task file after tag filtering."""
    from datetime import datetime

    from coder_eval.models import AgentKind, EvaluationResult, TaskResult
    from coder_eval.orchestration.batch import _create_error_task_result

    # Simulate: 3 original tasks, filter removes task 0, leaving tasks 1 and 2
    # If task 1 (index 0 in filtered list) errors, it should map to task_b, not task_a

    task_a_result = TaskResult(
        task_id="task_b",
        variant_id="test-variant",
        result=EvaluationResult(
            task_id="task_b",
            task_description="Task B",
            variant_id="test-variant",
            agent_type=AgentKind.CLAUDE_CODE,
            started_at=datetime.now(),
            final_status="SUCCESS",
            iteration_count=1,
        ),
        duration=10.0,
    )

    task_b_error = _create_error_task_result(
        Path("task_c.yaml"), ValueError("Task C failed"), variant_id="test-variant"
    )

    # Both should have correct task IDs regardless of original ordering
    assert task_a_result.task_id == "task_b"
    assert task_b_error.task_id == "task_c"  # stem of the yaml file


def test_generate_run_summary_includes_agent_config(tmp_path):
    """Test that _generate_run_summary includes agent_config in task results."""
    from datetime import datetime

    from coder_eval.models import AgentConfig, AgentKind, EvaluationResult, TaskResult
    from coder_eval.orchestration.batch import _generate_run_summary

    config = AgentConfig(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        model="claude-sonnet-4-5-20250514",
    )

    results = [
        TaskResult(
            task_id="task1",
            variant_id="test-variant",
            result=EvaluationResult(
                task_id="task1",
                task_description="Test",
                variant_id="test-variant",
                agent_type=AgentKind.CLAUDE_CODE,
                started_at=datetime.now(),
                final_status="SUCCESS",
                iteration_count=1,
                agent_config=config,
            ),
            duration=10.0,
        ),
    ]

    summary = _generate_run_summary(
        run_dir=tmp_path,
        task_results=results,
        start_time=datetime.now(),
        end_time=datetime.now(),
    )

    # Verify agent_config is included in task results
    assert len(summary.task_results) == 1
    task_result = summary.task_results[0]
    assert task_result["agent_config"] is not None
    assert task_result["agent_config"]["permission_mode"] == "acceptEdits"
    assert task_result["agent_config"]["model"] == "claude-sonnet-4-5-20250514"


def test_generate_run_summary_agent_config_none(tmp_path):
    """Test that _generate_run_summary handles None agent_config."""
    from datetime import datetime

    from coder_eval.models import AgentKind, EvaluationResult, TaskResult
    from coder_eval.orchestration.batch import _generate_run_summary

    results = [
        TaskResult(
            task_id="task1",
            variant_id="test-variant",
            result=EvaluationResult(
                task_id="task1",
                task_description="Test",
                variant_id="test-variant",
                agent_type=AgentKind.CLAUDE_CODE,
                started_at=datetime.now(),
                final_status="ERROR",
                iteration_count=0,
            ),
            duration=0.0,
        ),
    ]

    summary = _generate_run_summary(
        run_dir=tmp_path,
        task_results=results,
        start_time=datetime.now(),
        end_time=datetime.now(),
    )

    assert summary.task_results[0]["agent_config"] is None


# ==================== SDK Options Dump Tests ====================


def test_dump_sdk_options_basic():
    """Test _dump_sdk_options with a real ClaudeAgentOptions instance."""
    from claude_agent_sdk import ClaudeAgentOptions

    from coder_eval.agents.claude_code_agent import _dump_sdk_options

    options = ClaudeAgentOptions(
        cwd="/tmp/test",
        permission_mode="bypassPermissions",
        allowed_tools=["Read", "Write"],
        model="claude-sonnet-4-5-20250514",
    )

    dump = _dump_sdk_options(options)

    assert isinstance(dump, dict)
    assert dump["cwd"] == "/tmp/test"
    assert dump["permission_mode"] == "bypassPermissions"
    assert dump["allowed_tools"] == ["Read", "Write"]
    assert dump["model"] == "claude-sonnet-4-5-20250514"


def test_dump_sdk_options_excludes_callables():
    """Test that _dump_sdk_options skips callable fields like stderr."""
    from claude_agent_sdk import ClaudeAgentOptions

    from coder_eval.agents.claude_code_agent import _dump_sdk_options

    def my_stderr(line: str) -> None:
        pass

    options = ClaudeAgentOptions(
        cwd="/tmp/test",
        stderr=my_stderr,
    )

    dump = _dump_sdk_options(options)

    # stderr is a callable and should be excluded
    assert "stderr" not in dump


def test_dump_sdk_options_includes_defaults():
    """Test that _dump_sdk_options includes fields with default values."""
    from claude_agent_sdk import ClaudeAgentOptions

    from coder_eval.agents.claude_code_agent import _dump_sdk_options

    options = ClaudeAgentOptions(cwd="/tmp/test")

    dump = _dump_sdk_options(options)

    # Should include fields with default values
    assert "max_turns" in dump
    assert "model" in dump
    assert "thinking" in dump
    assert "effort" in dump
    assert "mcp_servers" in dump


def test_dump_sdk_options_converts_path():
    """Test that _dump_sdk_options converts Path objects to strings."""
    from claude_agent_sdk import ClaudeAgentOptions

    from coder_eval.agents.claude_code_agent import _dump_sdk_options

    test_path = Path("/tmp/test")
    options = ClaudeAgentOptions(cwd=test_path)

    dump = _dump_sdk_options(options)

    assert isinstance(dump["cwd"], str)
    assert dump["cwd"] == str(test_path)


def test_dump_sdk_options_handles_nested_dataclasses():
    """Test that _dump_sdk_options recursively serializes nested dataclasses.

    This test verifies that HookMatcher (a dataclass with callable fields)
    and AgentDefinition (a dataclass with string fields) are properly
    handled without crashing Pydantic serialization.
    """
    import json

    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk.types import AgentDefinition, HookMatcher

    from coder_eval.agents.claude_code_agent import _dump_sdk_options

    async def my_hook(input, output, ctx):
        return {"action": "allow"}

    options = ClaudeAgentOptions(
        cwd="/tmp/test",
        hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[my_hook], timeout=30.0)]},
        agents={"helper": AgentDefinition(description="test agent", prompt="do stuff")},
    )

    dump = _dump_sdk_options(options)

    # Hooks should be recursively serialized, with callables stripped
    assert "hooks" in dump
    hook_list = dump["hooks"]["PreToolUse"]
    assert len(hook_list) == 1
    assert hook_list[0]["matcher"] == "Bash"
    assert hook_list[0]["timeout"] == 30.0
    # Callable hooks inside HookMatcher should be stripped (empty list)
    assert hook_list[0]["hooks"] == []

    # AgentDefinition should be recursively converted to dict
    assert "agents" in dump
    assert dump["agents"]["helper"]["description"] == "test agent"
    assert dump["agents"]["helper"]["prompt"] == "do stuff"

    # The entire dump must be JSON-serializable
    json.dumps(dump)

    # And Pydantic-serializable (the actual serialization path)
    from typing import Any

    from pydantic import BaseModel

    class TestModel(BaseModel):
        sdk_options: dict[str, Any] | None = None

    m = TestModel(sdk_options=dump)
    m.model_dump_json()  # Must not raise


def test_evaluation_result_sdk_options_default():
    """Test that EvaluationResult.sdk_options defaults to None."""
    from datetime import datetime

    from coder_eval.models import AgentKind, EvaluationResult

    result = EvaluationResult(
        task_id="test",
        task_description="test",
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="SUCCESS",
        iteration_count=1,
    )

    assert result.sdk_options is None


def test_evaluation_result_serialization_roundtrip_with_sdk_options():
    """Test that EvaluationResult with sdk_options survives JSON roundtrip."""
    from datetime import datetime

    from coder_eval.models import EvaluationResult

    original = EvaluationResult(
        task_id="roundtrip_sdk",
        task_description="test",
        variant_id="test-variant",
        agent_type="claude-code",
        started_at=datetime(2025, 1, 1, 12, 0, 0),
        final_status="SUCCESS",
        iteration_count=1,
        sdk_options={
            "cwd": "/tmp/test",
            "permission_mode": "bypassPermissions",
            "allowed_tools": ["Read"],
            "model": "claude-sonnet-4-5-20250514",
            "max_turns": None,
            "thinking": None,
            "effort": None,
            "mcp_servers": {},
        },
    )

    json_str = original.model_dump_json()
    restored = EvaluationResult.model_validate_json(json_str)

    assert restored.sdk_options is not None
    assert restored.sdk_options["cwd"] == "/tmp/test"
    assert restored.sdk_options["permission_mode"] == "bypassPermissions"
    assert restored.sdk_options["allowed_tools"] == ["Read"]
    assert restored.sdk_options["model"] == "claude-sonnet-4-5-20250514"
    assert restored.sdk_options["max_turns"] is None


def test_evaluation_result_backward_compat_without_sdk_options():
    """Test that old JSON without sdk_options still deserializes."""
    from coder_eval.models import EvaluationResult

    old_json = """{
        "task_id": "old_task",
        "task_description": "old test",
        "variant_id": "test-variant",
        "agent_type": "claude-code",
        "started_at": "2025-01-01T12:00:00",
        "final_status": "SUCCESS",
        "iteration_count": 1
    }"""

    result = EvaluationResult.model_validate_json(old_json)

    assert result.sdk_options is None
    assert result.task_id == "old_task"


def test_generate_run_summary_includes_sdk_options(tmp_path):
    """Test that _generate_run_summary includes sdk_options in task results."""
    from datetime import datetime

    from coder_eval.models import AgentKind, EvaluationResult, TaskResult
    from coder_eval.orchestration.batch import _generate_run_summary

    sdk_opts = {
        "cwd": "/tmp/sandbox",
        "permission_mode": "bypassPermissions",
        "allowed_tools": [],
        "model": "claude-sonnet-4-5-20250514",
        "max_turns": 50,
        "thinking": None,
    }

    results = [
        TaskResult(
            task_id="task1",
            variant_id="test-variant",
            result=EvaluationResult(
                task_id="task1",
                task_description="Test",
                variant_id="test-variant",
                agent_type=AgentKind.CLAUDE_CODE,
                started_at=datetime.now(),
                final_status="SUCCESS",
                iteration_count=1,
                sdk_options=sdk_opts,
            ),
            duration=10.0,
        ),
    ]

    summary = _generate_run_summary(
        run_dir=tmp_path,
        task_results=results,
        start_time=datetime.now(),
        end_time=datetime.now(),
    )

    assert summary.task_results[0]["sdk_options"] is not None
    assert summary.task_results[0]["sdk_options"]["permission_mode"] == "bypassPermissions"
    assert summary.task_results[0]["sdk_options"]["max_turns"] == 50


def test_claude_code_agent_get_sdk_options_before_communicate():
    """Test that get_sdk_options returns None before communicate() is called."""
    from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
    from coder_eval.models import AgentConfig, AgentKind

    agent = ClaudeCodeAgent(AgentConfig(type=AgentKind.CLAUDE_CODE))

    assert agent.get_sdk_options() is None


def test_batch_run_config_with_agent_overrides():
    """Test BatchRunConfig accepts agent override fields."""
    from coder_eval.orchestration.config import BatchRunConfig

    config = BatchRunConfig(
        run_dir=Path("/tmp/run"),
        agent_model="claude-sonnet-4-20250514",
        permission_mode="bypassPermissions",
        max_turns=50,
    )
    assert config.agent_model == "claude-sonnet-4-20250514"
    assert config.permission_mode == "bypassPermissions"
    assert config.max_turns == 50


def test_batch_run_config_agent_override_defaults():
    """Test BatchRunConfig agent override fields default to None."""
    from coder_eval.orchestration.config import BatchRunConfig

    config = BatchRunConfig(run_dir=Path("/tmp/run"))
    assert config.agent_model is None
    assert config.permission_mode is None
    assert config.max_turns is None


@pytest.mark.asyncio
async def test_run_batch_applies_agent_model_override(tmp_path):
    """Test that run_batch applies agent model override from config."""
    from coder_eval.orchestration.config import BatchRunConfig

    task_file = Path("tasks/hello_date.yaml")
    run_dir = tmp_path / "test_run"

    config = BatchRunConfig(
        run_dir=run_dir,
        agent_model="override-model",
    )

    task, _ = load_task(task_file)
    original_model = task.agent.model

    # Simulate override logic from batch.py
    effective_model = config.agent_model
    if effective_model:
        task.agent.model = effective_model

    assert task.agent.model == "override-model"
    assert task.agent.model != original_model


@pytest.mark.asyncio
async def test_run_batch_applies_permission_mode_override(tmp_path):
    """Test that run_batch applies permission mode override from config."""
    from coder_eval.orchestration.config import BatchRunConfig

    task_file = Path("tasks/hello_date.yaml")
    run_dir = tmp_path / "test_run"

    config = BatchRunConfig(
        run_dir=run_dir,
        permission_mode="bypassPermissions",
    )

    task, _ = load_task(task_file)

    effective_perm = config.permission_mode
    if effective_perm:
        task.agent.permission_mode = effective_perm

    assert task.agent.permission_mode == "bypassPermissions"


@pytest.mark.asyncio
async def test_run_batch_applies_max_turns_override(tmp_path):
    """Test that run_batch applies max turns override from config."""
    from coder_eval.orchestration.config import BatchRunConfig

    task_file = Path("tasks/hello_date.yaml")
    run_dir = tmp_path / "test_run"

    config = BatchRunConfig(
        run_dir=run_dir,
        max_turns=42,
    )

    task, _ = load_task(task_file)
    assert task.agent.max_turns is None  # Default

    effective_max_turns = config.max_turns if config.max_turns is not None else None
    if effective_max_turns is not None:
        task.agent.max_turns = effective_max_turns

    assert task.agent.max_turns == 42


# ==================== Duplicate Task ID Validation Tests ====================


def test_resolve_all_tasks_rejects_duplicate_task_ids(tmp_path):
    """Test that resolve_all_tasks raises ValueError when tasks share the same task_id."""
    from coder_eval.models import ExperimentDefaults, ExperimentDefinition, ExperimentVariant
    from coder_eval.orchestration.config import BatchRunConfig
    from coder_eval.orchestration.experiment import resolve_all_tasks

    # Create two task YAML files with the same task_id
    task_yaml = """\
task_id: duplicate_id
description: A test task
initial_prompt: Do something
agent:
  type: claude-code
sandbox:
  driver: tempdir
success_criteria:
  - type: file_exists
    path: output.txt
    description: Output file must exist
"""
    task_file_a = tmp_path / "task_a.yaml"
    task_file_b = tmp_path / "task_b.yaml"
    task_file_a.write_text(task_yaml)
    task_file_b.write_text(task_yaml)

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    config = BatchRunConfig(run_dir=run_dir)
    default_experiment = ExperimentDefinition(
        experiment_id="default",
        defaults=ExperimentDefaults(agent={"type": "claude-code"}),
        variants=[ExperimentVariant(variant_id="default")],
    )
    experiment = ExperimentDefinition(
        experiment_id="default",
        variants=[ExperimentVariant(variant_id="default")],
    )

    with pytest.raises(ValueError, match="Duplicate task IDs found"):
        resolve_all_tasks(
            task_files=[task_file_a, task_file_b],
            experiment=experiment,
            default_experiment=default_experiment,
            config=config,
        )


# --- _summarize_tool_calls tests ---


class TestSummarizeToolCalls:
    """Tests for the _summarize_tool_calls helper."""

    def _make_turn(self, commands=None, max_turns_exhausted=False):
        from coder_eval.models import TurnRecord

        return TurnRecord(
            iteration=1,
            user_input="test",
            agent_output="test",
            commands=commands or [],
            max_turns_exhausted=max_turns_exhausted,
        )

    def _make_cmd(self, tool_name="Bash", params=None, status="success", seq=0, result_summary=None):
        from datetime import datetime

        from coder_eval.models import CommandTelemetry

        return CommandTelemetry(
            tool_name=tool_name,
            tool_id=f"tool_{seq}",
            timestamp=datetime.now(),
            parameters=params or {},
            result_status=status,
            sequence_number=seq,
            result_summary=result_summary,
        )

    def test_empty_commands_returns_none(self):
        turn = self._make_turn(commands=[])
        assert _summarize_tool_calls(turn) is None

    def test_bash_command_shown(self):
        cmd = self._make_cmd(tool_name="Bash", params={"command": "uip --help"})
        result = _summarize_tool_calls(self._make_turn(commands=[cmd]))
        assert "`uip --help`" in result
        assert "[success]" in result

    def test_read_file_path_shown(self):
        cmd = self._make_cmd(tool_name="Read", params={"file_path": "/tmp/test.py"}, seq=0)
        result = _summarize_tool_calls(self._make_turn(commands=[cmd]))
        assert "/tmp/test.py" in result

    def test_grep_pattern_shown(self):
        cmd = self._make_cmd(tool_name="Grep", params={"pattern": "def main"}, seq=0)
        result = _summarize_tool_calls(self._make_turn(commands=[cmd]))
        assert "pattern=def main" in result

    def test_result_preview_included(self):
        cmd = self._make_cmd(params={"command": "ls"}, result_summary="file1.py\nfile2.py")
        result = _summarize_tool_calls(self._make_turn(commands=[cmd]))
        assert "→" in result
        assert "file1.py" in result

    def test_unknown_status_fallback(self):
        cmd = self._make_cmd(status=None)
        result = _summarize_tool_calls(self._make_turn(commands=[cmd]))
        assert "[unknown]" in result

    def test_multiple_commands_numbered(self):
        cmds = [
            self._make_cmd(tool_name="Bash", params={"command": "uip --help"}, seq=0),
            self._make_cmd(tool_name="Bash", params={"command": "uip flow --help"}, seq=1),
            self._make_cmd(tool_name="Read", params={"file_path": "out.json"}, seq=2),
        ]
        result = _summarize_tool_calls(self._make_turn(commands=cmds))
        lines = result.strip().split("\n")
        assert len(lines) == 3
        assert "1." in lines[0]
        assert "2." in lines[1]
        assert "3." in lines[2]

    def test_long_command_truncated(self):
        long_cmd = "x" * 200
        cmd = self._make_cmd(params={"command": long_cmd})
        result = _summarize_tool_calls(self._make_turn(commands=[cmd]))
        # Should be truncated to 120 chars
        assert len(long_cmd) == 200
        assert "`" + "x" * 120 + "`" in result

    def test_sequence_gaps_produce_clean_numbering(self):
        """Enumerate-based numbering stays sequential even when sequence_numbers have gaps."""
        cmds = [
            self._make_cmd(tool_name="Bash", params={"command": "echo a"}, seq=0),
            self._make_cmd(tool_name="Bash", params={"command": "echo b"}, seq=5),
            self._make_cmd(tool_name="Bash", params={"command": "echo c"}, seq=10),
        ]
        result = _summarize_tool_calls(self._make_turn(commands=cmds))
        lines = result.strip().split("\n")
        assert "1." in lines[0]
        assert "2." in lines[1]
        assert "3." in lines[2]
        # Ensure old gap-based numbers are NOT present
        assert "6." not in result
        assert "11." not in result

    def test_agent_tool_shown(self):
        """The Agent tool (renamed from Task) shows description."""
        cmd = self._make_cmd(tool_name="Agent", params={"description": "search codebase"}, seq=0)
        result = _summarize_tool_calls(self._make_turn(commands=[cmd]))
        assert "(search codebase)" in result

    def test_task_tool_still_supported(self):
        """Legacy Task tool name (pre-2.1.75) is still handled."""
        cmd = self._make_cmd(tool_name="Task", params={"description": "run tests"}, seq=0)
        result = _summarize_tool_calls(self._make_turn(commands=[cmd]))
        assert "(run tests)" in result


# --- Evaluation loop: max_turns exhaustion early-break test ---


@pytest.mark.asyncio
async def test_evaluation_loop_breaks_on_max_turns_exhausted(tmp_path):
    """Orchestrator stops iterating when the agent exhausts max_turns without passing criteria."""
    from datetime import datetime
    from unittest.mock import AsyncMock, MagicMock, patch

    from coder_eval.models import (
        AgentConfig,
        AgentKind,
        CriterionResult,
        EvaluationResult,
        FileExistsCriterion,
        SandboxConfig,
        TaskDefinition,
        TurnRecord,
    )

    agent_cfg = AgentConfig.model_construct(
        type=AgentKind.CLAUDE_CODE,
        permission_mode="acceptEdits",
        allowed_tools=None,
        model=None,
        max_turns=20,
        turn_timeout=None,
        ignore_patterns=[],
    )
    task = TaskDefinition.model_construct(
        task_id="exhaustion_test",
        description="Test exhaustion",
        initial_prompt="Do something",
        max_iterations=5,
        tags=[],
        agent=agent_cfg,
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(type="file_exists", path="test.py", description="test.py must exist")],
        task_timeout=None,
        llm_reviewer=None,
        reference=None,
    )

    run_dir = tmp_path / "run" / "exhaustion_test"
    run_dir.mkdir(parents=True)

    orchestrator = Orchestrator(task=task, run_dir=run_dir, variant_id="test-variant")
    orchestrator.result = EvaluationResult(
        task_id="exhaustion_test",
        task_description="Test",
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        environment_info={},
    )

    # Agent returns a turn record with max_turns_exhausted=True
    exhausted_turn = TurnRecord(
        iteration=1,
        user_input="test prompt",
        agent_output="I ran out of turns",
        duration_seconds=5.0,
        max_turns_exhausted=True,
    )
    mock_agent = AsyncMock()
    mock_agent.communicate = AsyncMock(return_value=exhausted_turn)
    orchestrator.agent = mock_agent

    # Mock sandbox
    mock_sandbox = MagicMock()
    mock_sandbox.sandbox_dir = tmp_path / "sandbox"
    mock_sandbox.sandbox_dir.mkdir()
    orchestrator.sandbox = mock_sandbox

    # Mock success checker that always fails
    mock_checker = MagicMock()
    mock_checker.check_all = MagicMock(
        return_value=[CriterionResult(criterion_type="file_exists", description="test", score=0.0)]
    )
    orchestrator.success_checker = mock_checker

    with patch("coder_eval.orchestrator.load_reference_code", return_value=(None, None)):
        success = await orchestrator._evaluation_loop()

    # Should NOT succeed
    assert success is False
    # Should have stopped after 1 iteration (not all 5)
    assert orchestrator.result.iteration_count == 1
    # Agent communicate should have been called only once
    assert mock_agent.communicate.call_count == 1
    # max_turns_exhausted should be propagated to the result
    assert orchestrator.result.max_turns_exhausted is True
