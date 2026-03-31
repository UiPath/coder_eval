"""Tests for LLM reviewer fallback to deterministic feedback.

Tests ensure evaluation continuity when LLM review is unavailable.
"""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from coder_eval.models import (
    AgentConfig,
    AgentKind,
    CriterionResult,
    EvaluationResult,
    FileExistsCriterion,
    LLMDecision,
    SandboxConfig,
    TaskDefinition,
)
from coder_eval.orchestration.evaluation import generate_next_prompt
from coder_eval.orchestrator import Orchestrator


@pytest.mark.asyncio
async def test_llm_reviewer_fallback_on_failure(tmp_path):
    """Test that orchestrator falls back to deterministic feedback when LLM returns None.

    Hypothesis: LLM failure should not block evaluation.
    Expected: Deterministic feedback contains criterion scores and thresholds.

    Context: Lines 414-436 in orchestrator.py implement fallback logic.
    """
    # Create task with multiple criteria
    task = TaskDefinition(
        task_id="test_task",
        description="Test task",
        initial_prompt="Create output.txt and result.txt",
        max_iterations=3,
        agent=AgentConfig(type="claude-code"),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[
            FileExistsCriterion(
                path="output.txt",
                description="Output file must exist",
                weight=1.0,
                pass_threshold=1.0,
            ),
            FileExistsCriterion(
                path="result.txt",
                description="Result file must exist",
                weight=1.0,
                pass_threshold=1.0,
            ),
        ],
    )

    orchestrator = Orchestrator(
        task=task,
        run_dir=tmp_path / "run",
        preserve_sandbox=False,
        task_file=tmp_path / "task.yaml",
        variant_id="test-variant",
    )

    # Mock LLM reviewer to return None (simulates failure)
    orchestrator.llm_reviewer = MagicMock()
    orchestrator.llm_reviewer.review = MagicMock(return_value=None)

    # Initialize result
    orchestrator.result = EvaluationResult(
        task_id="test_task",
        task_description="Test task",
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        success_criteria_results=[],
        turns=[],
        duration_seconds=0.0,
    )

    # Mock criteria results (both failed)
    criteria_results = [
        CriterionResult(
            criterion_type="file_exists",
            description="Output file must exist",
            score=0.0,  # Failed - file doesn't exist
            details="File not found",
        ),
        CriterionResult(
            criterion_type="file_exists",
            description="Result file must exist",
            score=0.0,  # Failed - file doesn't exist
            details="File not found",
        ),
    ]

    # Generate feedback
    feedback = await generate_next_prompt(
        task=task,
        agent_output="I tried to create the files",
        criteria_results=criteria_results,
        iteration=1,
        llm_reviewer=orchestrator.llm_reviewer,
        reference_code=None,
    )

    # Verify deterministic feedback is used
    assert "The following checks failed" in feedback
    assert "Output file must exist" in feedback
    assert "Result file must exist" in feedback
    assert "Score: 0.00" in feedback
    assert "threshold: 1.0" in feedback or "threshold: 1.00" in feedback
    assert "Please fix these issues" in feedback


@pytest.mark.asyncio
async def test_llm_reviewer_succeeds_when_available(tmp_path):
    """Test that LLM reviewer feedback is used when available.

    Hypothesis: LLM feedback should take precedence over deterministic.
    Expected: LLM issues and next_steps returned in prompt.
    """
    task = TaskDefinition(
        task_id="test_task",
        description="Test task",
        initial_prompt="Test",
        max_iterations=3,
        agent=AgentConfig(type="claude-code"),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[
            FileExistsCriterion(path="output.txt", description="Check output"),
        ],
    )

    orchestrator = Orchestrator(
        task=task,
        run_dir=tmp_path / "run",
        preserve_sandbox=False,
        task_file=tmp_path / "task.yaml",
        variant_id="test-variant",
    )

    # Mock LLM reviewer to return decision
    llm_decision = LLMDecision(
        issues="Missing error handling on line 42",
        score=0.6,
        next_steps=["Add try-except block", "Validate input"],
        should_continue=True,
    )

    orchestrator.llm_reviewer = MagicMock()
    orchestrator.llm_reviewer.review = MagicMock(return_value=llm_decision)

    orchestrator.result = EvaluationResult(
        task_id="test_task",
        task_description="Test task",
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        success_criteria_results=[],
        turns=[],
        duration_seconds=0.0,
    )

    criteria_results = [
        CriterionResult(
            criterion_type="file_exists",
            description="Check output",
            score=0.0,
        ),
    ]

    # Generate feedback
    feedback = await generate_next_prompt(
        task=task,
        agent_output="Created output",
        criteria_results=criteria_results,
        iteration=1,
        llm_reviewer=orchestrator.llm_reviewer,
        reference_code=None,
    )

    # Verify LLM feedback is used
    assert "Missing error handling on line 42" in feedback
    assert "Add try-except block" in feedback
    assert "Validate input" in feedback
    assert "Please address these issues" in feedback


