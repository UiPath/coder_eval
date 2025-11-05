"""Error categorization, retry logic, and error context capture.

This module provides production-grade error handling with:
- 20 error categories with retry eligibility
- Exponential backoff retry logic with jitter
- Rich error context capture for debugging
- Actionable error tips for users
"""

import asyncio
import logging
import random
import traceback
from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)


class ErrorCategory(Enum):
    """Categorize errors for retry logic and reporting.

    Categories are grouped by component:
    - Agent errors (AGENT_*)
    - Sandbox errors (SANDBOX_*, VENV_*, PACKAGE_*, etc.)
    - Evaluator errors (CRITERION_*, LLM_REVIEWER_*)
    - Task errors (TASK_*)
    - System errors (DISK_FULL, OUT_OF_MEMORY)

    Each category has an associated retry policy in RETRY_CONFIG.
    """

    # Agent Errors - Communication and execution errors
    AGENT_TIMEOUT = "agent_timeout"  # Agent exceeded time limit (NOT retryable)
    AGENT_API_ERROR = "agent_api_error"  # API connection/network error (retryable)
    AGENT_RATE_LIMIT = "agent_rate_limit"  # API rate limit (retryable with long delay)
    AGENT_AUTH_ERROR = "agent_auth_error"  # Invalid API key (NOT retryable)
    AGENT_CRASH = "agent_crash"  # Agent process crashed (NOT retryable)
    AGENT_INVALID_OUTPUT = "agent_invalid_output"  # Malformed response (NOT retryable)

    # Sandbox Errors - Environment setup and execution
    SANDBOX_SETUP_ERROR = "sandbox_setup_error"  # Failed to create sandbox (retryable)
    SANDBOX_COMMAND_ERROR = "sandbox_command_error"  # Command execution failed (retryable)
    VENV_CREATION_ERROR = "venv_creation_error"  # Virtual env creation failed (retryable)
    PACKAGE_INSTALL_ERROR = "package_install_error"  # pip install failed (retryable)
    TEMPLATE_COPY_ERROR = "template_copy_error"  # Template copy failed (retryable)
    GIT_CLONE_ERROR = "git_clone_error"  # Git clone failed (retryable)

    # Criterion/Evaluator Errors
    CRITERION_CHECK_ERROR = "criterion_check_error"  # Error checking criterion (NOT retryable)
    LLM_REVIEWER_ERROR = "llm_reviewer_error"  # LLM reviewer failed (retryable)

    # Task Errors - Task definition and loading
    TASK_NOT_FOUND = "task_not_found"  # Task file doesn't exist (NOT retryable)
    TASK_INVALID = "task_invalid"  # Malformed task.yaml (NOT retryable)
    TESTS_FAILED = "tests_failed"  # Tests failed (expected, NOT retryable)

    # System Errors - Resource exhaustion
    DISK_FULL = "disk_full"  # No disk space (NOT retryable)
    OUT_OF_MEMORY = "out_of_memory"  # OOM error (NOT retryable)

    # Unknown
    UNKNOWN = "unknown"  # Uncategorized error (NOT retryable)


class RetryConfig(BaseModel):
    """Configuration for retry behavior.

    Defines how many times to retry and with what backoff strategy.
    """

    max_retries: int = Field(default=0, ge=0, description="Maximum retry attempts (0 = no retry)")
    backoff_multiplier: float = Field(default=2.0, gt=0, description="Exponential backoff multiplier")
    initial_delay: float = Field(default=5.0, gt=0, description="Initial delay in seconds")


class ErrorContext(BaseModel):
    """Structured error context for reporting and debugging.

    This model provides type-safe error context with validation.
    Use error_context_to_dict() to convert to dict for EvaluationResult.error_details.
    """

    error_category: str = Field(description="Error category value")
    error_message: str = Field(description="Exception message")
    stack_trace: str = Field(description="Full stack trace")
    task_id: str = Field(description="Task identifier")
    component: str | None = Field(default=None, description="Component that failed")
    agent_name: str | None = Field(default=None, description="Agent name if applicable")
    attempt_number: int = Field(ge=1, description="Attempt number (1-indexed)")
    is_retryable: bool = Field(description="Whether error is retryable")
    retry_delay_seconds: float = Field(ge=0.0, description="Delay before next retry")
    disk_usage_gb: float | None = Field(default=None, description="Disk usage in GB")
    memory_usage_gb: float | None = Field(default=None, description="Memory usage in GB")
    agent_stdout: str | None = Field(default=None, description="Agent stdout (truncated)")
    agent_stderr: str | None = Field(default=None, description="Agent stderr (truncated)")
    timestamp: str = Field(description="ISO timestamp when error occurred")


