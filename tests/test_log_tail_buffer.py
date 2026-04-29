"""Tests for the in-memory log tail buffer used by task_log_handler."""

import asyncio
import logging
import threading
from pathlib import Path

import pytest

from coder_eval.logging_config import (
    DEFAULT_LOG_TAIL_MAX_BYTES,
    _LogTailBuffer,
    _sanitise_log_text,
    setup_logging,
    task_log_handler,
)


def _make_record(msg: str, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="coder_eval.test",
        level=level,
        pathname="",
        lineno=0,
        msg=msg,
        args=(),
        exc_info=None,
    )


def _emit(buffer: _LogTailBuffer, msg: str) -> None:
    buffer.emit(_make_record(msg))


def test_buffer_captures_recent_lines_under_cap():
    buffer = _LogTailBuffer()
    buffer.setFormatter(logging.Formatter("%(message)s"))
    for i in range(5):
        _emit(buffer, f"line-{i}")
    text = buffer.get_text()
    for i in range(5):
        assert f"line-{i}" in text


def test_buffer_evicts_oldest_over_cap():
    buffer = _LogTailBuffer(max_bytes=200)
    buffer.setFormatter(logging.Formatter("%(message)s"))
    # Each line is roughly 50 bytes after the newline; emit enough to overflow.
    for i in range(20):
        _emit(buffer, f"message-number-{i:04d}-with-extra-padding-text")
    text = buffer.get_text()
    assert "message-number-0000" not in text
    assert "message-number-0019" in text


def test_buffer_strips_ansi_codes():
    buffer = _LogTailBuffer()
    buffer.setFormatter(logging.Formatter("%(message)s"))
    _emit(buffer, "\x1b[31mERROR\x1b[0m happened")
    text = buffer.get_text()
    assert "ERROR" in text
    assert "happened" in text
    assert "\x1b" not in text


def test_buffer_strips_c0_controls_keeps_newline_and_tab():
    buffer = _LogTailBuffer()
    buffer.setFormatter(logging.Formatter("%(message)s"))
    _emit(buffer, "before\x00\x07middle\tafter\nline")
    text = buffer.get_text()
    assert "\x00" not in text
    assert "\x07" not in text
    assert "\t" in text
    assert "\n" in text
    assert "beforemiddle\tafter" in text


def test_buffer_handles_unicode_byte_count():
    # Emoji + CJK chars take 3-4 UTF-8 bytes each — eviction must use byte
    # counts so the cap actually bounds memory.
    buffer = _LogTailBuffer(max_bytes=80)
    buffer.setFormatter(logging.Formatter("%(message)s"))
    for i in range(20):
        _emit(buffer, f"日本語テスト-{i}")
    text = buffer.get_text()
    encoded_size = len(text.encode("utf-8"))
    # Allow one over-cap line because eviction never drains to empty.
    assert encoded_size <= 200, f"size {encoded_size} too large"
    assert "日本語テスト-19" in text


def test_buffer_keeps_single_oversized_record():
    buffer = _LogTailBuffer(max_bytes=10)
    buffer.setFormatter(logging.Formatter("%(message)s"))
    big_msg = "x" * 1000
    _emit(buffer, big_msg)
    text = buffer.get_text()
    assert big_msg in text


def test_get_text_is_thread_safe():
    """get_text() must not raise RuntimeError when emit() runs concurrently.

    Without the lock, deque iteration in get_text() can race with popleft()
    in emit()'s eviction loop, producing 'deque mutated during iteration'.
    """
    buffer = _LogTailBuffer(max_bytes=100)  # small cap forces frequent eviction
    buffer.setFormatter(logging.Formatter("%(message)s"))

    errors: list[Exception] = []
    stop_event = threading.Event()

    def emitter() -> None:
        i = 0
        while not stop_event.is_set():
            buffer.emit(_make_record(f"line-{i}-padding-padding-padding"))
            i += 1

    def reader() -> None:
        while not stop_event.is_set():
            try:
                buffer.get_text()
            except RuntimeError as e:
                errors.append(e)
                stop_event.set()

    t1 = threading.Thread(target=emitter)
    t2 = threading.Thread(target=reader)
    t1.start()
    t2.start()
    stop_event.wait(timeout=0.5)
    stop_event.set()
    t1.join()
    t2.join()

    assert not errors, f"Thread safety violation in get_text(): {errors[0]}"


def test_sanitise_log_text_idempotent():
    s = _sanitise_log_text("\x1b[31mhello\x1b[0m\nworld")
    assert _sanitise_log_text(s) == s


def test_default_log_tail_max_bytes_is_200kb():
    assert DEFAULT_LOG_TAIL_MAX_BYTES == 200_000


def test_task_log_handler_yields_handle_with_get_text(tmp_path: Path):
    setup_logging(level="INFO")
    log_file = tmp_path / "task.log"
    logger = logging.getLogger("coder_eval.tail_test")
    raw_msg = "tail-\x1b[33mwarn\x1b[0m"

    with task_log_handler(log_file) as log_tail:
        logger.info(raw_msg)
        tail_text = log_tail.get_text()

    # Buffer is sanitised
    assert "warn" in tail_text
    assert "\x1b" not in tail_text

    # On-disk file keeps the raw escape bytes
    file_content = log_file.read_text()
    assert "\x1b" in file_content
    assert "warn" in file_content


def test_task_log_handler_buffer_isolated_per_task_id(tmp_path: Path):
    setup_logging(level="INFO")
    log_a = tmp_path / "a.log"
    log_b = tmp_path / "b.log"
    logger = logging.getLogger("coder_eval.iso_test")

    with task_log_handler(log_a, task_id="task-a") as tail_a:
        logger.info("hello-from-a", extra={"task_id": "task-a"})
        with task_log_handler(log_b, task_id="task-b") as tail_b:
            logger.info("hello-from-b", extra={"task_id": "task-b"})
            text_b = tail_b.get_text()
        text_a = tail_a.get_text()

    assert "hello-from-a" in text_a
    assert "hello-from-b" not in text_a
    assert "hello-from-b" in text_b
    assert "hello-from-a" not in text_b


@pytest.mark.asyncio
async def test_task_log_handler_buffer_isolated_across_async_tasks(tmp_path: Path):
    """Parallel asyncio tasks each get their own buffer scoped via the
    _current_task_id ContextVar, mirroring how run_batch dispatches tasks."""
    setup_logging(level="INFO")
    logger = logging.getLogger("coder_eval.async_iso_test")

    captured: dict[str, str] = {}

    async def run_one(task_id: str, log_file: Path) -> None:
        with task_log_handler(log_file, task_id=task_id) as tail:
            for i in range(3):
                logger.info("msg-from-%s-%d", task_id, i)
                await asyncio.sleep(0)
            captured[task_id] = tail.get_text()

    await asyncio.gather(
        run_one("alpha", tmp_path / "alpha.log"),
        run_one("beta", tmp_path / "beta.log"),
        run_one("gamma", tmp_path / "gamma.log"),
    )

    for owner in ("alpha", "beta", "gamma"):
        text = captured[owner]
        assert f"msg-from-{owner}-0" in text
        assert f"msg-from-{owner}-2" in text
        for other in ("alpha", "beta", "gamma"):
            if other != owner:
                assert f"msg-from-{other}-" not in text, f"buffer for {owner} contained messages from {other}"
