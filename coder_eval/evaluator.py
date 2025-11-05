"""Evaluators for checking task success and providing qualitative feedback."""

import json
import logging

from .criteria import BaseCriterion, CriterionRegistry, init_criteria
from .models import CriteriaResults, CriterionResult, LLMDecision, LLMReviewerConfig, SuccessCriteria, SuccessCriterion
from .sandbox import Sandbox


# Get module logger
logger = logging.getLogger(__name__)


class SuccessChecker:
    """Orchestrates criterion checking using registered checkers."""

    def __init__(
        self,
        sandbox: Sandbox,
        init_registry: bool = True,
        validate_registry: bool = True,
    ):
        """Initialize the success checker.

        Args:
            sandbox: Sandbox instance for running checks
            init_registry: Whether to initialize the criteria registry
            validate_registry: Whether to validate all expected types are registered
        """
        self.sandbox = sandbox
        self._checker_instances: dict[str, BaseCriterion] = {}
        # Cached reference code - automatically set by check()/check_all() when provided
        # Used by subsequent check() calls that don't explicitly pass reference_code
        self._reference_code: str | None = None

        # V3: Lazy initialization - registry loaded here, not at import
        if init_registry:
            init_criteria(validate=validate_registry)

    def check(
        self,
        criterion: SuccessCriterion,
        reference_code: str | None = None,
    ) -> CriterionResult:
        """Check a single criterion (backward compatibility wrapper).

        Args:
            criterion: Criterion definition
            reference_code: Optional reference code for comparison

        Returns:
            CriterionResult with score
        """
        # Persist reference_code for subsequent calls (backward compat)
        if reference_code is not None:
            self._reference_code = reference_code
        # Use instance variable if no reference_code provided
        ref_code = reference_code if reference_code is not None else self._reference_code
        return self._check_single(criterion, ref_code)

    def check_all(
        self,
        criteria: SuccessCriteria,
        reference_code: str | None = None,
    ) -> CriteriaResults:
        """Check all success criteria.

        Args:
            criteria: List of criterion definitions
            reference_code: Optional reference code for comparison

        Returns:
            List of criterion results with scores
        """
        # Persist reference_code for subsequent check() calls
        if reference_code is not None:
            self._reference_code = reference_code
        results = []
        for criterion in criteria:
            result = self._check_single(criterion, reference_code)
            results.append(result)
        return results

    def _get_checker_instance(self, criterion_type: str) -> BaseCriterion:
        """Get or create a checker instance (V3: cached).

        Args:
            criterion_type: The criterion type

        Returns:
            Checker instance (reused within this evaluation run)
        """
        if criterion_type not in self._checker_instances:
            checker_class = CriterionRegistry.get_checker(criterion_type)
            self._checker_instances[criterion_type] = checker_class()
        return self._checker_instances[criterion_type]

    def _check_single(
        self,
        criterion: SuccessCriterion,
        reference_code: str | None,
    ) -> CriterionResult:
        """Check a single criterion using registered checker.

        Args:
            criterion: Criterion definition (discriminated union)
            reference_code: Optional reference code

        Returns:
            CriterionResult with score
        """
        criterion_type = criterion.type

        # V3: Broader exception handling - catches checker constructor failures too
        try:
            # Get cached instance
            checker = self._get_checker_instance(criterion_type)
            result = checker.check(criterion, self.sandbox, reference_code)

            logger.info(f"Criterion '{criterion_type}' score: {result.score:.2f}")
            return result

        except KeyError:
            # No checker registered for this type - return failed result for consistency
            logger.error(f"No checker found for criterion type '{criterion_type}'")
            return CriterionResult(
                criterion_type=criterion_type,
                description=criterion.description,
                score=0.0,
                details=f"No checker registered for criterion type '{criterion_type}'",
                error=f"Unsupported criterion type: '{criterion_type}'",
            )
        except Exception as e:
            # V3: Catch ALL exceptions, including checker __init__ failures
            logger.exception(f"Checker failure for criterion '{criterion_type}': {e}")
            return CriterionResult(
                criterion_type=criterion_type,
                description=criterion.description,
                score=0.0,
                details="Error running checker",
                error=str(e),
            )


