"""Tests for evaluation feedback generation: LLM reviewer integration."""

from coder_eval.models import (
    AgentConfig,
    AgentKind,
    CriterionResult,
    FileExistsCriterion,
    LLMDecision,
    SandboxConfig,
    TaskDefinition,
)
from coder_eval.orchestration.evaluation import generate_next_prompt


class TestLLMFeedbackWithEmptyNextSteps:
    """Verify LLM reviewer issues are returned even when next_steps is empty."""

    def test_llm_issues_returned_even_without_next_steps(self):
        """LLM issues should be included in feedback even when next_steps is empty."""
        decision = LLMDecision(
            issues="The function is missing error handling for edge cases",
            score=0.4,
            next_steps=[],
            should_continue=True,
        )

        task = TaskDefinition(
            task_id="test",
            description="Test task",
            initial_prompt="Do something",
            agent=AgentConfig(type=AgentKind.CLAUDE_CODE),
            sandbox=SandboxConfig(),
            success_criteria=[FileExistsCriterion(description="File exists", path="test.py")],
        )

        criteria_results = [CriterionResult(criterion_type="file_exists", description="File exists", score=0.5)]

        result = generate_next_prompt(task=task, criteria_results=criteria_results, decision=decision)

        assert "missing error handling" in result, f"LLM issues discarded when next_steps is empty.\nGot:\n{result}"


def test_generate_next_prompt_uses_decision():
    """A non-None decision with next_steps is rendered as the Issues/Next steps block."""
    decision = LLMDecision(
        issues="Score logic is off by one",
        score=0.3,
        next_steps=["Fix iteration", "Add test"],
        should_continue=True,
    )
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        agent=AgentConfig(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(),
        success_criteria=[FileExistsCriterion(description="f", path="a.py")],
    )
    criteria_results = [CriterionResult(criterion_type="file_exists", description="f", score=0.0)]

    result = generate_next_prompt(task=task, criteria_results=criteria_results, decision=decision)

    assert "Issues:" in result
    assert "Score logic is off by one" in result
    assert "Next steps:" in result
    assert "- Fix iteration" in result
    assert "- Add test" in result