# Retry configuration per error category
# Non-retryable errors have max_retries=0 (default)
RETRY_CONFIG: dict[ErrorCategory, RetryConfig] = {
    # Agent errors - API issues are retryable
    ErrorCategory.AGENT_API_ERROR: RetryConfig(
        max_retries=3,
        backoff_multiplier=2.0,
        initial_delay=5.0,
    ),
    ErrorCategory.AGENT_RATE_LIMIT: RetryConfig(
        max_retries=5,
        backoff_multiplier=3.0,
        initial_delay=60.0,  # 1 minute initial delay
    ),
    # Sandbox errors - transient failures are retryable
    ErrorCategory.SANDBOX_SETUP_ERROR: RetryConfig(
        max_retries=2,
        backoff_multiplier=1.5,
        initial_delay=10.0,
    ),
    ErrorCategory.SANDBOX_COMMAND_ERROR: RetryConfig(
        max_retries=2,
        backoff_multiplier=1.5,
        initial_delay=10.0,
    ),
    ErrorCategory.PACKAGE_INSTALL_ERROR: RetryConfig(
        max_retries=2,
        backoff_multiplier=2.0,
        initial_delay=5.0,
    ),
    ErrorCategory.VENV_CREATION_ERROR: RetryConfig(
        max_retries=2,
        backoff_multiplier=2.0,
        initial_delay=5.0,
    ),
    ErrorCategory.TEMPLATE_COPY_ERROR: RetryConfig(
        max_retries=2,
        backoff_multiplier=1.5,
        initial_delay=5.0,
    ),
    ErrorCategory.GIT_CLONE_ERROR: RetryConfig(
        max_retries=3,
        backoff_multiplier=2.0,
        initial_delay=10.0,
    ),
    # LLM Reviewer errors
    ErrorCategory.LLM_REVIEWER_ERROR: RetryConfig(
        max_retries=2,
        backoff_multiplier=2.0,
        initial_delay=5.0,
    ),
    # All other errors are NOT retryable (max_retries=0 by default)
}


# Error tip system for better UX
ERROR_TIPS = {
    ErrorCategory.AGENT_RATE_LIMIT: (
        "Rate limit hit. Consider using --max-parallel=1 to reduce concurrent API calls, "
        "or check your API key quota at your provider's dashboard."
    ),
    ErrorCategory.AGENT_AUTH_ERROR: (
        "Authentication failed. Verify your ANTHROPIC_API_KEY is set correctly in .env file and has not expired."
    ),
    ErrorCategory.PACKAGE_INSTALL_ERROR: (
        "Package installation failed. Check your network connection, or try clearing pip cache with 'pip cache purge'."
    ),
    ErrorCategory.DISK_FULL: (
        "Disk full. Run 'coder-eval health' to check usage, then 'coder-eval gc' to free space, "
        "or delete old runs manually from the runs/ directory."
    ),
    ErrorCategory.VENV_CREATION_ERROR: (
        "Virtual environment creation failed. Ensure Python 3.13+ is installed and accessible."
    ),
    ErrorCategory.GIT_CLONE_ERROR: (
        "Git clone failed. Check your network connection and verify the repository URL is accessible."
    ),
    ErrorCategory.AGENT_API_ERROR: (
        "API connection error. Check your network connection and verify the API endpoint is accessible."
    ),
}


def should_retry(category: ErrorCategory, attempt: int) -> bool:
    """Determine if an error should be retried.

    Args:
        category: Error category
        attempt: Current attempt number (0-indexed)

    Returns:
        True if retry should be attempted
    """
    config = RETRY_CONFIG.get(category, RetryConfig())
    return attempt < config.max_retries


