"""
Unit tests for error_handling module.

Tests error categorization, retry logic, exponential backoff, and error context creation.
"""

from unittest.mock import AsyncMock, patch

import pytest

from coder_eval.errors.categories import RETRY_CONFIG, ErrorCategory, RetryConfig
from coder_eval.errors.categorization import categorize_error
from coder_eval.errors.executor import execute_with_retry
from coder_eval.errors.retry import create_error_context, truncate_log


class TestErrorCategory:
    """Test ErrorCategory enum and retry configuration."""

    def test_retryable_categories_have_retry_config(self):
        """Retryable error categories must be in RETRY_CONFIG."""
        # Retryable categories (those expected in RETRY_CONFIG)
        retryable = [
            ErrorCategory.AGENT_API_ERROR,
            ErrorCategory.AGENT_RATE_LIMIT,
            ErrorCategory.LLM_REVIEWER_ERROR,
            ErrorCategory.SANDBOX_SETUP_ERROR,
            ErrorCategory.SANDBOX_COMMAND_ERROR,
            ErrorCategory.VENV_CREATION_ERROR,
            ErrorCategory.PACKAGE_INSTALL_ERROR,
            ErrorCategory.GIT_CLONE_ERROR,
            ErrorCategory.TEMPLATE_COPY_ERROR,
        ]

        for category in retryable:
            assert category in RETRY_CONFIG, f"Missing retry config for {category}"

    def test_retry_config_types(self):
        """Verify retry config has correct types and values."""
        for _category, config in RETRY_CONFIG.items():
            assert isinstance(config, RetryConfig)
            assert config.max_retries >= 0
            assert config.initial_delay > 0
            assert config.backoff_multiplier >= 1.0

    def test_retryable_categories(self):
        """Verify which categories are retryable."""
        # All categories in RETRY_CONFIG should have max_retries > 0
        for category, config in RETRY_CONFIG.items():
            assert config.max_retries > 0, f"{category} in RETRY_CONFIG should be retryable"

    def test_non_retryable_categories(self):
        """Verify which categories are non-retryable (not in RETRY_CONFIG)."""
        non_retryable = [
            ErrorCategory.AGENT_TIMEOUT,
            ErrorCategory.AGENT_CRASH,
            ErrorCategory.AGENT_INVALID_OUTPUT,
            ErrorCategory.AGENT_AUTH_ERROR,
            ErrorCategory.TASK_NOT_FOUND,
            ErrorCategory.TASK_INVALID,
            ErrorCategory.UNKNOWN,
            ErrorCategory.CRITERION_CHECK_ERROR,  # Changed from CONFIG_ERROR/PERMISSION_ERROR
            ErrorCategory.DISK_FULL,  # Added valid non-retryable
            ErrorCategory.OUT_OF_MEMORY,  # Added valid non-retryable
        ]

        for category in non_retryable:
            assert category not in RETRY_CONFIG, f"{category} should not be retryable (not in RETRY_CONFIG)"