class LLMReviewer:
    """Qualitative evaluator using LLM Gateway for all models.

    Provides human-like feedback on code quality, approach, and
    suggests improvements beyond objective success criteria.

    All LLM calls are routed through UiPath LLM Gateway using LangChain
    integration, providing unified access to Anthropic, OpenAI, and other models.
    """

    def __init__(self, config: LLMReviewerConfig):
        """Initialize the LLM reviewer with Gateway client.

        Args:
            config: LLM reviewer configuration

        Raises:
            RuntimeError: If uipath_llmgw_client package is not installed
        """
        self.config = config

        if not config.enabled:
            return

        try:
            from uipath_llmgw_client.llmgw_langchain_client import LLMGatewayNormalizedChatModel
        except ImportError as e:
            raise RuntimeError(
                "uipath_llmgw_client is required for LLM reviewer. Install with: pip install uipath_llmgw_client"
            ) from e

        from .config import settings

        # Initialize LangChain-based Gateway client for all models
        self.llm = LLMGatewayNormalizedChatModel(
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            requesting_product=settings.llmgw_requesting_product,
            requesting_feature=settings.llmgw_requesting_feature,
        )

    def review(
        self,
        task_description: str,
        agent_output: str,
        current_iteration: int,
        max_iterations: int,
        reference_solution: str | None = None,
    ) -> LLMDecision | None:
        """Review the agent's work using LLM Gateway.

        Args:
            task_description: Description of the task
            agent_output: The agent's output/transcript
            current_iteration: Current iteration number
            max_iterations: Maximum allowed iterations
            reference_solution: Optional reference solution code for comparison

        Returns:
            LLM decision with assessment and suggestions, or None if review fails
        """
        if not self.config.enabled:
            return None

        prompt = self._build_review_prompt(
            task_description,
            agent_output,
            current_iteration,
            max_iterations,
            reference_solution,
        )

        # Log prompt in debug mode (visible with --verbose)
        logger.debug(f"LLM Review Prompt:\n{prompt}")

        try:
            response = self.llm.invoke(prompt)

            content = response.content
            if not isinstance(content, str):
                content = str(content)

            logger.debug(f"LLM Review Response:\n{content}")

            return self._parse_response(content)

        except Exception as e:
            logger.warning(f"LLM review failed: {e}")
            return None

    def _build_review_prompt(
        self,
        task_description: str,
        agent_output: str,
        current_iteration: int,
        max_iterations: int,
        reference_solution: str | None = None,
    ) -> str:
        """Build the review prompt for the LLM.

        Uses terse, developer-style code review language to generate
        direct, problem-focused feedback.

        Args:
            task_description: Description of the task
            agent_output: The agent's output
            current_iteration: Current iteration
            max_iterations: Maximum iterations
            reference_solution: Optional reference solution code for comparison

        Returns:
            Formatted prompt string
        """
        # Build reference section if provided (NEVER shown to agent!)
        reference_section = ""
        if reference_solution:
            reference_section = f"""
REFERENCE SOLUTION (for your review only - NOT visible to the agent):
```
{reference_solution}
```

Compare agent's approach to reference. Focus on correctness and completeness differences.

"""

        return f"""You are a code reviewer evaluating an agent's implementation.

TASK: {task_description}
{reference_section}
AGENT OUTPUT (Iteration {current_iteration}/{max_iterations}):
{agent_output}

Write a direct code review. Focus on what's wrong or needs improvement. No praise, no fluff.

Respond with ONLY valid JSON in this format:
{{
    "issues": "Direct critique. 1-2 sentences. What's broken or suboptimal?",
    "score": 0.7,
    "next_steps": ["Fix X", "Add Y", "Remove Z"],
    "should_continue": true
}}

Where:
- issues: Terse problem description. Skip "The agent..." phrasing.
- score: 0.0 (broken) to 1.0 (perfect)
- next_steps: Action-oriented imperatives (not "Consider..." or "Suggestion:")
- should_continue: true if more work needed, false if done/stuck

Examples of GOOD "issues" (terse, direct, problem-focused):
- "Script works but overcomplicated. Remove manual auth - SDK handles it."
- "Missing error handling on line 15. API call can fail."
- "Logic correct but inefficient. Use set instead of nested loops."
- "Incomplete. Missing file validation and edge case handling."

Examples of BAD "issues" (too verbose, praise-heavy):
- "The agent has made good progress and the approach is reasonable..."
- "Overall the implementation is solid, however there are a few suggestions..."
- "Great work on the main functionality! Consider adding..."

Examples of GOOD "next_steps":
- "Add try-except around API call"
- "Remove lines 10-15 (redundant)"
- "Refactor parseData() - too complex"

Examples of BAD "next_steps":
- "Consider adding error handling"
- "It might be good to simplify..."
- "Suggestion: the code could be refactored"

NOTE: Use temperature=0.0 for most deterministic, terse output.

JSON response:"""

    def _parse_response(self, response: str) -> LLMDecision | None:
        """Parse the LLM response into an LLMDecision.

        Args:
            response: Raw response from the LLM

        Returns:
            Parsed LLMDecision, or None if parsing fails
        """
        try:
            # Try to extract JSON from the response
            # Sometimes LLMs add extra text around the JSON
            response = response.strip()

            # Find JSON object boundaries
            start_idx = response.find("{")
            end_idx = response.rfind("}") + 1

            if start_idx == -1 or end_idx == 0:
                raise ValueError("No JSON object found in response")

            json_str = response[start_idx:end_idx]
            data = json.loads(json_str)

            return LLMDecision(**data)

        except Exception as e:
            logger.warning(f"Failed to parse LLM response: {e}")
            logger.debug(f"Response was: {response}")
            return None
