"""Error categorization, retry logic, and error context capture.

This package provides production-grade error handling with:
- A categorized error taxonomy with per-category retry eligibility
- Exponential backoff retry logic with jitter
- Rich error context capture for debugging
- Actionable error tips for users
"""

from .agent import AgentConfigError, AgentCrashError, format_timeout_reason, truncate_crash_message
from .budget import BudgetExceededError
from .judge import JudgeInfrastructureError
from .timeout import EvaluationTimeoutError, TaskTimeoutError, TurnTimeoutError


__all__ = [
    "AgentConfigError",
    "AgentCrashError",
    "BudgetExceededError",
    "EvaluationTimeoutError",
    "JudgeInfrastructureError",
    "TaskTimeoutError",
    "TurnTimeoutError",
    "format_timeout_reason",
    "truncate_crash_message",
]
