"""Logging configuration for coder_eval.

This module provides centralized logging setup with:
- Color-coded console output (if terminal supports it)
- Optional file logging
- ContextVar-based task_id injection for parallel log isolation
- Customizable log levels
- Per-task log file persistence via context managers
"""

import logging
import re
import sys
import threading
from collections import deque
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .path_utils import TASK_LOG_FILENAME


APP_LOGGER_NAME = "coder_eval"

# Bounded ring-buffer used by ``_LogTailBuffer`` to capture a sanitised tail of
# task logs for the HTML report. Sized so a 200 KB tail comfortably covers the
# last few hundred lines of a typical run without bloating ``task.json``.
DEFAULT_LOG_TAIL_MAX_BYTES = 200_000

# ANSI CSI escape sequences (e.g. ``\x1b[31m``). Stripped from the buffered tail
# only — the on-disk task.log keeps raw bytes so ``tail -f`` renders colour.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# C0 control bytes except ``\t`` (\x09) and ``\n`` (\x0a). DEL (\x7f) is left
# alone — it is rare and harmless in HTML.
_C0_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitise_log_text(text: str) -> str:
    """Strip ANSI CSI escapes and most C0 control bytes; keep ``\\t`` and ``\\n``."""
    text = _ANSI_ESCAPE_RE.sub("", text)
    text = _C0_CONTROL_RE.sub("", text)
    return text


class _LogTailBuffer(logging.Handler):
    """Bounded in-memory ring buffer of formatted log records.

    Eviction is byte-based (UTF-8) so non-ASCII content doesn't blow past the
    cap. Used by ``task_log_handler`` as a sibling of the on-disk file handler;
    ``get_text()`` returns the sanitised concatenation suitable for embedding
    in an HTML report.
    """

    def __init__(self, max_bytes: int = DEFAULT_LOG_TAIL_MAX_BYTES) -> None:
        super().__init__()
        self._max_bytes = max_bytes
        self._records: deque[str] = deque()
        self._size: int = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            self.handleError(record)
            return
        line = msg + "\n"
        line_bytes = len(line.encode("utf-8"))
        self._records.append(line)
        self._size += line_bytes
        while len(self._records) > 1 and self._size > self._max_bytes:
            old = self._records.popleft()
            self._size -= len(old.encode("utf-8"))

    def get_text(self) -> str:
        # Acquire the handler's lock so concurrent emit() calls (which also
        # hold self.lock via Handler.handle()) cannot mutate _records while
        # we iterate it for the join.  Without this, a watchdog/proxy thread
        # logging during the finally-block tail capture would raise
        # "RuntimeError: deque mutated during iteration".
        self.acquire()
        try:
            return _sanitise_log_text("".join(self._records))
        finally:
            self.release()


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
    """Formatter for console output.

    Renders ``HH:MM:SS [LEVEL  ] [task_id] <logger>: <msg>`` where:
    - ``LEVEL`` is left-padded to 7 chars for column alignment. CRITICAL (8)
      overflows by one — acceptable because it's not used in this codebase.
    - ``<logger>`` has the ``coder_eval.`` prefix stripped for readability.
      Non-coder_eval loggers (e.g. ``aiohttp.*``) pass through unchanged.
    - ``[task_id]`` is only rendered when the record carries one.
    """

    _LEVEL_PAD = 7

    def format(self, record: logging.LogRecord) -> str:
        padded_level = record.levelname.ljust(self._LEVEL_PAD)
        if sys.stderr.isatty():
            color = COLORS.get(record.levelname, COLORS["RESET"])
            level_field = f"{color}{padded_level}{COLORS['RESET']}"
        else:
            level_field = padded_level

        name = getattr(record, "name", "") or ""
        display_name = name.removeprefix(f"{APP_LOGGER_NAME}.") if isinstance(name, str) else str(name)

        timestamp = self.formatTime(record, self.datefmt)
        task_id = getattr(record, "task_id", None)
        if task_id:
            return f"{timestamp} [{level_field}] [{task_id}] {display_name}: {record.getMessage()}"
        return f"{timestamp} [{level_field}] {display_name}: {record.getMessage()}"


def _inject_task_id_from_contextvar(record: logging.LogRecord) -> str | None:
    """If ``record`` has no ``task_id`` attribute, copy the value from
    ``_current_task_id`` onto it (when set) and return the final value.

    ``None`` is returned when neither the record nor the ContextVar
    carries a task_id, matching the prior behaviour of both filters.

    Idempotent: an existing ``task_id`` on the record (including empty
    string, which callers may set explicitly to mean "no task_id") is
    preserved and returned as-is.
    """
    record_task_id: str | None = getattr(record, "task_id", None)
    if record_task_id is None:
        cv_task_id = _current_task_id.get()
        if cv_task_id is not None:
            record.task_id = cv_task_id
            return cv_task_id
    return record_task_id


