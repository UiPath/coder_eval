"""Logging configuration for coder_eval.

This module provides centralized logging setup with:
- Color-coded console output (if terminal supports it)
- Optional file logging
- Task-specific context via LoggerAdapter
- Customizable log levels
- Per-task log file persistence via context managers
"""

import logging
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


# ANSI color codes for terminal output
COLORS = {
    "DEBUG": "\033[36m",  # Cyan
    "INFO": "\033[32m",  # Green
    "WARNING": "\033[33m",  # Yellow
    "ERROR": "\033[31m",  # Red
    "CRITICAL": "\033[35m",  # Magenta
    "RESET": "\033[0m",  # Reset
}


class TaskContextFormatter(logging.Formatter):
    """Formatter that includes task_id if present in LogRecord extra."""

    def format(self, record: logging.LogRecord) -> str:
        """Format log record with optional task_id context.

        Args:
            record: Log record to format

        Returns:
            Formatted log string
        """
        # Apply color codes if outputting to terminal
        if sys.stderr.isatty():
            levelname = record.levelname
            color = COLORS.get(levelname, COLORS["RESET"])
            colored_levelname = f"{color}{levelname}{COLORS['RESET']}"
        else:
            colored_levelname = record.levelname

        # Include task_id if present in extra context
        timestamp = self.formatTime(record, self.datefmt)
        task_id = getattr(record, "task_id", None)
        if task_id:
            return f"{timestamp} [{colored_levelname}] [{task_id}] {record.name}: {record.getMessage()}"
        else:
            # Standard format without task_id
            return f"{timestamp} [{colored_levelname}] {record.name}: {record.getMessage()}"


def setup_logging(level: str = "INFO", log_file: Path | None = None, verbose: bool = False) -> None:
    """Configure logging for the application.

    This configures the 'coder_eval' top-level logger and all child loggers will inherit.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Optional path to log file
        verbose: If True, set level to DEBUG (overrides level parameter)

    Example:
        >>> setup_logging(level="INFO", log_file=Path("run.log"))
        >>> logger = logging.getLogger(__name__)
        >>> logger.info("Application started")
    """
    # Determine effective log level
    log_level = logging.DEBUG if verbose else getattr(logging, level.upper(), logging.INFO)

    # Get the top-level coder_eval logger (all module loggers will inherit from this)
    app_logger = logging.getLogger("coder_eval")
    app_logger.setLevel(log_level)
    app_logger.handlers.clear()  # Remove any existing handlers

    # Console handler (stderr) with colored output
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(log_level)
    console_formatter = TaskContextFormatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"
    )
    console_handler.setFormatter(console_formatter)
    app_logger.addHandler(console_handler)

    # File handler (optional)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(log_level)
        # File logs don't need colors
        file_formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
        file_handler.setFormatter(file_formatter)
        app_logger.addHandler(file_handler)

    # Prevent propagation to root logger (avoid duplicate logs)
    app_logger.propagate = False

    # Log initial configuration (at DEBUG level so it doesn't clutter INFO output)
    app_logger.debug(f"Logging configured: level={logging.getLevelName(log_level)}, file={log_file}")


@contextmanager
def task_log_handler(task_log_path: Path, level: int = logging.DEBUG):
    """Context manager for task-specific logging.

    Automatically adds a FileHandler at the start and removes it at the end,
    guaranteeing cleanup even if exceptions occur.

    Args:
        task_log_path: Path to task log file
        level: Logging level for file output (default: DEBUG)

    Yields:
        None (handler is managed internally)

    Example:
        >>> with task_log_handler(Path("task.log")):
        ...     logger.info("This goes to both console and task.log")
    """
    # Create handler
    handler = logging.FileHandler(task_log_path, mode="w", encoding="utf-8")
    handler.setLevel(level)

    # Format with full details for file output
    formatter = logging.Formatter(fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    handler.setFormatter(formatter)

    # Add to coder_eval logger (which is the parent for all our loggers)
    # This ensures we capture all coder_eval.* logs
    app_logger = logging.getLogger("coder_eval")
    app_logger.addHandler(handler)

    # Also ensure app logger level allows DEBUG messages
    original_level = app_logger.level
    if app_logger.level > level:
        app_logger.setLevel(level)

    try:
        yield
    finally:
        # Guaranteed cleanup
        app_logger.removeHandler(handler)
        handler.close()
        # Restore original level
        app_logger.setLevel(original_level)


def aggregate_task_logs(run_dir: Path) -> None:
    """Aggregate all task logs into a single run.log file.

    This function should be called after all tasks have completed.
    It creates a run.log file by concatenating all task.log files
    with clear separators and metadata.

    Args:
        run_dir: Path to run directory containing task subdirectories

    Example:
        >>> # After running tasks
        >>> aggregate_task_logs(Path("runs/2025-10-16_14-25-18"))
        >>> # Creates: runs/2025-10-16_14-25-18/run.log
    """
    run_log_path = run_dir / "run.log"
    task_log_paths = sorted(run_dir.glob("*/task.log"))

    if not task_log_paths:
        # No task logs found - create empty run.log
        run_log_path.write_text("No task logs found.\n")
        return

    with open(run_log_path, "w", encoding="utf-8") as outfile:
        outfile.write(f"# Run Log: {run_dir.name}\n")
        outfile.write(f"# Aggregated from {len(task_log_paths)} task(s)\n")
        outfile.write(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        outfile.write("\n" + "=" * 80 + "\n\n")

        for task_log_path in task_log_paths:
            task_id = task_log_path.parent.name
            outfile.write(f"\n{'=' * 80}\n")
            outfile.write(f"TASK: {task_id}\n")
            outfile.write(f"{'=' * 80}\n\n")

            with open(task_log_path, encoding="utf-8") as infile:
                outfile.write(infile.read())

            outfile.write("\n")
