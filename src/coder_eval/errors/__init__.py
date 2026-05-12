"""Error categorization, retry logic, and error context capture.

This package provides production-grade error handling with:
- 20 error categories with retry eligibility
- Exponential backoff retry logic with jitter
- Rich error context capture for debugging
- Actionable error tips for users
"""

from .agent import AgentCrashError, format_timeout_reason, truncate_crash_message
from .budget import BudgetExceededError
from .timeout import EvaluationTimeoutError, TaskTimeoutError, TurnTimeoutError


__all__ = [
    "AgentCrashError",
    "BudgetExceededError",
    "EvaluationTimeoutError",
    "TaskTimeoutError",
    "TurnTimeoutError",
    "format_timeout_reason",
    "truncate_crash_message",
]