class _ConsoleTaskIdInjector(logging.Filter):
    """Copy ``_current_task_id`` ContextVar onto records via
    ``_inject_task_id_from_contextvar`` so the console formatter can render
    ``[task_id]``. Idempotent: if ``task_id`` is already set (e.g. via
    ``LoggerAdapter`` extra or another filter), it is preserved.
    Always returns True so no records are dropped.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        _inject_task_id_from_contextvar(record)
        return True


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
    app_logger = logging.getLogger(APP_LOGGER_NAME)
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
    console_handler.addFilter(_ConsoleTaskIdInjector())
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
        return _inject_task_id_from_contextvar(record) == self.task_id


# Lock for thread-safe handler add/remove and level restoration in parallel batch runs
_task_handler_lock = threading.Lock()


@dataclass
class _TaskHandlerRefState:
    """Mutable module-level state for ``task_log_handler`` ref counting.

    Replaces the previous monkey-patched attributes on the app logger
    (``_task_handler_count``, ``_task_handler_original_level``). There
    is exactly one app logger (``APP_LOGGER_NAME``), so a single module-
    level instance suffices — no keying needed. All reads and writes
    are guarded by ``_task_handler_lock``.
    """

    count: int = 0
    original_level: int | None = None


_task_handler_state: _TaskHandlerRefState = _TaskHandlerRefState()


@contextmanager
def task_log_handler(
    task_log_file: Path, level: int = logging.DEBUG, task_id: str | None = None
) -> Generator[_LogTailBuffer]:
    """Context manager for task-specific logging.

    Attaches a ``FileHandler`` (raw bytes, uncapped) and a sibling
    ``_LogTailBuffer`` (sanitised, bounded) to the app logger at the start and
    removes both at the end, guaranteeing cleanup even if exceptions occur. The
    buffer is yielded directly so callers can call ``get_text()`` to capture a
    sanitised tail of the log alongside the on-disk file.

    When task_id is provided, a filter is applied so that in parallel batch runs
    each task's log file only contains its own messages. The ContextVar
    ``_current_task_id`` is set so that plain loggers (no LoggerAdapter)
    automatically get the correct task_id injected by ``_TaskIdFilter``.

    Thread-safe: uses a lock and reference counting so that concurrent handlers
    correctly restore the original log level when the last handler exits. The
    buffer is a passive sibling and does NOT participate in the refcount.

    Args:
        task_log_file: Path to task log file
        level: Logging level for file output (default: DEBUG)
        task_id: Optional task ID for filtering in parallel runs

    Yields:
        ``_LogTailBuffer`` exposing ``get_text()`` for the sanitised log tail.

    Example:
        >>> with task_log_handler(Path("task.log"), task_id="my_task") as log_tail:
        ...     logger.info("This goes to both console and task.log")
        ...     tail_text = log_tail.get_text()
    """
    # Create handler
    handler = logging.FileHandler(task_log_file, mode="w", encoding="utf-8")
    handler.setLevel(level)

    # Format with full details for file output
    formatter = logging.Formatter(fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    handler.setFormatter(formatter)

    # Sibling in-memory buffer; same level + formatter so the tail mirrors the
    # file content (modulo sanitisation done at get_text() time).
    tail_buffer = _LogTailBuffer()
    tail_buffer.setLevel(level)
    tail_buffer.setFormatter(formatter)

    # Add task_id filter to prevent cross-contamination in parallel runs.
    # Both handlers share an equivalent filter so the buffer is isolated too.
    task_filter: _TaskIdFilter | None = None
    buffer_filter: _TaskIdFilter | None = None
    if task_id:
        task_filter = _TaskIdFilter(task_id)
        buffer_filter = _TaskIdFilter(task_id)
        handler.addFilter(task_filter)
        tail_buffer.addFilter(buffer_filter)

    # Thread-safe handler registration with reference-counted level management.
    # Refcount tracks the file handler only; the buffer rides along.
    app_logger = logging.getLogger(APP_LOGGER_NAME)
    with _task_handler_lock:
        if _task_handler_state.count == 0:
            _task_handler_state.original_level = app_logger.level
        _task_handler_state.count += 1
        app_logger.addHandler(handler)
        app_logger.addHandler(tail_buffer)
        if app_logger.level > level:
            app_logger.setLevel(level)

    # Set ContextVar so plain loggers in this async context get the correct task_id
    token: Token[str | None] | None = None
    if task_id:
        token = _current_task_id.set(task_id)

    try:
        yield tail_buffer
    finally:
        # Guaranteed cleanup with thread-safe level restoration
        with _task_handler_lock:
            app_logger.removeHandler(handler)
            app_logger.removeHandler(tail_buffer)
            _task_handler_state.count = max(_task_handler_state.count - 1, 0)
            if _task_handler_state.count == 0:
                restored = _task_handler_state.original_level
                if restored is not None:
                    app_logger.setLevel(restored)
                _task_handler_state.original_level = None
        # Reset ContextVar (token-based reset restores previous value in nested contexts)
        if token is not None:
            _current_task_id.reset(token)
        handler.close()
        tail_buffer.close()


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
    task_log_paths = sorted(run_dir.glob(f"**/{TASK_LOG_FILENAME}"))

    if not task_log_paths:
        # No task logs found - create empty experiment.log
        run_log_path.write_text("No task logs found.\n", encoding="utf-8")
        return

    with open(run_log_path, "w", encoding="utf-8") as outfile:
        outfile.write(f"# Run Log: {run_dir.name}\n")
        outfile.write(f"# Aggregated from {len(task_log_paths)} task(s)\n")
        outfile.write(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        outfile.write("\n" + "=" * 80 + "\n\n")

        for task_log_file in task_log_paths:
            # Use the path relative to run_dir so nested task ids from dataset
            # fan-out (variant/suite/row) render with full context, not just
            # the leaf directory name. as_posix() keeps the header consistent
            # across platforms (experiment.log is commonly shared / pasted).
            task_id = task_log_file.parent.relative_to(run_dir).as_posix()
            outfile.write(f"\n{'=' * 80}\n")
            outfile.write(f"TASK: {task_id}\n")
            outfile.write(f"{'=' * 80}\n\n")

            with open(task_log_file, encoding="utf-8") as infile:
                outfile.write(infile.read())

            outfile.write("\n")
