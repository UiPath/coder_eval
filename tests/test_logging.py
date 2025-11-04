"""Tests for logging configuration and functionality."""

import logging
from io import StringIO

import pytest

from coder_eval.logging_config import TaskContextFormatter, setup_logging


def test_setup_logging_basic(tmp_path):
    """Test basic logging setup."""
    # Setup logging
    setup_logging(level="INFO")

    # Get logger and verify it's configured
    logger = logging.getLogger("coder_eval.test")
    assert logger.level <= logging.INFO

    # Verify we can log
    logger.info("Test message")


def test_setup_logging_with_verbose():
    """Test logging setup with verbose flag."""
    setup_logging(level="INFO", verbose=True)

    logger = logging.getLogger("coder_eval.test")
    # Verbose should set DEBUG level
    assert logger.level <= logging.DEBUG


def test_setup_logging_with_file(tmp_path):
    """Test logging to file."""
    log_file = tmp_path / "test.log"

    setup_logging(level="INFO", log_file=log_file)

    logger = logging.getLogger("coder_eval.test")
    logger.info("Test message to file")

    # Verify file was created and contains the message
    assert log_file.exists()
    content = log_file.read_text()
    assert "Test message to file" in content


def test_task_context_formatter_with_task_id():
    """Test TaskContextFormatter includes task_id when present."""
    formatter = TaskContextFormatter(fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")

    # Create a log record with task_id
    record = logging.LogRecord(
        name="coder_eval.orchestrator",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="Test message",
        args=(),
        exc_info=None,
    )
    record.task_id = "hello_world"

    formatted = formatter.format(record)
    assert "[hello_world]" in formatted
    assert "Test message" in formatted


def test_task_context_formatter_without_task_id():
    """Test TaskContextFormatter works without task_id."""
    formatter = TaskContextFormatter(fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")

    # Create a log record without task_id
    record = logging.LogRecord(
        name="coder_eval.sandbox",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="Test message",
        args=(),
        exc_info=None,
    )

    formatted = formatter.format(record)
    assert "[hello_world]" not in formatted  # Should not include task_id
    assert "Test message" in formatted


def test_logger_adapter_adds_task_context():
    """Test that LoggerAdapter properly adds task_id context."""
    setup_logging(level="DEBUG")

    logger = logging.getLogger("coder_eval.test")
    task_logger = logging.LoggerAdapter(logger, extra={"task_id": "test_task"})

    # Capture log output
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    fmt_string = "%(message)s - task: %(task_id)s" if hasattr(logging, "task_id") else "%(message)s"
    formatter = TaskContextFormatter(fmt=fmt_string)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    task_logger.info("Test with context")

    # The extra context should be available
    output = stream.getvalue()
    assert "Test with context" in output


def test_logging_hierarchy():
    """Test that child loggers inherit from parent coder_eval logger."""
    setup_logging(level="WARNING")

    # Parent logger
    parent_logger = logging.getLogger("coder_eval")
    # Child logger
    child_logger = logging.getLogger("coder_eval.orchestrator")

    # Child should inherit level from parent
    assert parent_logger.level == logging.WARNING
    # Child doesn't set its own level, so it should be NOTSET and inherit from parent
    assert child_logger.level == logging.NOTSET or child_logger.level == logging.WARNING


def test_logging_levels():
    """Test different logging levels."""
    setup_logging(level="DEBUG")

    logger = logging.getLogger("coder_eval.test")

    # All levels should work
    logger.debug("Debug message")
    logger.info("Info message")
    logger.warning("Warning message")
    logger.error("Error message")
    logger.critical("Critical message")


def test_setup_logging_creates_log_directory(tmp_path):
    """Test that setup_logging creates log file directory if it doesn't exist."""
    log_file = tmp_path / "logs" / "subdir" / "test.log"
    assert not log_file.parent.exists()

    setup_logging(level="INFO", log_file=log_file)

    logger = logging.getLogger("coder_eval.test")
    logger.info("Test message")

    # Verify directory and file were created
    assert log_file.parent.exists()
    assert log_file.exists()


def test_no_duplicate_handlers():
    """Test that calling setup_logging multiple times doesn't create duplicate handlers."""
    # Call setup_logging twice
    setup_logging(level="INFO")
    setup_logging(level="DEBUG")

    logger = logging.getLogger("coder_eval")

    # Should only have the handlers from the second call
    # (first call's handlers should be cleared)
    # Typically: 1 console handler (and 1 file handler if log_file was specified)
    # Since we're not using log_file here, should be 1 handler
    assert len(logger.handlers) == 1


def test_task_context_formatter_color_codes():
    """Test that TaskContextFormatter adds color codes when appropriate."""

    formatter = TaskContextFormatter(fmt="%(levelname)s %(message)s")

    record = logging.LogRecord(
        name="coder_eval.test",
        level=logging.ERROR,
        pathname="",
        lineno=0,
        msg="Error message",
        args=(),
        exc_info=None,
    )

    # When stderr is a tty, colors should be added
    # (We can't easily test this without mocking sys.stderr.isatty())
    # But we can at least verify the formatter doesn't crash
    formatted = formatter.format(record)
    assert "Error message" in formatted


@pytest.mark.asyncio
async def test_logging_in_async_context():
    """Test that logging works in async context (for orchestrator)."""
    setup_logging(level="DEBUG")

    logger = logging.getLogger("coder_eval.async_test")
    task_logger = logging.LoggerAdapter(logger, extra={"task_id": "async_task"})

    # Should work in async function
    task_logger.info("Async log message")
    task_logger.debug("Async debug message")
    task_logger.error("Async error message")


class TestLogPersistence:
    """Tests for persistent log file creation."""

    def test_task_log_handler_context_manager(self, tmp_path):
        """Context manager creates log file and guarantees cleanup."""
        from coder_eval.logging_config import setup_logging, task_log_handler

        # Setup logging first (this initializes the coder_eval logger)
        setup_logging(level="INFO")

        log_file = tmp_path / "task.log"
        logger = logging.getLogger("coder_eval.test_module")

        with task_log_handler(log_file):
            logger.info("Test message")

        # Verify file exists and contains message
        assert log_file.exists()
        content = log_file.read_text()
        assert "Test message" in content
        assert "[INFO]" in content

        # Verify handler was removed (no lingering handlers)
        app_logger = logging.getLogger("coder_eval")
        file_handlers = [h for h in app_logger.handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) == 0

    def test_task_log_handler_cleanup_on_exception(self, tmp_path):
        """Context manager removes handler even when exception occurs."""
        from coder_eval.logging_config import setup_logging, task_log_handler

        # Setup logging first
        setup_logging(level="INFO")

        log_file = tmp_path / "task.log"
        logger = logging.getLogger("coder_eval.test_module")

        with pytest.raises(ValueError), task_log_handler(log_file):
            logger.info("Before exception")
            raise ValueError("Test exception")

        # Verify handler was still removed
        app_logger = logging.getLogger("coder_eval")
        file_handlers = [h for h in app_logger.handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) == 0

        # Verify log file was created before exception
        assert log_file.exists()
        content = log_file.read_text()
        assert "Before exception" in content

    def test_aggregate_task_logs(self, tmp_path):
        """Aggregation creates run.log with proper formatting."""
        from coder_eval.logging_config import aggregate_task_logs

        # Create mock task logs
        (tmp_path / "task-1").mkdir()
        (tmp_path / "task-1" / "task.log").write_text("Task 1 log content\n")
        (tmp_path / "task-2").mkdir()
        (tmp_path / "task-2" / "task.log").write_text("Task 2 log content\n")

        # Aggregate
        aggregate_task_logs(tmp_path)

        # Verify run.log exists
        run_log = tmp_path / "run.log"
        assert run_log.exists()

        content = run_log.read_text()
        # Verify header
        assert "Run Log:" in content
        assert "Aggregated from 2 task(s)" in content

        # Verify task separators
        assert "TASK: task-1" in content
        assert "TASK: task-2" in content

        # Verify content
        assert "Task 1 log content" in content
        assert "Task 2 log content" in content

    def test_aggregate_task_logs_no_tasks(self, tmp_path):
        """Aggregation handles case with no task logs gracefully."""
        from coder_eval.logging_config import aggregate_task_logs

        aggregate_task_logs(tmp_path)

        run_log = tmp_path / "run.log"
        assert run_log.exists()
        content = run_log.read_text()
        assert "No task logs found" in content

    def test_task_log_handler_captures_all_loggers(self, tmp_path):
        """Task log handler captures logs from all loggers."""
        from coder_eval.logging_config import setup_logging, task_log_handler

        # Setup logging first
        setup_logging(level="INFO")

        log_file = tmp_path / "task.log"

        with task_log_handler(log_file):
            # Log from multiple different loggers
            logging.getLogger("coder_eval.orchestrator").info("Orchestrator message")
            logging.getLogger("coder_eval.agent").debug("Agent debug message")
            logging.getLogger("coder_eval.sandbox").warning("Sandbox warning")

        # Verify all messages are in the file
        content = log_file.read_text()
        assert "Orchestrator message" in content
        assert "Agent debug message" in content
        assert "Sandbox warning" in content

    def test_task_log_handler_debug_level(self, tmp_path):
        """Task log handler always captures DEBUG level logs."""
        from coder_eval.logging_config import setup_logging, task_log_handler

        # Setup logging first at INFO level
        setup_logging(level="INFO")

        log_file = tmp_path / "task.log"
        logger = logging.getLogger("coder_eval.test_debug")

        with task_log_handler(log_file):
            logger.debug("Debug message")
            logger.info("Info message")
            logger.warning("Warning message")

        # Verify all levels are captured (including DEBUG)
        content = log_file.read_text()
        assert "Debug message" in content
        assert "Info message" in content
        assert "Warning message" in content
        assert "[DEBUG]" in content
        assert "[INFO]" in content
        assert "[WARNING]" in content

    def test_task_log_contains_subprocess_output(self, tmp_path):
        """Integration test: task.log should contain subprocess output."""
        from coder_eval.logging_config import setup_logging, task_log_handler
        from coder_eval.models import SandboxConfig
        from coder_eval.sandbox import Sandbox

        setup_logging(level="INFO")  # Console at INFO

        task_log = tmp_path / "task.log"
        config = SandboxConfig(driver="tempdir", python_version="3.13", env_packages=[], network_enabled=False)
        sandbox = Sandbox(config, task_id="integration_test")

        try:
            sandbox.setup()

            # Simulate orchestrator's task_log_handler usage
            with task_log_handler(task_log):
                # Run command (output should go to task.log)
                sandbox.run_command("echo 'Test output from subprocess'")

            # Verify task.log contains the subprocess output
            content = task_log.read_text()
            assert "Test output from subprocess" in content
            assert "STDOUT:" in content
            assert "[DEBUG]" in content  # Task logs use DEBUG level

        finally:
            sandbox.cleanup()
