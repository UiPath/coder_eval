"""Error categorization logic using pattern matching.

This module provides the categorize_error function that analyzes exceptions
and maps them to appropriate ErrorCategory values for retry logic.
"""

import logging
import re
from typing import Any

from .agent import AgentConfigError, AgentCrashError
from .budget import BudgetExceededError
from .categories import ErrorCategory
from .timeout import EvaluationTimeoutError


logger = logging.getLogger(__name__)


def provider_error_category(*, code: object = None, status_code: object = None) -> ErrorCategory | None:
    """Map provider-owned codes and HTTP statuses, never diagnostic prose."""
    if isinstance(code, str):
        normalized = code.lower().replace("_", "")
        if normalized in {
            "insufficientquota",
            "billinghardlimitreached",
            "billingnotactive",
            "paymentrequired",
            "usagelimitexceeded",
            "billingerror",
        }:
            return ErrorCategory.AGENT_BILLING_ERROR
        if normalized in {"ratelimitexceeded", "ratelimiterror", "ratelimit"}:
            return ErrorCategory.AGENT_RATE_LIMIT
        if normalized in {"authenticationfailed", "authenticationerror", "unauthorized"}:
            return ErrorCategory.AGENT_AUTH_ERROR
        if normalized in {"invalidrequest", "contextwindowexceeded", "contentfilter"}:
            return ErrorCategory.AGENT_INVALID_OUTPUT
        if normalized in {"servererror", "serveroverloaded", "responsestreamdisconnected", "httpconnectionfailed"}:
            # HTTP status is more specific than a transport envelope.
            return provider_error_category(status_code=status_code) or ErrorCategory.AGENT_API_ERROR
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        if status_code == 402:
            return ErrorCategory.AGENT_BILLING_ERROR
        if status_code in {401, 403}:
            return ErrorCategory.AGENT_AUTH_ERROR
        if status_code == 429:
            return ErrorCategory.AGENT_RATE_LIMIT
        if 500 <= status_code <= 599:
            return ErrorCategory.AGENT_API_ERROR
    return None


def provider_exception_category(error: BaseException) -> ErrorCategory | None:
    """Read structured provider metadata through an exception chain."""
    seen: set[int] = set()
    statuses = []
    while id(error) not in seen:
        seen.add(id(error))
        codes = [getattr(error, "code", None)]
        body = getattr(error, "body", None)
        if isinstance(body, dict):
            detail = body.get("error", body)
            if isinstance(detail, dict):
                codes.extend([detail.get("code"), detail.get("type")])
        status = getattr(error, "status_code", getattr(getattr(error, "response", None), "status_code", None))
        for code in codes:
            if (category := provider_error_category(code=code, status_code=status)) is not None:
                return category
        statuses.append(status)
        cause = error.__cause__ or error.__context__
        if cause is None:
            break
        error = cause
    for status in statuses:
        if (category := provider_error_category(status_code=status)) is not None:
            return category
    return None


def _categorize_by_exception_type(error: Exception, component: str) -> ErrorCategory | None:
    """Typed-exception checks (most reliable), in precedence order.

    AgentCrashError uses only its adapter-reported category. Other exceptions
    can fall through to the legacy message and component classifiers.
    """
    if isinstance(error, AgentCrashError):
        return error.category or ErrorCategory.AGENT_CRASH

    # Check custom timeout exceptions first (before generic TimeoutError)
    if isinstance(error, EvaluationTimeoutError):
        return ErrorCategory.AGENT_TIMEOUT

    # RunLimits budget breach — typed, distinct from time-based exceptions.
    if isinstance(error, BudgetExceededError):
        return ErrorCategory.BUDGET_EXCEEDED

    # HAZARD: AgentConfigError subclasses RuntimeError, so this typed check must
    # precede any pattern fallback that would re-categorise it as retryable.
    if isinstance(error, AgentConfigError):
        return ErrorCategory.AGENT_CONFIG_ERROR

    if isinstance(error, TimeoutError):
        if component == "agent":
            return ErrorCategory.AGENT_TIMEOUT
        return None  # fall through: let the component group categorize it

    if isinstance(error, FileNotFoundError):
        if "task" in str(error).lower() or "yaml" in str(error).lower():
            return ErrorCategory.TASK_NOT_FOUND
        return None  # fall through: let the component group categorize it

    if isinstance(error, MemoryError):
        return ErrorCategory.OUT_OF_MEMORY

    if (category := provider_exception_category(error)) is not None:
        return category

    # Check for known SDK exceptions (if available)
    try:
        from anthropic import AuthenticationError, RateLimitError

        if isinstance(error, RateLimitError):
            return ErrorCategory.AGENT_RATE_LIMIT
        if isinstance(error, AuthenticationError):
            return ErrorCategory.AGENT_AUTH_ERROR
    except ImportError:
        pass  # anthropic package not installed; fall through to string matching

    return None


