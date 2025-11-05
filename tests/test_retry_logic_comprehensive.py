"""Comprehensive retry logic tests - non-retryable, exhaustion, cancellation, jitter, categorization.

Tests ensure retry mechanism behaves correctly under all scenarios.
"""

import asyncio
import random
from unittest.mock import AsyncMock, patch

import pytest

from coder_eval.errors.categories import ErrorCategory
from coder_eval.errors.categorization import categorize_error
from coder_eval.errors.executor import execute_with_retry
from coder_eval.errors.retry import get_retry_delay, should_retry


@pytest.mark.asyncio
async def test_execute_with_retry_stops_on_non_retryable():
    """Test that non-retryable errors (auth) fail immediately.

    Hypothesis: Authentication errors should not be retried.
    Expected: Single attempt, then exception raised.

    Context: Lines 566-568 in error_handling.py check should_retry.
    """
    attempts = 0

    async def flaky_operation():
        nonlocal attempts
        attempts += 1
        raise Exception("Invalid API Key")  # Triggers AGENT_AUTH_ERROR

    context = {"task_id": "test-task", "component": "agent"}

    # Should fail immediately without retries
    with pytest.raises(Exception, match="Invalid API Key"):
        await execute_with_retry(flaky_operation, "test_op", context, max_attempts=5)

    # Only one attempt - no retries
    assert attempts == 1


@pytest.mark.asyncio
async def test_execute_with_retry_exhausts_retries():
    """Test that retry loop terminates after max_retries.

    Hypothesis: Persistent retryable errors should exhaust retries and fail.
    Expected: Initial attempt + max_retries, then exception raised.

    Context: Lines 571-576 check attempt >= config.max_retries.
    """
    attempts = 0

    async def always_fails():
        nonlocal attempts
        attempts += 1
        raise Exception("Rate limit exceeded")  # Retryable error

    context = {"task_id": "test-task", "component": "agent"}

    # Mock sleep to speed up test
    with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(Exception, match="Rate limit"):
        # AGENT_RATE_LIMIT has max_retries=5
        await execute_with_retry(always_fails, "test_op", context, max_attempts=10)

    # Should attempt: 1 initial + 5 retries = 6 total
    # Note: execute_with_retry uses RETRY_CONFIG[AGENT_RATE_LIMIT].max_retries = 5
    assert attempts == 6  # Initial + 5 retries


@pytest.mark.asyncio
async def test_execute_with_retry_handles_cancellation():
    """Test that asyncio.CancelledError propagates immediately.

    Hypothesis: Cancellation should bypass retry logic entirely.
    Expected: No retries, immediate propagation.

    Context: Lines 555-557 catch and re-raise CancelledError.
    """
    attempts = 0

    async def cancelled_operation():
        nonlocal attempts
        attempts += 1
        raise asyncio.CancelledError

    context = {"task_id": "test-task"}

    # Mock sleep (should not be called)
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, pytest.raises(asyncio.CancelledError):
        await execute_with_retry(cancelled_operation, "test_op", context, max_attempts=5)

    # Only one attempt, no retries
    assert attempts == 1
    # Sleep should not be called
    mock_sleep.assert_not_awaited()


def test_get_retry_delay_with_jitter_bounds():
    """Test that jitter stays within bounds (0-25% of base delay).

    Hypothesis: Exponential backoff with bounded jitter prevents thundering herd.
    Expected: Delay = base * multiplier^attempt + jitter(0-25%).

    Context: Lines 221-227 implement exponential backoff + jitter.
    """
    category = ErrorCategory.AGENT_API_ERROR
    # Config: initial_delay=5.0, multiplier=2.0

    # Mock random.uniform to return predictable jitter
    with patch.object(random, "uniform", return_value=0.5) as mock_uniform:
        # Attempt 0: base=5.0, jitter=0.5
        delay_0 = get_retry_delay(category, 0)
        # Attempt 1: base=10.0, jitter=0.5
        delay_1 = get_retry_delay(category, 1)
        # Attempt 2: base=20.0, jitter=0.5
        delay_2 = get_retry_delay(category, 2)

    # Verify exponential backoff
    assert abs(delay_0 - 5.5) < 0.01  # 5.0 + 0.5
    assert abs(delay_1 - 10.5) < 0.01  # 10.0 + 0.5
    assert abs(delay_2 - 20.5) < 0.01  # 20.0 + 0.5

    # Verify uniform was called with correct bounds (0 to 25% of base)
    calls = mock_uniform.call_args_list
    assert calls[0][0] == (0, 5.0 * 0.25)  # Attempt 0
    assert calls[1][0] == (0, 10.0 * 0.25)  # Attempt 1
    assert calls[2][0] == (0, 20.0 * 0.25)  # Attempt 2


def test_categorize_error_timeout_component_specific():
    """Test that TimeoutError maps to different categories by component.

    Hypothesis: Component context disambiguates timeout errors.
    Expected: agent -> AGENT_TIMEOUT, evaluator -> LLM_REVIEWER_ERROR.

    Context: Lines 281-286 in error_handling.py check component.
    """
    timeout_error = TimeoutError("Operation timed out")

    # Agent timeout
    agent_category = categorize_error(timeout_error, {"component": "agent"})
    assert agent_category == ErrorCategory.AGENT_TIMEOUT

    # Evaluator timeout
    evaluator_category = categorize_error(timeout_error, {"component": "evaluator"})
    assert evaluator_category == ErrorCategory.LLM_REVIEWER_ERROR

    # Unknown component falls back to UNKNOWN
    unknown_category = categorize_error(timeout_error, {"component": "unknown"})
    assert unknown_category == ErrorCategory.UNKNOWN