def get_retry_delay(category: ErrorCategory, attempt: int) -> float:
    """Calculate delay before retry with exponential backoff and jitter.

    Formula: (initial_delay * backoff_multiplier^attempt) + jitter
    Jitter: Random value between 0 and 25% of base delay

    Args:
        category: Error category
        attempt: Current attempt number (0-indexed)

    Returns:
        Delay in seconds

    Example:
        >>> get_retry_delay(ErrorCategory.AGENT_API_ERROR, 0)  # doctest: +SKIP
        5.2  # 5.0 base + 0.2 jitter
        >>> get_retry_delay(ErrorCategory.AGENT_API_ERROR, 1)  # doctest: +SKIP
        10.8  # 10.0 base + 0.8 jitter
    """
    config = RETRY_CONFIG.get(category, RetryConfig())
    base_delay = config.initial_delay * (config.backoff_multiplier**attempt)

    # Add jitter: up to 25% of base delay to prevent thundering herd
    jitter = random.uniform(0, base_delay * 0.25)

    return base_delay + jitter


def get_error_tip(category: ErrorCategory) -> str:
    """Get actionable tip for error category.

    Args:
        category: Error category

    Returns:
        Actionable tip string
    """
    return ERROR_TIPS.get(category, "Check logs for details or run with --verbose for more information.")


def categorize_error(
    error: Exception,
    context: dict[str, Any],
    hint: ErrorCategory | None = None,
) -> ErrorCategory:
    """Categorize an exception into an ErrorCategory.

    Uses pattern matching on error message and exception type,
    plus context hints (component name) to determine category.

    Prioritizes:
    1. Explicit hint (if provided by caller who knows the context)
    2. Exception type checking (most reliable)
    3. Known SDK exceptions (if available)
    4. String pattern matching (fallback)

    Args:
        error: The exception to categorize
        context: Additional context dict with keys:
            - component: "agent", "sandbox", "evaluator", etc.
            - task_id: Task identifier (for logging)
        hint: Optional explicit category (when caller knows the type)

    Returns:
        ErrorCategory enum value

    Example:
        >>> categorize_error(TimeoutError(), {"component": "agent"})
        <ErrorCategory.AGENT_TIMEOUT: 'agent_timeout'>
        >>> categorize_error(Exception("Rate limit"), {"component": "agent"})
        <ErrorCategory.AGENT_RATE_LIMIT: 'agent_rate_limit'>
    """
    # Use hint if provided (caller knows best)
    if hint:
        return hint

    component = context.get("component", "")

    # 1. Check specific exception types first (most reliable)
    if isinstance(error, TimeoutError):
        if component == "agent":
            return ErrorCategory.AGENT_TIMEOUT
        if component == "evaluator":
            return ErrorCategory.LLM_REVIEWER_ERROR
        return ErrorCategory.UNKNOWN

    if isinstance(error, FileNotFoundError):
        if "task" in str(error).lower() or "yaml" in str(error).lower():
            return ErrorCategory.TASK_NOT_FOUND
        return ErrorCategory.UNKNOWN

    if isinstance(error, MemoryError):
        return ErrorCategory.OUT_OF_MEMORY

    # 2. Check for known SDK exceptions (if available)
    # Note: Uncomment when anthropic SDK exceptions are available
    # try:
    #     from anthropic import RateLimitError, AuthenticationError
    #     if isinstance(error, RateLimitError):
    #         return ErrorCategory.AGENT_RATE_LIMIT
    #     if isinstance(error, AuthenticationError):
    #         return ErrorCategory.AGENT_AUTH_ERROR
    # except ImportError:
    #     pass

    # 3. Fallback to string matching
    error_str = str(error).lower()

    # Authentication errors
    if any(pat in error_str for pat in ["authentication", "unauthorized", "invalid api key", "401"]):
        return ErrorCategory.AGENT_AUTH_ERROR

    # Rate limiting
    if any(pat in error_str for pat in ["rate limit", "429", "ratelimit", "too many requests"]):
        return ErrorCategory.AGENT_RATE_LIMIT

    # Timeouts
    if "timeout" in error_str:
        if component == "agent":
            return ErrorCategory.AGENT_TIMEOUT
        if component == "evaluator":
            return ErrorCategory.LLM_REVIEWER_ERROR
        return ErrorCategory.UNKNOWN

    # API/Network errors
    if any(pat in error_str for pat in ["api error", "connection", "network", "502", "503", "504"]):
        if component == "agent":
            return ErrorCategory.AGENT_API_ERROR
        if component == "evaluator":
            return ErrorCategory.LLM_REVIEWER_ERROR
        return ErrorCategory.UNKNOWN

    # Disk errors
    if any(pat in error_str for pat in ["disk", "no space", "enospc"]):
        return ErrorCategory.DISK_FULL

    # Memory errors
    if any(pat in error_str for pat in ["memory", "oom", "out of memory"]):
        return ErrorCategory.OUT_OF_MEMORY

    # Component-specific categorization
    if component == "sandbox":
        if "venv" in error_str or "virtualenv" in error_str:
            return ErrorCategory.VENV_CREATION_ERROR
        if "install" in error_str or "pip" in error_str or "package" in error_str:
            return ErrorCategory.PACKAGE_INSTALL_ERROR
        if "git" in error_str or "clone" in error_str:
            return ErrorCategory.GIT_CLONE_ERROR
        if "template" in error_str or "copy" in error_str:
            return ErrorCategory.TEMPLATE_COPY_ERROR
        return ErrorCategory.SANDBOX_SETUP_ERROR

    if component == "agent":
        if "crash" in error_str or "killed" in error_str:
            return ErrorCategory.AGENT_CRASH
        if "invalid" in error_str or "malformed" in error_str:
            return ErrorCategory.AGENT_INVALID_OUTPUT
        # Default to API error for unknown agent failures (more likely to be retryable)
        return ErrorCategory.AGENT_API_ERROR

    if component == "evaluator":
        # Default to LLM_REVIEWER_ERROR for evaluator component
        # (orchestrator uses this for LLM reviewer calls which are retryable)
        if "criterion" in error_str or "check" in error_str:
            return ErrorCategory.CRITERION_CHECK_ERROR
        return ErrorCategory.LLM_REVIEWER_ERROR

    if component == "task":
        if "invalid" in error_str or "malformed" in error_str or "validation" in error_str:
            return ErrorCategory.TASK_INVALID
        return ErrorCategory.TASK_NOT_FOUND

    # Default to unknown
    logger.warning(f"Could not categorize error: {error} (component={component})")
    return ErrorCategory.UNKNOWN


