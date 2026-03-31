"""Tests for task log isolation via ContextVar injection."""

import asyncio
import logging

import pytest

from coder_eval.logging_config import _current_task_id, _TaskIdFilter, setup_logging, task_log_handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(task_id: str | None = None) -> logging.LogRecord:
    """Create a LogRecord, optionally stamped with task_id."""
    record = logging.LogRecord(
        name="coder_eval.test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="test",
        args=(),
        exc_info=None,
    )
    if task_id is not None:
        record.task_id = task_id  # type: ignore[attr-defined]
    return record


# ===========================================================================
# Unit: _TaskIdFilter
# ===========================================================================


class TestTaskIdFilter:
    """Unit tests for the ContextVar-aware filter."""

    def test_accepts_matching_task_id(self):
        f = _TaskIdFilter("task_a")
        assert f.filter(_make_record(task_id="task_a")) is True

    def test_rejects_different_task_id(self):
        f = _TaskIdFilter("task_a")
        assert f.filter(_make_record(task_id="task_b")) is False

    def test_rejects_none_task_id(self):
        """Key behavior change: no task_id → rejected (was accepted before)."""
        f = _TaskIdFilter("task_a")
        assert f.filter(_make_record()) is False

    def test_injects_from_contextvar_and_matches(self):
        """Plain-logger record gets task_id from ContextVar, then matches."""
        f = _TaskIdFilter("task_a")
        token = _current_task_id.set("task_a")
        try:
            record = _make_record()
            assert f.filter(record) is True
            assert record.task_id == "task_a"  # type: ignore[attr-defined]
        finally:
            _current_task_id.reset(token)

    def test_injects_from_contextvar_and_rejects(self):
        """Plain-logger record gets task_id from ContextVar, but doesn't match this filter."""
        f = _TaskIdFilter("task_b")
        token = _current_task_id.set("task_a")
        try:
            assert f.filter(_make_record()) is False
        finally:
            _current_task_id.reset(token)

    def test_does_not_overwrite_existing_task_id(self):
        """LoggerAdapter-stamped record is not overwritten by ContextVar."""
        f = _TaskIdFilter("from_adapter")
        token = _current_task_id.set("from_contextvar")
        try:
            record = _make_record(task_id="from_adapter")
            assert f.filter(record) is True
            assert record.task_id == "from_adapter"  # type: ignore[attr-defined]
        finally:
            _current_task_id.reset(token)


# ===========================================================================
# Unit: task_log_handler ContextVar lifecycle
# ===========================================================================


class TestTaskLogHandlerContextVar:
    """Verify task_log_handler sets and resets the ContextVar correctly."""

    def test_sets_and_resets_contextvar(self, tmp_path):
        setup_logging(level="INFO")
        assert _current_task_id.get() is None

        with task_log_handler(tmp_path / "task.log", task_id="my/task"):
            assert _current_task_id.get() == "my/task"

        assert _current_task_id.get() is None

    def test_nested_contexts_restore_previous_value(self, tmp_path):
        """Token-based reset restores previous value, not just None."""
        setup_logging(level="INFO")

        with task_log_handler(tmp_path / "a.log", task_id="outer"):
            assert _current_task_id.get() == "outer"
            with task_log_handler(tmp_path / "b.log", task_id="inner"):
                assert _current_task_id.get() == "inner"
            assert _current_task_id.get() == "outer"

        assert _current_task_id.get() is None

    def test_contextvar_reset_on_exception(self, tmp_path):
        setup_logging(level="INFO")
        with pytest.raises(RuntimeError):  # noqa: SIM117 — nested form avoids code-scanning false positive
            with task_log_handler(tmp_path / "task.log", task_id="crash"):
                raise RuntimeError("boom")
        assert _current_task_id.get() is None


# ===========================================================================
# Integration: Log file isolation
# ===========================================================================


