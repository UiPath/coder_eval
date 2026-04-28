"""Tests for timeout error categorization."""

from coder_eval.errors.categories import ErrorCategory
from coder_eval.errors.categorization import categorize_error
from coder_eval.errors.timeout import TaskTimeoutError, TurnTimeoutError


class TestTimeoutCategorization:
    """Test that custom timeout exceptions are categorized correctly."""

    def test_turn_timeout_with_agent_component(self):
        """TurnTimeoutError with agent component -> AGENT_TIMEOUT."""
        err = TurnTimeoutError(60.0, task_id="t1", iteration=1)
        result = categorize_error(err, {"component": "agent"})
        assert result == ErrorCategory.AGENT_TIMEOUT

    def test_task_timeout_with_agent_component(self):
        """TaskTimeoutError with agent component -> AGENT_TIMEOUT."""
        err = TaskTimeoutError(300.0, task_id="t1")
        result = categorize_error(err, {"component": "agent"})
        assert result == ErrorCategory.AGENT_TIMEOUT

    def test_turn_timeout_without_component(self):
        """TurnTimeoutError without component -> AGENT_TIMEOUT (regardless)."""
        err = TurnTimeoutError(60.0)
        result = categorize_error(err, {})
        assert result == ErrorCategory.AGENT_TIMEOUT

    def test_task_timeout_without_component(self):
        """TaskTimeoutError without component -> AGENT_TIMEOUT (regardless)."""
        err = TaskTimeoutError(300.0)
        result = categorize_error(err, {})
        assert result == ErrorCategory.AGENT_TIMEOUT

    def test_generic_timeout_error_unchanged(self):
        """Regular TimeoutError with agent component still works as before."""
        err = TimeoutError("generic timeout")
        result = categorize_error(err, {"component": "agent"})
        assert result == ErrorCategory.AGENT_TIMEOUT

    def test_hint_overrides_custom_timeout(self):
        """Explicit hint overrides even custom timeout errors."""
        err = TurnTimeoutError(60.0)
        result = categorize_error(err, {}, hint=ErrorCategory.UNKNOWN)
        assert result == ErrorCategory.UNKNOWN