def truncate_log(log: str, max_chars: int = 1000) -> str:
    """Truncate log preserving both start and end.

    Keeps first 40% and last 60% of the log instead of just the end.
    This preserves important startup information while still showing
    the final error state.

    Args:
        log: Log string to truncate
        max_chars: Maximum characters to keep (default: 1000)

    Returns:
        Truncated log string with separator if truncated

    Example:
        >>> truncate_log("x" * 100, max_chars=100)
        'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
        >>> len(truncate_log("x" * 2000, max_chars=1000))
        1000
    """
    if len(log) <= max_chars:
        return log

    # Keep first 40% and last 60% with separator
    separator = "\n\n... [truncated] ...\n\n"
    head_size = int(max_chars * 0.4)
    tail_size = max(0, max_chars - head_size - len(separator))

    return log[:head_size] + separator + log[-tail_size:]


def error_context_to_dict(ctx: ErrorContext) -> dict[str, Any]:
    """Convert ErrorContext to dict for EvaluationResult.error_details.

    Args:
        ctx: ErrorContext instance

    Returns:
        Dict representation of error context with error_tip added
    """
    result = ctx.model_dump()
    # Add error tip for this error category
    category = ErrorCategory(ctx.error_category)
    result["error_tip"] = get_error_tip(category)
    return result


