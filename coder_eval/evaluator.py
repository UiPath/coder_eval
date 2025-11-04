"""Evaluators for checking task success and providing qualitative feedback."""

import functools
import json
import logging
import re
from typing import Any

from .models import (
    CodeLintsCriterion,
    CriteriaResults,
    CriterionResult,
    FileContainsCriterion,
    FileExistsCriterion,
    FileMatchesRegexCriterion,
    LLMDecision,
    LLMReviewerConfig,
    ProgramStdoutEqualsCriterion,
    PylintScoreCriterion,
    PytestCriterion,
    ReferenceComparisonCriterion,
    RunCommandCriterion,
    SuccessCriteria,
    SuccessCriterion,
)
from .sandbox import Sandbox
from .scorers import ComplexityScorer, SimilarityScorer


# Get module logger
logger = logging.getLogger(__name__)

PYTEST_IMPERFECT_SCORE_CAP = 0.99  # Max score when exit code != 0 (prevents perfect score on failures)


def handle_criterion_errors(func: Any) -> Any:
    """Decorator to handle exceptions in criterion checker methods.

    Wraps _check_* methods to catch exceptions and return a failed
    CriterionResult with error details instead of propagating the exception.

    This eliminates duplicate try/except blocks across all checkers while
    ensuring consistent error handling and logging.

    Args:
        func: The checker method to wrap

    Returns:
        Wrapped function that handles exceptions gracefully
    """

    @functools.wraps(func)
    def wrapper(self: Any, criterion: SuccessCriterion) -> CriterionResult:
        try:
            return func(self, criterion)
        except Exception as e:
            # Log the exception for debugging
            logger.warning(f"Criterion check '{criterion.type}' failed with exception: {e}", exc_info=True)
            # Return failed result with error details
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                error=str(e),
            )

    return wrapper


