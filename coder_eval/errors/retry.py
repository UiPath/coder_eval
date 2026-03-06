"""Retry logic with exponential backoff and error context management.

This module provides retry functionality, error context creation, and helper
utilities for production-grade error handling.
"""

import logging
import random
import traceback
from datetime import datetime
from typing import Any

from .categories import ERROR_TIPS, RETRY_CONFIG, ErrorCategory, ErrorContext, RetryConfig
from .categorization import categorize_error


logger = logging.getLogger(__name__)


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


def compute_backoff(config: RetryConfig, attempt: int) -> float:
    """Calculate delay with exponential backoff and jitter.

    Formula: (initial_delay * backoff_multiplier^attempt) + jitter
    Jitter: Random value between 0 and 25% of base delay

    Args:
        config: Retry configuration (initial delay, multiplier)
        attempt: Current attempt number (0-indexed)

    Returns:
        Delay in seconds
    """
    base_delay = config.initial_delay * (config.backoff_multiplier**attempt)

    # Add jitter: up to 25% of base delay to prevent thundering herd
    jitter = random.uniform(0, base_delay * 0.25)

    return base_delay + jitter


def get_retry_delay(category: ErrorCategory, attempt: int) -> float:
    """Calculate delay before retry with exponential backoff and jitter.

    Convenience wrapper around ``compute_backoff`` that looks up the
    ``RetryConfig`` for the given error category.

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
    return compute_backoff(config, attempt)


def get_error_tip(category: ErrorCategory) -> str:
    """Get actionable tip for error category.

    Args:
        category: Error category

    Returns:
        Actionable tip string
    """
    return ERROR_TIPS.get(category, "Check logs for details or run with --verbose for more information.")


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