def _mentions_status(error_str: str, *codes: str) -> bool:
    """True when an HTTP status code appears as a whole word, so a port or a GUID holding its digits does not match."""
    return any(re.search(rf"\b{code}\b", error_str) for code in codes)


def _categorize_by_message(error_str: str, component: str) -> ErrorCategory | None:
    """String-pattern matching on the already-lowercased error message.

    ``error_str`` is the caller's ``str(error).lower()``. Patterns run in precedence
    order; returns None when no pattern matches (fall through to component
    categorization). The component-gated branches also return ``None`` for
    non-agent components, so e.g. a sandbox pip failure mentioning "connection"
    reaches ``PACKAGE_INSTALL_ERROR`` (retryable) instead of ``UNKNOWN``.
    """
    # Authentication errors
    if any(pat in error_str for pat in ["authentication", "unauthorized", "invalid api key"]) or _mentions_status(
        error_str, "401"
    ):
        return ErrorCategory.AGENT_AUTH_ERROR

    # Billing/credit errors (NOT retryable). Broad patterns are intentional: a false
    # positive skips one retry, a false negative wastes every retry on an error that
    # will never succeed.
    if any(
        pat in error_str for pat in ["credit", "billing", "payment", "insufficient", "quota exceeded", "spending limit"]
    ) or _mentions_status(error_str, "402"):
        return ErrorCategory.AGENT_BILLING_ERROR

    # Rate limiting
    if any(pat in error_str for pat in ["rate limit", "ratelimit", "too many requests"]) or _mentions_status(
        error_str, "429"
    ):
        return ErrorCategory.AGENT_RATE_LIMIT

    # Timeouts
    if "timeout" in error_str:
        if component == "agent":
            return ErrorCategory.AGENT_TIMEOUT
        return None  # fall through: let the component group categorize it

    # Content filtering (Bedrock guardrails) — NOT retryable, same output will be blocked again
    if any(pat in error_str for pat in ["content filtering policy", "content filter", "guardrail"]):
        return ErrorCategory.AGENT_INVALID_OUTPUT

    # API/Network errors
    if any(pat in error_str for pat in ["api error", "connection", "network"]) or _mentions_status(
        error_str, "502", "503", "504"
    ):
        if component == "agent":
            return ErrorCategory.AGENT_API_ERROR
        return None  # fall through: let the component group categorize it

    # Disk errors
    if any(pat in error_str for pat in ["disk", "no space", "enospc"]):
        return ErrorCategory.DISK_FULL

    # Memory errors
    if any(pat in error_str for pat in ["memory", "oom", "out of memory"]):
        return ErrorCategory.OUT_OF_MEMORY

    return None


def _categorize_by_component(error: Exception, error_str: str, component: str) -> ErrorCategory | None:
    """Component-specific categorization for callers that supplied a component hint.

    Returns None **only** when ``component`` is not one of sandbox/agent/evaluator/task,
    so the main function reaches the final AgentCrashError / UNKNOWN fallback.
    """
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
        # Heuristics for a plain RuntimeError that describes a crash without being
        # typed as one; the typed check below catches the agent's wrapped ones.
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
        return ErrorCategory.CRITERION_CHECK_ERROR

    if component == "task":
        if "invalid" in error_str or "malformed" in error_str or "validation" in error_str:
            return ErrorCategory.TASK_INVALID
        return ErrorCategory.TASK_NOT_FOUND

    return None


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

    # Precedence: typed-exception group -> message-string group -> component group.
    # None means "no match in my group, fall through", so a sandbox failure reaches
    # the sandbox categories and their retries below.
    if (category := _categorize_by_exception_type(error, component)) is not None:
        return category

    error_str = str(error).lower()
    if (category := _categorize_by_message(error_str, component)) is not None:
        return category

    if (category := _categorize_by_component(error, error_str, component)) is not None:
        return category

    # Default to unknown
    logger.warning(f"Could not categorize error: {error} (component={component})")
    return ErrorCategory.UNKNOWN
