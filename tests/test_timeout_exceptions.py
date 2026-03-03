"""Tests for custom timeout exceptions."""

from coder_eval.errors.timeout import EvaluationTimeoutError, TaskTimeoutError, TurnTimeoutError


class TestTurnTimeoutError:
    """Test TurnTimeoutError exception."""

    def test_fields(self):
        """TurnTimeoutError sets correct fields."""
        err = TurnTimeoutError(120.0, task_id="task_001", iteration=3)
        assert err.timeout_seconds == 120.0
        assert err.layer == "turn"
        assert err.task_id == "task_001"
        assert err.iteration == 3
        assert err.elapsed_seconds is None

    def test_message(self):
        """TurnTimeoutError produces expected message."""
        err = TurnTimeoutError(60.0, iteration=2)
        assert "Agent turn timed out after 60.0s" in str(err)
        assert "iteration 2" in str(err)

    def test_inherits_from_evaluation_timeout(self):
        """TurnTimeoutError inherits from EvaluationTimeoutError."""
        err = TurnTimeoutError(60.0)
        assert isinstance(err, EvaluationTimeoutError)
        assert isinstance(err, Exception)

    def test_does_not_inherit_from_timeout_error(self):
        """TurnTimeoutError does NOT inherit from TimeoutError."""
        err = TurnTimeoutError(60.0)
        assert not isinstance(err, TimeoutError)


class TestTaskTimeoutError:
    """Test TaskTimeoutError exception."""

    def test_fields(self):
        """TaskTimeoutError sets correct fields."""
        err = TaskTimeoutError(600.0, task_id="task_002", elapsed_seconds=601.5)
        assert err.timeout_seconds == 600.0
        assert err.layer == "task"
        assert err.task_id == "task_002"
        assert err.iteration is None
        assert err.elapsed_seconds == 601.5

    def test_message(self):
        """TaskTimeoutError produces expected message."""
        err = TaskTimeoutError(300.0)
        assert "Task timed out after 300.0s" in str(err)

    def test_inherits_from_evaluation_timeout(self):
        """TaskTimeoutError inherits from EvaluationTimeoutError."""
        err = TaskTimeoutError(300.0)
        assert isinstance(err, EvaluationTimeoutError)
        assert isinstance(err, Exception)

    def test_does_not_inherit_from_timeout_error(self):
        """TaskTimeoutError does NOT inherit from TimeoutError."""
        err = TaskTimeoutError(300.0)
        assert not isinstance(err, TimeoutError)


class TestEvaluationTimeoutError:
    """Test EvaluationTimeoutError base class."""

    def test_base_fields(self):
        """EvaluationTimeoutError stores all structured fields."""
        err = EvaluationTimeoutError(
            "test message",
            timeout_seconds=100.0,
            layer="turn",
            task_id="t1",
            iteration=5,
            elapsed_seconds=99.0,
        )
        assert err.timeout_seconds == 100.0
        assert err.layer == "turn"
        assert err.task_id == "t1"
        assert err.iteration == 5
        assert err.elapsed_seconds == 99.0
        assert str(err) == "test message"