class TestCategorizeError:
    """Test error categorization logic."""

    # Agent component errors
    def test_categorize_agent_timeout_exception(self):
        """TimeoutError with agent component → AGENT_TIMEOUT."""
        error = TimeoutError("Agent timed out")
        result = categorize_error(error, {"component": "agent"})
        assert result == ErrorCategory.AGENT_TIMEOUT

    def test_categorize_agent_timeout_string(self):
        """String 'timeout' with agent component → AGENT_TIMEOUT."""
        error = Exception("Connection timeout")
        result = categorize_error(error, {"component": "agent"})
        assert result == ErrorCategory.AGENT_TIMEOUT

    def test_categorize_agent_api_error(self):
        """Network/API errors with agent component → AGENT_API_ERROR."""
        test_cases = [
            Exception("API error occurred"),
            Exception("Connection failed"),
            Exception("Network unreachable"),
            Exception("502 Bad Gateway"),
            Exception("503 Service Unavailable"),
        ]

        for error in test_cases:
            result = categorize_error(error, {"component": "agent"})
            assert result == ErrorCategory.AGENT_API_ERROR, f"Failed for: {error}"

    def test_categorize_agent_rate_limit(self):
        """Rate limit errors → AGENT_RATE_LIMIT."""
        test_cases = [
            Exception("Rate limit exceeded"),
            Exception("429 Too Many Requests"),
            Exception("RateLimit error"),
        ]

        for error in test_cases:
            result = categorize_error(error, {"component": "agent"})
            assert result == ErrorCategory.AGENT_RATE_LIMIT

    def test_categorize_agent_auth_error(self):
        """Authentication errors → AGENT_AUTH_ERROR."""
        test_cases = [
            Exception("Authentication failed"),
            Exception("Unauthorized access"),
            Exception("Invalid API key"),
            Exception("401 Unauthorized"),
        ]

        for error in test_cases:
            result = categorize_error(error, {"component": "agent"})
            assert result == ErrorCategory.AGENT_AUTH_ERROR

    def test_categorize_agent_crash(self):
        """Agent crash errors → AGENT_CRASH."""
        error = Exception("Agent process crashed")
        result = categorize_error(error, {"component": "agent"})
        assert result == ErrorCategory.AGENT_CRASH

    def test_categorize_agent_cli_process_failure(self):
        """CLI process failure with exit code → AGENT_CRASH (not retryable)."""
        error = RuntimeError("CLI process failed (exit code 1): some error output")
        result = categorize_error(error, {"component": "agent"})
        assert result == ErrorCategory.AGENT_CRASH

    def test_categorize_agent_generic_exit_code_not_crash(self):
        """Generic mention of 'exit code' should NOT match AGENT_CRASH."""
        error = RuntimeError("Command returned exit code 1")
        result = categorize_error(error, {"component": "agent"})
        # Falls through to default AGENT_API_ERROR, not AGENT_CRASH
        assert result == ErrorCategory.AGENT_API_ERROR

    def test_categorize_agent_invalid_output(self):
        """Invalid/malformed output → AGENT_INVALID_OUTPUT."""
        error = Exception("Malformed JSON response")
        result = categorize_error(error, {"component": "agent"})
        assert result == ErrorCategory.AGENT_INVALID_OUTPUT

    # Evaluator component errors (CRITICAL: LLM reviewer retry functionality)
    def test_categorize_evaluator_timeout_exception(self):
        """TimeoutError with evaluator component → LLM_REVIEWER_ERROR (retryable)."""
        error = TimeoutError("LLM reviewer timeout")
        result = categorize_error(error, {"component": "evaluator"})
        assert result == ErrorCategory.LLM_REVIEWER_ERROR

    def test_categorize_evaluator_timeout_string(self):
        """Timeout string with evaluator component → LLM_REVIEWER_ERROR (retryable)."""
        error = Exception("Request timeout")
        result = categorize_error(error, {"component": "evaluator"})
        assert result == ErrorCategory.LLM_REVIEWER_ERROR

    def test_categorize_evaluator_network_error(self):
        """Network errors with evaluator component → LLM_REVIEWER_ERROR (retryable)."""
        test_cases = [
            Exception("API error"),
            Exception("Connection failed"),
            Exception("Network unreachable"),
            Exception("502 Bad Gateway"),
            Exception("503 Service Unavailable"),
        ]

        for error in test_cases:
            result = categorize_error(error, {"component": "evaluator"})
            assert result == ErrorCategory.LLM_REVIEWER_ERROR, f"Failed for: {error}"

    def test_categorize_evaluator_default_is_llm_reviewer_error(self):
        """Evaluator component defaults to LLM_REVIEWER_ERROR (retryable)."""
        error = Exception("Some unknown evaluator error")
        result = categorize_error(error, {"component": "evaluator"})
        assert result == ErrorCategory.LLM_REVIEWER_ERROR

    def test_categorize_evaluator_criterion_check_error(self):
        """Criterion-specific errors → CRITERION_CHECK_ERROR."""
        test_cases = [
            Exception("Criterion check failed"),
            Exception("Check validation error"),
        ]

        for error in test_cases:
            result = categorize_error(error, {"component": "evaluator"})
            assert result == ErrorCategory.CRITERION_CHECK_ERROR

    # Sandbox component errors
    def test_categorize_sandbox_venv_error(self):
        """Virtual environment errors → VENV_CREATION_ERROR."""
        error = Exception("Failed to create venv")
        result = categorize_error(error, {"component": "sandbox"})
        assert result == ErrorCategory.VENV_CREATION_ERROR

    def test_categorize_sandbox_package_install_error(self):
        """Package installation errors → PACKAGE_INSTALL_ERROR."""
        test_cases = [
            Exception("pip install failed"),
            Exception("Package not found"),
        ]

        for error in test_cases:
            result = categorize_error(error, {"component": "sandbox"})
            assert result == ErrorCategory.PACKAGE_INSTALL_ERROR

    def test_categorize_sandbox_git_clone_error(self):
        """Git clone errors → GIT_CLONE_ERROR."""
        error = Exception("Git clone failed")
        result = categorize_error(error, {"component": "sandbox"})
        assert result == ErrorCategory.GIT_CLONE_ERROR

    def test_categorize_sandbox_template_copy_error(self):
        """Template copy errors → TEMPLATE_COPY_ERROR."""
        error = Exception("Template copy failed")
        result = categorize_error(error, {"component": "sandbox"})
        assert result == ErrorCategory.TEMPLATE_COPY_ERROR

    def test_categorize_sandbox_default(self):
        """Unknown sandbox errors → SANDBOX_SETUP_ERROR."""
        error = Exception("Unknown sandbox error")
        result = categorize_error(error, {"component": "sandbox"})
        assert result == ErrorCategory.SANDBOX_SETUP_ERROR

    # Task component errors
    def test_categorize_task_not_found(self):
        """File not found with task keywords → TASK_NOT_FOUND."""
        test_cases = [
            FileNotFoundError("task.yaml not found"),
            FileNotFoundError("Missing YAML file"),
        ]

        for error in test_cases:
            result = categorize_error(error, {"component": "task"})
            assert result == ErrorCategory.TASK_NOT_FOUND

    def test_categorize_task_invalid(self):
        """Invalid/malformed task → TASK_INVALID."""
        error = Exception("Invalid task definition")
        result = categorize_error(error, {"component": "task"})
        assert result == ErrorCategory.TASK_INVALID

    # System-wide errors
    def test_categorize_disk_full(self):
        """Disk space errors → DISK_FULL."""
        test_cases = [
            Exception("No space left on device"),
            Exception("Disk full"),
            Exception("ENOSPC error"),
        ]

        for error in test_cases:
            result = categorize_error(error, {"component": "sandbox"})
            assert result == ErrorCategory.DISK_FULL

    def test_categorize_out_of_memory_exception(self):
        """MemoryError → OUT_OF_MEMORY."""
        error = MemoryError("Out of memory")
        result = categorize_error(error, {"component": "agent"})
        assert result == ErrorCategory.OUT_OF_MEMORY

    def test_categorize_out_of_memory_string(self):
        """OOM string patterns → OUT_OF_MEMORY."""
        test_cases = [
            Exception("Out of memory"),
            Exception("OOM killed"),
            Exception("Memory allocation failed"),
        ]

        for error in test_cases:
            result = categorize_error(error, {"component": "agent"})
            assert result == ErrorCategory.OUT_OF_MEMORY

    # Edge cases
    def test_categorize_with_hint_overrides_logic(self):
        """Explicit hint parameter overrides categorization logic."""
        error = TimeoutError("timeout")
        result = categorize_error(error, {"component": "agent"}, hint=ErrorCategory.UNKNOWN)
        assert result == ErrorCategory.UNKNOWN

    def test_categorize_unknown_component(self):
        """Unknown component → UNKNOWN."""
        error = Exception("Some error")
        result = categorize_error(error, {"component": "unknown_component"})
        assert result == ErrorCategory.UNKNOWN

    def test_categorize_no_component(self):
        """Missing component → UNKNOWN."""
        error = Exception("Some error")
        result = categorize_error(error, {})
        assert result == ErrorCategory.UNKNOWN


