"""Error categorization logic using pattern matching.

This module provides the categorize_error function that analyzes exceptions
and maps them to appropriate ErrorCategory values for retry logic.
"""

import logging
from typing import Any

from .categories import ErrorCategory


logger = logging.getLogger(__name__)


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

    # Billing/credit errors (NOT retryable - retrying wastes time)
    if any(
        pat in error_str
        for pat in ["credit", "billing", "payment", "insufficient", "402", "quota exceeded", "spending limit"]
    ):
        return ErrorCategory.AGENT_BILLING_ERROR

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
        if (
            "crash" in error_str
            or "killed" in error_str
            or "cli process failed (exit code" in error_str
            or "command failed with exit code" in error_str
        ):
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