# Parametrized test for comprehensive error categorization
@pytest.mark.parametrize(
    "error,context,hint,expected",
    [
        # Hint precedence
        (ValueError("test"), {}, ErrorCategory.AGENT_CRASH, ErrorCategory.AGENT_CRASH),
        # Exception types
        (TimeoutError(), {"component": "agent"}, None, ErrorCategory.AGENT_TIMEOUT),
        (TimeoutError(), {"component": "evaluator"}, None, ErrorCategory.LLM_REVIEWER_ERROR),
        (FileNotFoundError("task.yaml"), {}, None, ErrorCategory.TASK_NOT_FOUND),
        (MemoryError(), {}, None, ErrorCategory.OUT_OF_MEMORY),
        # String matching - auth
        (Exception("Invalid API Key"), {}, None, ErrorCategory.AGENT_AUTH_ERROR),
        (Exception("Authentication failed"), {}, None, ErrorCategory.AGENT_AUTH_ERROR),
        (Exception("401 Unauthorized"), {}, None, ErrorCategory.AGENT_AUTH_ERROR),
        # String matching - rate limit
        (Exception("Rate limit exceeded"), {}, None, ErrorCategory.AGENT_RATE_LIMIT),
        (Exception("429 Too Many Requests"), {}, None, ErrorCategory.AGENT_RATE_LIMIT),
        # String matching - disk
        (Exception("No space left on device"), {}, None, ErrorCategory.DISK_FULL),
        # Component-specific matching
        (Exception("Failed to create virtualenv"), {"component": "sandbox"}, None, ErrorCategory.VENV_CREATION_ERROR),
        (Exception("pip install failed"), {"component": "sandbox"}, None, ErrorCategory.PACKAGE_INSTALL_ERROR),
        (Exception("git clone error"), {"component": "sandbox"}, None, ErrorCategory.GIT_CLONE_ERROR),
        # Fallback to UNKNOWN
        (ValueError("Unknown error"), {}, None, ErrorCategory.UNKNOWN),
    ],
)
def test_categorize_error_scenarios_parametrized(error, context, hint, expected):
    """Comprehensive parametrized test for error categorization.

    Tests hint precedence, exception types, and string matching.
    """
    category = categorize_error(error, context, hint=hint)
    assert category == expected


@pytest.mark.asyncio
async def test_execute_with_retry_succeeds_on_first_try():
    """Test happy path where operation succeeds immediately.

    Hypothesis: Successful operations should not trigger any retry logic.
    Expected: Single attempt, result returned.
    """
    attempts = 0

    async def successful_operation():
        nonlocal attempts
        attempts += 1
        return "success"

    context = {"task_id": "test-task"}

    result = await execute_with_retry(successful_operation, "test_op", context)

    assert result == "success"
    assert attempts == 1


@pytest.mark.asyncio
async def test_execute_with_retry_eventually_succeeds():
    """Test that operation succeeds after transient failures.

    Hypothesis: Transient errors should be retried until success.
    Expected: Multiple attempts, eventual success.
    """
    attempts = 0

    async def flaky_operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise Exception("Rate limit exceeded")  # Retryable
        return "success"

    context = {"task_id": "test-task", "component": "agent"}

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await execute_with_retry(flaky_operation, "test_op", context, max_attempts=5)

    assert result == "success"
    assert attempts == 3  # Failed twice, succeeded third time


def test_should_retry_respects_config():
    """Test that should_retry correctly uses RetryConfig.

    Hypothesis: Retry eligibility depends on attempt count and max_retries.
    Expected: True while attempts < max_retries, False after.
    """
    # AGENT_API_ERROR has max_retries=3
    category = ErrorCategory.AGENT_API_ERROR

    assert should_retry(category, 0) is True  # First retry
    assert should_retry(category, 1) is True  # Second retry
    assert should_retry(category, 2) is True  # Third retry
    assert should_retry(category, 3) is False  # Exhausted

    # AGENT_AUTH_ERROR has max_retries=0 (non-retryable)
    non_retryable = ErrorCategory.AGENT_AUTH_ERROR
    assert should_retry(non_retryable, 0) is False  # Never retry


def test_get_retry_delay_exponential_backoff():
    """Test that delay grows exponentially with attempt number.

    Hypothesis: Backoff prevents overwhelming external services.
    Expected: Each attempt has multiplier^attempt increase.
    """
    category = ErrorCategory.AGENT_API_ERROR
    # Config: initial_delay=5.0, multiplier=2.0

    with patch.object(random, "uniform", return_value=0):  # No jitter
        delay_0 = get_retry_delay(category, 0)
        delay_1 = get_retry_delay(category, 1)
        delay_2 = get_retry_delay(category, 2)

    assert abs(delay_0 - 5.0) < 0.01  # 5.0 * 2^0 = 5.0
    assert abs(delay_1 - 10.0) < 0.01  # 5.0 * 2^1 = 10.0
    assert abs(delay_2 - 20.0) < 0.01  # 5.0 * 2^2 = 20.0
