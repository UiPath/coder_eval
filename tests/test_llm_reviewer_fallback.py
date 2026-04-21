"""Tests for LLM reviewer fallback to deterministic feedback.

Tests ensure evaluation continuity when LLM review is unavailable.
"""

from coder_eval.models import (
    AgentConfig,
    CriterionResult,
    FileExistsCriterion,
    LLMDecision,
    SandboxConfig,
    TaskDefinition,
)
from coder_eval.orchestration.evaluation import generate_next_prompt


def test_llm_reviewer_fallback_on_failure():
    """When decision is None, deterministic feedback lists failed criteria.

    Hypothesis: LLM failure (decision=None) should not block evaluation.
    Expected: Deterministic feedback contains criterion scores and thresholds.
    """
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

    criteria_results = [
        CriterionResult(
            criterion_type="file_exists",
            description="Output file must exist",
            score=0.0,
            details="File not found",
        ),
        CriterionResult(
            criterion_type="file_exists",
            description="Result file must exist",
            score=0.0,
            details="File not found",
        ),
    ]

    feedback = generate_next_prompt(task=task, criteria_results=criteria_results, decision=None)

    assert "The following checks failed" in feedback
    assert "Output file must exist" in feedback
    assert "Result file must exist" in feedback
    assert "Score: 0.00" in feedback
    assert "threshold: 1.0" in feedback or "threshold: 1.00" in feedback
    assert "Please fix these issues" in feedback


def test_llm_reviewer_succeeds_when_available():
    """When decision is provided, LLM feedback is used instead of deterministic."""
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

    decision = LLMDecision(
        issues="Missing error handling on line 42",
        score=0.6,
        next_steps=["Add try-except block", "Validate input"],
        should_continue=True,
    )

    criteria_results = [
        CriterionResult(
            criterion_type="file_exists",
            description="Check output",
            score=0.0,
        ),
    ]

    feedback = generate_next_prompt(task=task, criteria_results=criteria_results, decision=decision)

    assert "Missing error handling on line 42" in feedback
    assert "Add try-except block" in feedback
    assert "Validate input" in feedback
    assert "Please address these issues" in feedback


def test_fallback_includes_criterion_details():
    """Deterministic fallback includes detailed error messages."""
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

    criteria_results = [
        CriterionResult(
            criterion_type="file_exists",
            description="Create output file",
            score=0.0,
            error="FileNotFoundError: /sandbox/output.txt not found",
        ),
    ]

    feedback = generate_next_prompt(task=task, criteria_results=criteria_results, decision=None)

    assert "Create output file" in feedback
    assert "Error: FileNotFoundError" in feedback
    assert "Score: 0.00" in feedback


def test_fallback_with_partial_pass():
    """Fallback lists only failed criteria when some pass and some fail."""
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

    criteria_results = [
        CriterionResult(
            criterion_type="file_exists",
            description="This one passes",
            score=1.0,
        ),
        CriterionResult(
            criterion_type="file_exists",
            description="This one fails",
            score=0.0,
            details="File not found",
        ),
    ]

    feedback = generate_next_prompt(task=task, criteria_results=criteria_results, decision=None)

    assert "This one fails" in feedback
    assert "This one passes" not in feedback
    assert "Score: 0.00" in feedback