def create_error_context(
    error: Exception,
    task_id: str,
    attempt: int,
    component: str | None = None,
    agent_name: str | None = None,
    **kwargs,
) -> dict[str, Any]:
    """Create comprehensive error context for reporting.

    Internally creates a type-safe ErrorContext model, then converts to dict
    for backward compatibility with EvaluationResult.error_details.

    Args:
        error: The exception that occurred
        task_id: Task identifier
        attempt: Attempt number (1-indexed)
        component: Component that failed (agent/sandbox/evaluator)
        agent_name: Agent name (optional)
        **kwargs: Additional context fields:
            - disk_usage_gb: Disk usage in GB
            - memory_usage_gb: Memory usage in GB
            - agent_stdout: Agent stdout (will be truncated)
            - agent_stderr: Agent stderr (will be truncated)

    Returns:
        Dict with error context fields (from ErrorContext.model_dump())

    Example:
        >>> ctx = create_error_context(
        ...     error=ValueError("test"),
        ...     task_id="task-001",
        ...     attempt=1,
        ...     component="agent"
        ... )
        >>> ctx["task_id"]
        'task-001'
        >>> ctx["attempt_number"]
        1
    """
    # Categorize error
    category = categorize_error(error, {"component": component, "task_id": task_id})

    # Truncate logs (improved: keeps start + end)
    agent_stdout = kwargs.get("agent_stdout")
    if agent_stdout:
        agent_stdout = truncate_log(agent_stdout)

    agent_stderr = kwargs.get("agent_stderr")
    if agent_stderr:
        agent_stderr = truncate_log(agent_stderr)

    # Convert to 0-indexed attempt (defensive: prevent negative indexing)
    attempt_index = max(attempt - 1, 0)

    # Create type-safe ErrorContext model
    ctx = ErrorContext(
        error_category=category.value,
        error_message=str(error),
        stack_trace=traceback.format_exc(),
        task_id=task_id,
        component=component,
        agent_name=agent_name,
        attempt_number=attempt,
        is_retryable=should_retry(category, attempt_index),
        retry_delay_seconds=get_retry_delay(category, attempt_index) if should_retry(category, attempt_index) else 0.0,
        disk_usage_gb=kwargs.get("disk_usage_gb"),
        memory_usage_gb=kwargs.get("memory_usage_gb"),
        agent_stdout=agent_stdout,
        agent_stderr=agent_stderr,
        timestamp=datetime.now().isoformat(),
    )

    # Return dict for backward compatibility
    return error_context_to_dict(ctx)


async def execute_with_retry(
    operation: Callable[[], Awaitable[Any]],
    operation_name: str,
    context: dict[str, Any],
    max_attempts: int | None = None,
) -> Any:
    """Execute an operation with automatic retry on transient errors.

    This is the core retry mechanism. It wraps any async operation and:
    1. Executes the operation
    2. Catches exceptions
    3. Categorizes the error
    4. Retries if eligible (with exponential backoff + jitter)
    5. Re-raises after exhausting retries

    Args:
        operation: Async callable to execute (no arguments, use closures/lambdas)
        operation_name: Human-readable operation name for logging
        context: Context dict with:
            - task_id: Task identifier (required)
            - component: Component name (optional but recommended)
            - agent_name: Agent name (optional)
        max_attempts: Override max attempts (defaults to 10 as safety limit)

    Returns:
        Result from operation

    Raises:
        Last exception if all retries exhausted

    Example:
        >>> async def flaky_api_call():
        ...     return await agent.communicate(prompt)
        >>>
        >>> result = await execute_with_retry(
        ...     operation=flaky_api_call,
        ...     operation_name="Agent communication",
        ...     context={"task_id": "task-001", "component": "agent"},
        ... )
    """
    task_id = context.get("task_id", "unknown")
    last_error = None

    # Determine max attempts (safety limit)
    if max_attempts is None:
        max_attempts = 10

    for attempt in range(max_attempts):
        try:
            # Execute operation
            return await operation()

        except asyncio.CancelledError:
            # Re-raise cancellation immediately - not an error to retry
            raise

        except Exception as e:
            last_error = e

            # Categorize error
            category = categorize_error(e, context)

            # Check if we should retry
            if not should_retry(category, attempt):
                logger.error(f"[{task_id}] {operation_name} failed (non-retryable): {category.value} - {e}")
                raise

            # Check if we've exhausted attempts
            config = RETRY_CONFIG.get(category, RetryConfig())
            if attempt >= config.max_retries:
                logger.error(
                    f"[{task_id}] {operation_name} failed after {attempt + 1} attempts: {category.value} - {e}"
                )
                raise

            # Calculate backoff with jitter
            delay = get_retry_delay(category, attempt)

            # Enhanced warning with actionable tip
            tip = get_error_tip(category)
            message = (
                f"[{task_id}] {operation_name} failed (attempt {attempt + 1}/{config.max_retries + 1}): "
                f"{category.value} - {e}. Retrying in {delay:.1f}s...\n  💡 Tip: {tip}"
            )
            logger.warning(message)

            # Async sleep (non-blocking)
            await asyncio.sleep(delay)

    # Should never reach here, but just in case
    if last_error:
        raise last_error
    raise RuntimeError(f"Unexpected retry loop exit for {operation_name}")