@pytest.mark.asyncio
async def test_fallback_includes_criterion_details(tmp_path):
    """Test that deterministic fallback includes detailed error messages.

    Hypothesis: Fallback should provide actionable information.
    Expected: Feedback includes criterion details and errors.
    """
    task = TaskDefinition(
        task_id="test_task",
        description="Test task",
        initial_prompt="Test",
        max_iterations=3,
        agent=AgentConfig(type="claude-code"),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[
            FileExistsCriterion(
                path="output.txt",
                description="Create output file",
                pass_threshold=1.0,
            ),
        ],
    )

    orchestrator = Orchestrator(
        task=task,
        run_dir=tmp_path / "run",
        preserve_sandbox=False,
        task_file=tmp_path / "task.yaml",
        variant_id="test-variant",
    )

    # No LLM reviewer
    orchestrator.llm_reviewer = None

    orchestrator.result = EvaluationResult(
        task_id="test_task",
        task_description="Test task",
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        success_criteria_results=[],
        turns=[],
        duration_seconds=0.0,
    )

    # Criterion failed with error
    criteria_results = [
        CriterionResult(
            criterion_type="file_exists",
            description="Create output file",
            score=0.0,
            error="FileNotFoundError: /sandbox/output.txt not found",
        ),
    ]

    feedback = await generate_next_prompt(
        task=task,
        agent_output="Output",
        criteria_results=criteria_results,
        iteration=1,
        llm_reviewer=orchestrator.llm_reviewer,
        reference_code=None,
    )

    # Verify error details included
    assert "Create output file" in feedback
    assert "Error: FileNotFoundError" in feedback
    assert "Score: 0.00" in feedback


@pytest.mark.asyncio
async def test_fallback_with_partial_pass(tmp_path):
    """Test fallback feedback when some criteria pass and some fail.

    Hypothesis: Only failed criteria should appear in feedback.
    Expected: Feedback lists only failed criteria.
    """
    task = TaskDefinition(
        task_id="test_task",
        description="Test task",
        initial_prompt="Test",
        max_iterations=3,
        agent=AgentConfig(type="claude-code"),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[
            FileExistsCriterion(
                path="success.txt",
                description="This one passes",
                pass_threshold=1.0,
            ),
            FileExistsCriterion(
                path="failure.txt",
                description="This one fails",
                pass_threshold=1.0,
            ),
        ],
    )

    orchestrator = Orchestrator(
        task=task,
        run_dir=tmp_path / "run",
        preserve_sandbox=False,
        task_file=tmp_path / "task.yaml",
        variant_id="test-variant",
    )

    orchestrator.llm_reviewer = None

    orchestrator.result = EvaluationResult(
        task_id="test_task",
        task_description="Test task",
        variant_id="test-variant",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status="FAILURE",
        iteration_count=0,
        success_criteria_results=[],
        turns=[],
        duration_seconds=0.0,
    )

    criteria_results = [
        CriterionResult(
            criterion_type="file_exists",
            description="This one passes",
            score=1.0,  # Passed
        ),
        CriterionResult(
            criterion_type="file_exists",
            description="This one fails",
            score=0.0,  # Failed
            details="File not found",
        ),
    ]

    feedback = await generate_next_prompt(
        task=task,
        agent_output="Output",
        criteria_results=criteria_results,
        iteration=1,
        llm_reviewer=orchestrator.llm_reviewer,
        reference_code=None,
    )

    # Verify only failed criterion in feedback
    assert "This one fails" in feedback
    assert "This one passes" not in feedback
    assert "Score: 0.00" in feedback
