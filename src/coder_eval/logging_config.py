"""Logging configuration for coder_eval.

This module provides centralized logging setup with:
- Color-coded console output (if terminal supports it)
- Optional file logging
- ContextVar-based task_id injection for parallel log isolation
- Customizable log levels
- Per-task log file persistence via context managers
"""

import logging
import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime
from pathlib import Path


# ContextVar that tracks the current task_id for the running async context.
# Each asyncio task gets its own copy, so parallel tasks are isolated.
# Set by task_log_handler; read by _TaskIdFilter to inject into plain-logger records.
_current_task_id: ContextVar[str | None] = ContextVar("_current_task_id", default=None)

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
    for h in list(app_logger.handlers):
        app_logger.removeHandler(h)
        h.close()

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


class _TaskIdFilter(logging.Filter):
    """Filter that injects task_id from ContextVar and accepts only matching records.

    Two responsibilities:
    1. If a record has no task_id, inject from the ContextVar (for plain-logger modules).
    2. Strict equality check — records without task_id are rejected (no cross-contamination).
    """

    def __init__(self, task_id: str):
        super().__init__()
        self.task_id = task_id

    def filter(self, record: logging.LogRecord) -> bool:
        record_task_id = getattr(record, "task_id", None)
        if record_task_id is None:
            cv_task_id = _current_task_id.get()
            if cv_task_id is not None:
                record.task_id = cv_task_id  # type: ignore[attr-defined]
                record_task_id = cv_task_id
        return record_task_id == self.task_id


# Lock for thread-safe handler add/remove and level restoration in parallel batch runs
_task_handler_lock = threading.Lock()


@contextmanager
def task_log_handler(task_log_path: Path, level: int = logging.DEBUG, task_id: str | None = None) -> Generator[None]:
    """Context manager for task-specific logging.

    Automatically adds a FileHandler at the start and removes it at the end,
    guaranteeing cleanup even if exceptions occur.

    When task_id is provided, a filter is applied so that in parallel batch runs
    each task's log file only contains its own messages. The ContextVar
    ``_current_task_id`` is set so that plain loggers (no LoggerAdapter)
    automatically get the correct task_id injected by ``_TaskIdFilter``.

    Thread-safe: uses a lock and reference counting so that concurrent handlers
    correctly restore the original log level when the last handler exits.

    Args:
        task_log_path: Path to task log file
        level: Logging level for file output (default: DEBUG)
        task_id: Optional task ID for filtering in parallel runs

    Yields:
        None (handler is managed internally)

    Example:
        >>> with task_log_handler(Path("task.log"), task_id="my_task"):
        ...     logger.info("This goes to both console and task.log")
    """
    # Create handler
    handler = logging.FileHandler(task_log_path, mode="w", encoding="utf-8")
    handler.setLevel(level)

    # Format with full details for file output
    formatter = logging.Formatter(fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    handler.setFormatter(formatter)

    # Add task_id filter to prevent cross-contamination in parallel runs
    task_filter: _TaskIdFilter | None = None
    if task_id:
        task_filter = _TaskIdFilter(task_id)
        handler.addFilter(task_filter)

    # Thread-safe handler registration with reference-counted level management
    app_logger = logging.getLogger("coder_eval")
    with _task_handler_lock:
        handler_count: int = getattr(app_logger, "_task_handler_count", 0)
        if handler_count == 0:
            # First handler: save the true original level
            app_logger._task_handler_original_level = app_logger.level  # type: ignore[attr-defined]
        app_logger._task_handler_count = handler_count + 1  # type: ignore[attr-defined]
        app_logger.addHandler(handler)
        if app_logger.level > level:
            app_logger.setLevel(level)

    # Set ContextVar so plain loggers in this async context get the correct task_id
    token: Token[str | None] | None = None
    if task_id:
        token = _current_task_id.set(task_id)

    try:
        yield
    finally:
        # Guaranteed cleanup with thread-safe level restoration
        with _task_handler_lock:
            app_logger.removeHandler(handler)
            remaining: int = getattr(app_logger, "_task_handler_count", 1) - 1
            app_logger._task_handler_count = max(remaining, 0)  # type: ignore[attr-defined]
            if remaining <= 0:
                # Last handler: restore the true original level
                original = getattr(app_logger, "_task_handler_original_level", app_logger.level)
                app_logger.setLevel(original)
                if hasattr(app_logger, "_task_handler_original_level"):
                    del app_logger._task_handler_original_level  # pyright: ignore[reportAttributeAccessIssue]
        # Reset ContextVar (token-based reset restores previous value in nested contexts)
        if token is not None:
            _current_task_id.reset(token)
        if task_filter:
            handler.removeFilter(task_filter)
        handler.close()


def aggregate_task_logs(run_dir: Path) -> None:
    """Aggregate all task logs into a single experiment.log file.

    This function should be called after all tasks have completed.
    It creates an experiment.log file by concatenating all task.log files
    with clear separators and metadata.

    Args:
        run_dir: Path to run directory containing task subdirectories

    Example:
        >>> # After running tasks
        >>> aggregate_task_logs(Path("runs/2025-10-16_14-25-18"))
        >>> # Creates: runs/2025-10-16_14-25-18/experiment.log
    """
    run_log_path = run_dir / "experiment.log"
    task_log_paths = sorted(run_dir.glob("**/task.log"))

    if not task_log_paths:
        # No task logs found - create empty experiment.log
        run_log_path.write_text("No task logs found.\n")
        return

    with open(run_log_path, "w", encoding="utf-8") as outfile:
        outfile.write(f"# Run Log: {run_dir.name}\n")
        outfile.write(f"# Aggregated from {len(task_log_paths)} task(s)\n")
        outfile.write(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        outfile.write("\n" + "=" * 80 + "\n\n")

        for task_log_path in task_log_paths:
            # Use the path relative to run_dir so nested task ids from dataset
            # fan-out (variant/suite/row) render with full context, not just
            # the leaf directory name. as_posix() keeps the header consistent
            # across platforms (experiment.log is commonly shared / pasted).
            task_id = task_log_path.parent.relative_to(run_dir).as_posix()
            outfile.write(f"\n{'=' * 80}\n")
            outfile.write(f"TASK: {task_id}\n")
            outfile.write(f"{'=' * 80}\n\n")

            with open(task_log_path, encoding="utf-8") as infile:
                outfile.write(infile.read())

            outfile.write("\n")
