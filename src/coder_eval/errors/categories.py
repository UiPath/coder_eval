"""Error categories and configuration for retry logic.

This module defines error categories, retry configuration, and structured
error context for production-grade error handling.
"""

from enum import Enum

from pydantic import BaseModel, Field


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
    AGENT_BILLING_ERROR = "agent_billing_error"  # Insufficient credits/billing issue (NOT retryable)
    AGENT_CRASH = "agent_crash"  # Agent process crashed (retryable — CLI crashes are often transient)
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
    ErrorCategory.AGENT_CRASH: RetryConfig(
        max_retries=2,
        backoff_multiplier=2.0,
        initial_delay=5.0,
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
    ErrorCategory.AGENT_TIMEOUT: (
        "Agent or task timed out. Consider increasing --turn-timeout or --task-timeout, "
        "or simplifying the task to require fewer iterations."
    ),
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
    ErrorCategory.AGENT_BILLING_ERROR: (
        "Insufficient API credits or billing issue. Check your account balance and billing settings "
        "at your provider's dashboard."
    ),
    ErrorCategory.AGENT_CRASH: (
        "Claude CLI process exited unexpectedly. Common causes: insufficient API credits, "
        "invalid API key, or network issues. Run 'claude --version' to verify the CLI works, "
        "then try running your prompt directly with 'claude' to see the actual error."
    ),
}