class TestLogFileIsolation:
    """Integration tests for log file content isolation."""

    def test_nested_contexts_isolate_plain_loggers(self, tmp_path):
        """Plain loggers from multiple modules are isolated in nested contexts."""
        setup_logging(level="INFO")
        log_a = tmp_path / "a.log"
        log_b = tmp_path / "b.log"

        orch = logging.getLogger("coder_eval.orchestrator")
        checker = logging.getLogger("coder_eval.evaluation.checker")

        with task_log_handler(log_a, task_id="a"):
            orch.info("orch A")
            checker.info("checker A")
            with task_log_handler(log_b, task_id="b"):
                orch.info("orch B")
                checker.info("checker B")

        content_a = log_a.read_text()
        content_b = log_b.read_text()

        assert "orch A" in content_a and "checker A" in content_a
        assert "orch B" not in content_a and "checker B" not in content_a
        assert "orch B" in content_b and "checker B" in content_b
        assert "orch A" not in content_b and "checker A" not in content_b

    def test_logger_adapter_coexists_with_contextvar(self, tmp_path):
        """LoggerAdapter (proxy) and plain loggers both route correctly."""
        setup_logging(level="INFO")
        log_a = tmp_path / "a.log"
        log_b = tmp_path / "b.log"

        base = logging.getLogger("coder_eval.proxy.server")
        adapter_a = logging.LoggerAdapter(base, extra={"task_id": "a"})
        plain = logging.getLogger("coder_eval.criteria.run_command")

        with task_log_handler(log_a, task_id="a"):
            adapter_a.info("proxy A")
            plain.info("criteria A")
            with task_log_handler(log_b, task_id="b"):
                plain.info("criteria B")

        content_a = log_a.read_text()
        content_b = log_b.read_text()
        assert "proxy A" in content_a and "criteria A" in content_a
        assert "proxy A" not in content_b and "criteria A" not in content_b
        assert "criteria B" in content_b

    def test_handler_without_task_id_captures_all(self, tmp_path):
        """Backward compat: handler without task_id has no filter."""
        setup_logging(level="INFO")
        log_file = tmp_path / "task.log"

        with task_log_handler(log_file):
            logging.getLogger("coder_eval.test").info("test message")

        assert "test message" in log_file.read_text()

    def test_logger_level_restored_after_concurrent_handlers(self, tmp_path):
        setup_logging(level="WARNING")
        app = logging.getLogger("coder_eval")
        original = app.level

        with (
            task_log_handler(tmp_path / "a.log", level=logging.DEBUG, task_id="a"),
            task_log_handler(tmp_path / "b.log", level=logging.DEBUG, task_id="b"),
        ):
            pass

        assert app.level == original


# ===========================================================================
# E2E: Async parallel isolation (primary regression test)
# ===========================================================================


class TestAsyncParallelIsolation:
    """The primary regression test for the contamination bug:
    asyncio.gather with concurrent tasks using plain loggers."""

    def test_three_concurrent_tasks_fully_isolated(self, tmp_path):
        setup_logging(level="INFO")

        orch = logging.getLogger("coder_eval.orchestrator")
        checker = logging.getLogger("coder_eval.evaluation.checker")
        proxy = logging.getLogger("coder_eval.proxy.server")

        # Use unique names that won't match substrings in module names
        task_names = ["alpha", "bravo", "charlie"]

        async def run_task(name: str, log_path):
            with task_log_handler(log_path, task_id=name):
                orch.info(f"orch_{name}")
                checker.info(f"checker_{name}")
                proxy.info(f"proxy_{name}")
                await asyncio.sleep(0.01)
                orch.info(f"done_{name}")

        async def main():
            paths = {n: tmp_path / f"{n}.log" for n in task_names}
            await asyncio.gather(*(run_task(n, paths[n]) for n in paths))
            return paths

        paths = asyncio.run(main())

        for name, path in paths.items():
            content = path.read_text()
            assert f"orch_{name}" in content
            assert f"checker_{name}" in content
            assert f"proxy_{name}" in content
            assert f"done_{name}" in content
            for other in paths:
                if other != name:
                    assert f"_{other}" not in content, f"{name}.log contains {other}'s messages"
