"""Retry executor - main retry orchestration logic."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .categories import RETRY_CONFIG, RetryConfig
from .categorization import categorize_error
from .retry import get_error_tip, get_retry_delay, should_retry


logger = logging.getLogger(__name__)


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
            config = RETRY_CONFIG.get(category, RetryConfig())

            # Check if we should retry (handles both non-retryable categories and exhausted attempts)
            if not should_retry(category, attempt):
                if config.max_retries == 0:
                    logger.error(f"[{task_id}] {operation_name} failed (non-retryable): {category.value} - {e}")
                else:
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
