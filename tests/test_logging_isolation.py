"""Tests for task log isolation in parallel batch runs.

Validates that task_log_handler correctly filters logs by task_id
and restores logger level after concurrent handlers exit.
"""

import logging

from coder_eval.logging_config import setup_logging, task_log_handler


class TestTaskLogIsolation:
    """Verify task_log_handler isolates logs by task_id in parallel runs."""

    def setup_method(self):
        """Save logger state before each test."""
        app_logger = logging.getLogger("coder_eval")
        self._orig_propagate = app_logger.propagate
        self._orig_level = app_logger.level
        self._orig_handlers = list(app_logger.handlers)

    def teardown_method(self):
        """Restore logger state after each test to avoid polluting other tests."""
        app_logger = logging.getLogger("coder_eval")
        app_logger.propagate = self._orig_propagate
        app_logger.setLevel(self._orig_level)
        app_logger.handlers = self._orig_handlers

    def test_parallel_task_logs_are_isolated_with_task_id(self, tmp_path):
        """When task_id is provided, each task's log should only contain its own messages."""
        setup_logging(level="INFO")

        log_file_a = tmp_path / "task_a.log"
        log_file_b = tmp_path / "task_b.log"

        base_logger = logging.getLogger("coder_eval.orchestrator")
        logger_a = logging.LoggerAdapter(base_logger, extra={"task_id": "task_a"})
        logger_b = logging.LoggerAdapter(base_logger, extra={"task_id": "task_b"})

        with task_log_handler(log_file_a, task_id="task_a"):
            logger_a.info("Message from task A")

            with task_log_handler(log_file_b, task_id="task_b"):
                logger_b.info("Message from task B")
                logger_a.info("Another message from task A during overlap")

        content_a = log_file_a.read_text()
        content_b = log_file_b.read_text()

        assert "Message from task A" in content_a
        assert "Another message from task A during overlap" in content_a
        assert "Another message from task A" not in content_b, (
            "Task B's log contains task A's messages (cross-contamination)"
        )
        assert "Message from task B" in content_b

    def test_logger_level_restored_correctly_after_concurrent_handlers(self, tmp_path):
        """Logger level should be restored correctly even with nested handlers."""
        setup_logging(level="WARNING")

        app_logger = logging.getLogger("coder_eval")
        original_level = app_logger.level

        log_file_a = tmp_path / "task_a.log"
        log_file_b = tmp_path / "task_b.log"

        with (
            task_log_handler(log_file_a, level=logging.DEBUG, task_id="a"),
            task_log_handler(log_file_b, level=logging.DEBUG, task_id="b"),
        ):
            pass

        assert app_logger.level == original_level, (
            f"Logger level not restored correctly. Expected {original_level}, got {app_logger.level}"
        )

    def test_task_log_handler_without_task_id_still_works(self, tmp_path):
        """Backward compatibility: handler without task_id captures all logs."""
        setup_logging(level="INFO")

        log_file = tmp_path / "task.log"
        logger = logging.getLogger("coder_eval.test_module")

        with task_log_handler(log_file):
            logger.info("Test message")

        content = log_file.read_text()
        assert "Test message" in content