# Note: get_retry_delay is an internal function not exported from error_handling module
# Testing is done indirectly through execute_with_retry tests


class TestExecuteWithRetry:
    """Test retry orchestration logic."""

    @pytest.mark.asyncio
    async def test_execute_with_retry_success_first_attempt(self):
        """Successful operation on first attempt → no retries."""
        mock_operation = AsyncMock(return_value="success")

        result = await execute_with_retry(
            operation=mock_operation, operation_name="Test operation", context={"component": "test"}
        )

        assert result == "success"
        assert mock_operation.call_count == 1

    @pytest.mark.asyncio
    async def test_execute_with_retry_retryable_error_eventually_succeeds(self):
        """Retryable error → retry until success."""
        mock_operation = AsyncMock(
            side_effect=[
                Exception("503 Service Unavailable"),
                Exception("Connection failed"),
                "success",
            ]
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await execute_with_retry(
                operation=mock_operation, operation_name="Test operation", context={"component": "agent"}
            )

            assert result == "success"
            assert mock_operation.call_count == 3

    @pytest.mark.asyncio
    async def test_execute_with_retry_non_retryable_error_fails_immediately(self):
        """Non-retryable error → fail immediately without retry."""
        mock_operation = AsyncMock(side_effect=Exception("Invalid API key"))

        with pytest.raises(Exception, match="Invalid API key"):
            await execute_with_retry(
                operation=mock_operation, operation_name="Test operation", context={"component": "agent"}
            )

        assert mock_operation.call_count == 1

    @pytest.mark.asyncio
    async def test_execute_with_retry_max_retries_exceeded(self):
        """Retryable error exceeds max retries → raise last error."""
        mock_operation = AsyncMock(side_effect=Exception("503 Service Unavailable"))

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(Exception, match="503 Service Unavailable"):
                await execute_with_retry(
                    operation=mock_operation,
                    operation_name="Test operation",
                    context={"component": "agent"},
                    max_attempts=3,
                )

            assert mock_operation.call_count == 3

    @pytest.mark.asyncio
    async def test_execute_with_retry_respects_custom_max_attempts(self):
        """Custom max_attempts parameter overrides config."""
        mock_operation = AsyncMock(side_effect=Exception("Rate limit exceeded"))

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(Exception, match="Rate limit exceeded"):
                await execute_with_retry(
                    operation=mock_operation,
                    operation_name="Test operation",
                    context={"component": "agent"},
                    max_attempts=2,  # Override default of 5 for AGENT_RATE_LIMIT
                )

            assert mock_operation.call_count == 2

    @pytest.mark.asyncio
    async def test_execute_with_retry_evaluator_timeout_is_retryable(self):
        """CRITICAL: Evaluator TimeoutError must be retryable."""
        mock_operation = AsyncMock(side_effect=[TimeoutError("LLM reviewer timeout"), "success"])

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await execute_with_retry(
                operation=mock_operation, operation_name="LLM reviewer", context={"component": "evaluator"}
            )

            assert result == "success"
            assert mock_operation.call_count == 2, "Evaluator timeout should trigger retry"

    @pytest.mark.asyncio
    async def test_execute_with_retry_evaluator_network_error_is_retryable(self):
        """CRITICAL: Evaluator network errors must be retryable."""
        mock_operation = AsyncMock(side_effect=[Exception("Connection failed"), "success"])

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await execute_with_retry(
                operation=mock_operation, operation_name="LLM reviewer", context={"component": "evaluator"}
            )

            assert result == "success"
            assert mock_operation.call_count == 2, "Evaluator network error should trigger retry"

    @pytest.mark.asyncio
    async def test_execute_with_retry_waits_between_retries(self):
        """Verify retry delays are applied."""
        mock_operation = AsyncMock(side_effect=[Exception("503 Service Unavailable"), "success"])

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await execute_with_retry(
                operation=mock_operation, operation_name="Test operation", context={"component": "agent"}
            )

            assert result == "success"
            assert mock_sleep.call_count == 1
            # Should sleep for initial_delay ± jitter (5.0 ± 25% for AGENT_API_ERROR)
            sleep_duration = mock_sleep.call_args[0][0]
            assert 3.75 <= sleep_duration <= 6.25


class TestCreateErrorContext:
    """Test error context creation and reporting."""

    def test_create_error_context_basic(self):
        """Create error context with minimal parameters."""
        error = Exception("Test error")
        context = create_error_context(error=error, task_id="task_001", attempt=1)

        assert isinstance(context, dict)
        assert context["error_category"] == ErrorCategory.UNKNOWN.value
        assert context["error_message"] == "Test error"
        assert context["task_id"] == "task_001"
        assert context["attempt_number"] == 1
        assert context["is_retryable"] is False
        assert "stack_trace" in context

    def test_create_error_context_with_component(self):
        """Error context includes component information."""
        error = Exception("API error")
        context = create_error_context(error=error, task_id="task_002", attempt=2, component="agent")

        assert context["component"] == "agent"
        assert context["error_category"] == ErrorCategory.AGENT_API_ERROR.value
        assert context["is_retryable"] is True
        assert context["retry_delay_seconds"] > 0

    def test_create_error_context_with_agent_name(self):
        """Error context includes agent name when provided."""
        error = TimeoutError("timeout")
        context = create_error_context(
            error=error, task_id="task_003", attempt=1, component="agent", agent_name="claude-code"
        )

        assert context["agent_name"] == "claude-code"

    def test_create_error_context_retryable_vs_non_retryable(self):
        """Verify retry parameters differ for retryable vs non-retryable errors."""
        # Retryable error
        retryable_error = Exception("503 Service Unavailable")
        retryable_context = create_error_context(
            error=retryable_error, task_id="task_004", attempt=1, component="agent"
        )

        assert retryable_context["is_retryable"] is True
        assert retryable_context["retry_delay_seconds"] > 0

        # Non-retryable error
        non_retryable_error = Exception("Invalid API key")
        non_retryable_context = create_error_context(
            error=non_retryable_error, task_id="task_005", attempt=1, component="agent"
        )

        assert non_retryable_context["is_retryable"] is False
        assert non_retryable_context["retry_delay_seconds"] == 0.0

    def test_create_error_context_includes_error_tip(self):
        """Error context includes actionable error tip."""
        error = Exception("Rate limit exceeded")
        context = create_error_context(error=error, task_id="task_006", attempt=1, component="agent")

        assert "error_tip" in context
        assert "rate limit" in context["error_tip"].lower()  # Should mention rate limit


class TestTruncateLog:
    """Test log truncation utility."""

    def test_truncate_log_short_string(self):
        """Short strings are not truncated."""
        short_log = "Short log message"
        result = truncate_log(short_log, max_chars=1000)
        assert result == short_log

    def test_truncate_log_empty_string(self):
        """Empty strings are returned as-is."""
        result = truncate_log("", max_chars=1000)
        assert result == ""

    def test_truncate_log_exact_max_chars(self):
        """String exactly at max_chars is not truncated."""
        log = "x" * 1000
        result = truncate_log(log, max_chars=1000)
        assert result == log

    def test_truncate_log_long_string_has_separator(self):
        """Long strings are truncated with separator."""
        long_log = "x" * 2000
        result = truncate_log(long_log, max_chars=1000)

        assert len(result) <= 1000
        assert "..." in result or "truncated" in result.lower()

    def test_truncate_log_preserves_head_and_tail(self):
        """Truncation preserves beginning (40%) and end (60%) of log."""
        long_log = "A" * 1000 + "B" * 1000
        result = truncate_log(long_log, max_chars=1000)

        # Should start with 'A's (head)
        assert result[0] == "A"

        # Should end with 'B's (tail)
        assert result[-1] == "B"

    def test_truncate_log_custom_max_chars(self):
        """Custom max_chars parameter is respected."""
        long_log = "x" * 1000
        result = truncate_log(long_log, max_chars=500)

        assert len(result) <= 500