class SuccessChecker:
    """Objective evaluator that checks success criteria.

    Runs deterministic checks like file existence, command execution,
    and test results to determine if a task was completed successfully.

    This class is responsible for ALL criterion evaluation logic.
    Models are pure data containers; this class implements the business logic.
    """

    def __init__(self, sandbox: Sandbox):
        """Initialize the success checker.

        Args:
            sandbox: Sandbox instance to check against
        """
        self.sandbox = sandbox

        # Dispatch table for criterion checking
        # Maps criterion type string to checker method
        # Note: All checker methods are decorated with @handle_criterion_errors
        # for consistent exception handling
        self._checkers = {
            "file_exists": self._check_file_exists,
            "file_contains": self._check_file_contains,
            "run_command": self._check_run_command,
            "program_stdout_equals": self._check_program_stdout,
            "pytest": self._check_pytest,
            "file_matches_regex": self._check_file_matches_regex,
            "code_lints": self._check_code_lints,
            "pylint_score": self._check_pylint_score,
            "reference_comparison": self._check_reference_comparison,
        }

        # Reference code cache (set by check_all)
        self._reference_code: str | None = None

    def check_all(
        self,
        criteria: SuccessCriteria,
        reference_code: str | None = None,
    ) -> CriteriaResults:
        """Check all success criteria.

        Args:
            criteria: List of criteria to check
            reference_code: Optional reference solution code for reference_comparison criterion

        Returns:
            List of results for each criterion
        """
        # Store reference code for use by _check_reference_comparison
        self._reference_code = reference_code
        return [self.check(criterion) for criterion in criteria]

    def check(self, criterion: SuccessCriterion) -> CriterionResult:
        """Dispatch to appropriate checker based on criterion type.

        Args:
            criterion: Criterion to check

        Returns:
            Result of the criterion check

        Raises:
            TypeError: If criterion type is not supported
        """
        checker = self._checkers.get(criterion.type)
        if checker is None:
            raise TypeError(
                f"Unsupported criterion type: {criterion.type}. Supported types: {', '.join(self._checkers.keys())}"
            )
        return checker(criterion)

    @handle_criterion_errors
    def _check_file_exists(self, criterion: FileExistsCriterion) -> CriterionResult:
        """Check if the file exists in the sandbox.

        Args:
            criterion: File existence criterion

        Returns:
            Result with binary score (1.0 if exists, 0.0 if not)
        """
        exists = self.sandbox.file_exists(criterion.path)
        score = 1.0 if exists else 0.0

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=f"File '{criterion.path}' {'exists' if exists else 'does not exist'}",
        )

    @handle_criterion_errors
    def _check_file_contains(self, criterion: FileContainsCriterion) -> CriterionResult:
        """Check if file contains required strings and excludes forbidden ones.

        Args:
            criterion: File contains criterion

        Returns:
            Result with fractional score based on includes/excludes matched
        """
        if not self.sandbox.file_exists(criterion.path):
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                error=f"File '{criterion.path}' does not exist",
            )

        content = self.sandbox.get_file_content(criterion.path)

        # Calculate includes score (fraction of includes found)
        includes_found = sum(1 for inc in criterion.includes if inc in content)
        includes_total = len(criterion.includes)
        includes_score = includes_found / includes_total if includes_total > 0 else 1.0

        # Calculate excludes score (fraction of excludes absent)
        if criterion.excludes:
            excludes_found = sum(1 for exc in criterion.excludes if exc in content)
            excludes_total = len(criterion.excludes)
            excludes_score = 1.0 - (excludes_found / excludes_total)
        else:
            excludes_score = 1.0

        # Combined score: average of includes and excludes
        score = (includes_score + excludes_score) / 2.0

        # Build details
        details_parts = []
        details_parts.append(f"Includes: {includes_found}/{includes_total} found")
        if criterion.excludes:
            excludes_absent = len(criterion.excludes) - sum(1 for exc in criterion.excludes if exc in content)
            details_parts.append(f"Excludes: {excludes_absent}/{len(criterion.excludes)} absent")
        details_parts.append(f"Score: {score:.2f}")

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details="; ".join(details_parts),
        )

    @handle_criterion_errors
    def _check_run_command(self, criterion: RunCommandCriterion) -> CriterionResult:
        """Execute command and check exit code.

        Args:
            criterion: Run command criterion

        Returns:
            Result with binary score (1.0 if exit code matches, 0.0 if not)
        """
        logger.debug(f"Running command for criterion '{criterion.description}': {criterion.command}")
        exit_code, stdout, stderr = self.sandbox.run_command(criterion.command, timeout=criterion.timeout)

        score = 1.0 if exit_code == criterion.expected_exit_code else 0.0

        details = f"Exit code: {exit_code} (expected: {criterion.expected_exit_code})"
        if stdout:
            details += f"\nStdout: {stdout[:200]}"  # Truncate long output
        if stderr:
            details += f"\nStderr: {stderr[:200]}"

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=details,
        )

    @handle_criterion_errors
    def _check_program_stdout(self, criterion: ProgramStdoutEqualsCriterion) -> CriterionResult:
        """Execute command and compare output.

        Args:
            criterion: Program stdout criterion

        Returns:
            Result with binary score (1.0 if exact match and exit 0, 0.0 otherwise)
        """
        logger.debug(f"Running command for criterion '{criterion.description}': {criterion.command}")
        exit_code, stdout, _stderr = self.sandbox.run_command(criterion.command, timeout=criterion.timeout)

        stdout_stripped = stdout.strip()
        expected_stripped = criterion.expected_output.strip()

        score = 1.0 if (stdout_stripped == expected_stripped and exit_code == 0) else 0.0

        details = f"Exit code: {exit_code}\n"
        details += f"Expected: {expected_stripped}\n"
        details += f"Got: {stdout_stripped}"

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=details,
        )

    @handle_criterion_errors
    def _check_pytest(self, criterion: PytestCriterion) -> CriterionResult:
        """Run pytest and check results.

        Args:
            criterion: Pytest criterion

        Returns:
            Result with fractional score (tests_passed / tests_total)
        """
        # Build pytest command
        cmd_parts = ["pytest", criterion.path, *criterion.args]
        command = " ".join(cmd_parts)
        logger.debug(f"Running pytest for criterion '{criterion.description}': {command}")

        exit_code, stdout, stderr = self.sandbox.run_command(command, timeout=criterion.timeout)

        # Combine stdout and stderr (pytest may write to either stream)
        combined_output = (stdout or "") + "\n" + (stderr or "")

        # Helper to extract counts from output
        def _extract_count(pattern: str) -> int:
            match = re.search(pattern, combined_output, re.IGNORECASE)
            return int(match.group(1)) if match else 0

        # Parse all test result categories
        passed = _extract_count(r"(\d+)\s+passed")
        failed = _extract_count(r"(\d+)\s+failed")
        errors = _extract_count(r"(\d+)\s+errors?")
        skipped = _extract_count(r"(\d+)\s+skipped")
        collected = _extract_count(r"collected\s+(\d+)\s+items?")

        # Calculate score
        if collected == 0:
            # No tests collected is a failure (likely wrong path or no test files)
            score = 0.0
            details = f"Exit code: {exit_code}, Pytest: No tests collected (score: 0.00)\n"
        elif passed + failed + errors == 0:
            # Tests collected but none ran (possibly all skipped)
            score = 0.0
            details = (
                f"Exit code: {exit_code}, Pytest: {collected} collected, {skipped} skipped, none ran (score: 0.00)\n"
            )
        else:
            # Normal case: calculate score from passed/failed/errors
            total_run = passed + failed + errors
            score = passed / total_run if total_run > 0 else 0.0

            # Never give perfect score if pytest exited non-zero
            if exit_code != 0 and score == 1.0:
                score = PYTEST_IMPERFECT_SCORE_CAP

            details = f"Exit code: {exit_code}, Pytest: {passed} passed, {failed} failed, {errors} errors"
            if skipped > 0:
                details += f", {skipped} skipped"
            details += f" out of {collected} collected (score: {score:.2f})\n"

        # Extract summary line from output for additional context
        for line in combined_output.split("\n"):
            if "passed" in line or "failed" in line or "error" in line:
                details += line + "\n"
                break

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=details,
        )

    @handle_criterion_errors
    def _check_file_matches_regex(self, criterion: FileMatchesRegexCriterion) -> CriterionResult:
        """Check if file content matches the regex pattern.

        Args:
            criterion: File matches regex criterion

        Returns:
            Result with binary score (1.0 if pattern matches as expected, 0.0 otherwise)
        """
        if not self.sandbox.file_exists(criterion.path):
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                error=f"File '{criterion.path}' does not exist",
            )

        content = self.sandbox.get_file_content(criterion.path)

        # Compile and search for pattern
        try:
            regex = re.compile(criterion.pattern, criterion.flags)
            match = regex.search(content)
        except re.error as e:
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                error=f"Invalid regex pattern: {e}",
            )

        # Check based on must_match flag
        if criterion.must_match:
            score = 1.0 if match is not None else 0.0
            if match:
                matched_text = match.group()[:100]
                details = f"Pattern '{criterion.pattern}' found in file (matched: '{matched_text}')"
            else:
                details = f"Pattern '{criterion.pattern}' not found in file"
        else:
            score = 1.0 if match is None else 0.0
            if match is None:
                details = f"Pattern '{criterion.pattern}' correctly absent from file"
            else:
                matched_text = match.group()[:100]
                details = f"Pattern '{criterion.pattern}' found but should not be present (matched: '{matched_text}')"

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=details,
        )

    @handle_criterion_errors
    def _check_code_lints(self, criterion: CodeLintsCriterion) -> CriterionResult:
        """Run linter and check results.

        Args:
            criterion: Code lints criterion

        Returns:
            Result with binary score (1.0 if linter passes, 0.0 if errors/warnings)
        """
        # Build linter command
        cmd_parts = [criterion.linter, criterion.path, *criterion.args]
        command = " ".join(cmd_parts)
        logger.debug(f"Running linter for criterion '{criterion.description}': {command}")

        exit_code, stdout, stderr = self.sandbox.run_command(command, timeout=criterion.timeout)

        # Most linters return 0 on success, non-zero on issues
        # Some linters (like ruff) return different codes for errors vs warnings
        # If allow_warnings: 0 = clean, 1 = warnings only; else strict mode
        score = (1.0 if exit_code in (0, 1) else 0.0) if criterion.allow_warnings else 1.0 if exit_code == 0 else 0.0

        # Build details from output
        details = f"Exit code: {exit_code} (score: {score:.2f})\n"

        # Include relevant output (truncate if too long)
        output = stdout if stdout else stderr
        if output:
            lines = output.strip().split("\n")
            if len(lines) <= 10:
                details += output
            else:
                # Show first few and last few lines
                details += "\n".join(lines[:5])
                details += f"\n... ({len(lines) - 10} more lines) ...\n"
                details += "\n".join(lines[-5:])
        else:
            details += "No linter output (clean)"

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=details,
        )

    @handle_criterion_errors
    def _check_pylint_score(self, criterion: PylintScoreCriterion) -> CriterionResult:
        """Run pylint and extract quality score.

        Pylint output format:
            -------------------------------------------------------------------
            Your code has been rated at 8.75/10 (previous run: 8.50/10, +0.25)

        Args:
            criterion: Pylint score criterion

        Returns:
            Result with continuous score (0.0-1.0) normalized from pylint's 0-10 scale
        """
        # Build pylint command
        cmd_parts = ["pylint", criterion.path]

        # Add optional rcfile
        if criterion.rcfile:
            cmd_parts.extend(["--rcfile", criterion.rcfile])

        # Add optional fail-under (makes pylint exit non-zero below threshold)
        if criterion.fail_under is not None:
            cmd_parts.extend(["--fail-under", str(criterion.fail_under)])

        # Add any additional arguments
        cmd_parts.extend(criterion.args)

        command = " ".join(cmd_parts)
        logger.debug(f"Running pylint for criterion '{criterion.description}': {command}")

        # Execute pylint in sandbox
        exit_code, stdout, stderr = self.sandbox.run_command(command, timeout=criterion.timeout)

        # Parse pylint output for score
        score_result = self._parse_pylint_output(stdout, stderr)

        if score_result is None:
            # Pylint ran but no score found - treat as error
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                error="Could not parse pylint score from output",
            )

        pylint_score, details_text = score_result

        # Normalize score to 0.0-1.0 (clamp to 0.0 minimum for negative scores)
        normalized_score = max(0.0, pylint_score / 10.0)

        # Determine threshold (min_score takes precedence for clarity)
        if criterion.min_score is not None:
            threshold_text = f"min_score={criterion.min_score}/10"
        else:
            threshold_text = f"pass_threshold={criterion.pass_threshold}"

        # Build comprehensive details
        details = f"Pylint score: {pylint_score:.2f}/10 (normalized: {normalized_score:.3f})\n"
        details += f"Threshold: {threshold_text}\n"
        details += f"Exit code: {exit_code}\n"
        details += f"\n{details_text}"

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=normalized_score,
            details=details,
        )

    def _parse_pylint_output(self, stdout: str, stderr: str) -> tuple[float, str] | None:
        """Parse pylint output to extract score and details.

        Args:
            stdout: Standard output from pylint
            stderr: Standard error from pylint

        Returns:
            Tuple of (score, details_text) or None if parsing fails
        """
        # Combine output (pylint may write to stderr)
        output = stdout + "\n" + stderr

        # Pylint always outputs scores in the format "Your code has been rated at X.XX/10"
        # This is the standard pylint output format and is unlikely to change
        # Pattern breakdown (Issue 3 fix - supports negative scores):
        #   (-?\d+(?:\.\d+)?) - Captures score with optional minus and decimal
        #     -?              - Optional minus sign for negative scores
        #     \d+             - One or more digits (integer part)
        #     (?:\.\d+)?      - Optional decimal part (non-capturing group)
        #   /10               - Literal "/10" suffix from pylint output
        # Matches: "-1.50/10", "0.00/10", "8/10", "9.75/10"
        score_pattern = r"Your code has been rated at (-?\d+(?:\.\d+)?)/10"

        match = re.search(score_pattern, output)

        if not match:
            return None

        score = float(match.group(1))

        # Extract summary section (everything after the score line)
        score_line_idx = output.find(match.group(0))
        summary = output[score_line_idx:].strip()

        # Truncate if too long (keep first 500 chars)
        if len(summary) > 500:
            summary = summary[:500] + "\n... (truncated)"

        return score, summary

    @handle_criterion_errors
    def _check_reference_comparison(self, criterion: ReferenceComparisonCriterion) -> CriterionResult:
        """Compare agent code against reference solution.

        Uses the reference code passed to check_all() and compares it with
        the agent's generated file using the specified comparison method.

        Args:
            criterion: Reference comparison criterion

        Returns:
            Result with similarity score [0.0, 1.0]
        """
        # Check that reference code was provided
        if not self._reference_code:
            return CriterionResult(
                criterion_type="reference_comparison",
                description=criterion.description,
                score=0.0,
                error="No reference code provided (task.reference not set)",
            )

        # Check sandbox is initialized
        if not self.sandbox.sandbox_dir:
            return CriterionResult(
                criterion_type="reference_comparison",
                description=criterion.description,
                score=0.0,
                error="Sandbox not initialized",
            )

        # Load agent code
        agent_path = self.sandbox.sandbox_dir / criterion.agent_file
        if not agent_path.exists():
            return CriterionResult(
                criterion_type="reference_comparison",
                description=criterion.description,
                score=0.0,
                error=f"Agent file not found: {criterion.agent_file}",
            )

        try:
            agent_code = agent_path.read_text()
        except Exception as e:
            return CriterionResult(
                criterion_type="reference_comparison",
                description=criterion.description,
                score=0.0,
                error=f"Failed to read agent file: {e}",
            )

        # Compare using specified method
        try:
            similarity_scorer = SimilarityScorer()

            if criterion.comparison_method == "ast":
                score = similarity_scorer.score_ast_similarity(agent_code, self._reference_code)
            elif criterion.comparison_method == "token":
                score = similarity_scorer.score_token_similarity(agent_code, self._reference_code)
            elif criterion.comparison_method == "complexity":
                complexity_scorer = ComplexityScorer()
                metrics = complexity_scorer.score_complexity(agent_code, self._reference_code, {})
                score = metrics["scores"]["overall_complexity"]
            else:
                return CriterionResult(
                    criterion_type="reference_comparison",
                    description=criterion.description,
                    score=0.0,
                    error=f"Unknown comparison method: {criterion.comparison_method}",
                )

            details = (
                f"Comparison method: {criterion.comparison_method}\n"
                f"Similarity: {score:.3f}\n"
                f"Threshold: {criterion.similarity_threshold:.3f}"
            )

            return CriterionResult(
                criterion_type="reference_comparison",
                description=criterion.description,
                score=score,
                details=details,
            )

        except Exception as e:
            return CriterionResult(
                criterion_type="reference_comparison",
                description=criterion.description,
                score=0.0,
                error=f"Comparison failed: {e}",
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
