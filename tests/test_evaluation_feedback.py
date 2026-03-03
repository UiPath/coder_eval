"""Tests for evaluation feedback generation: LLM reviewer integration."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

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

    async def test_llm_issues_returned_even_without_next_steps(self):
        """LLM issues should be included in feedback even when next_steps is empty."""
        mock_reviewer = MagicMock()
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

        logger = logging.getLogger("coder_eval.test")

        with patch("coder_eval.orchestration.evaluation.execute_with_retry", new_callable=AsyncMock) as mock_retry:
            mock_retry.return_value = decision

            result = await generate_next_prompt(
                task=task,
                agent_output="some output",
                criteria_results=criteria_results,
                iteration=1,
                llm_reviewer=mock_reviewer,
                reference_code=None,
                logger=logger,
            )

        assert "missing error handling" in result, f"LLM issues discarded when next_steps is empty.\nGot:\n{result}"
