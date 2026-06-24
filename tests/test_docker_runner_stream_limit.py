"""Regression: the docker stdout reader must not choke on a large wire line.

A single ``STREAM_EVENT`` line carrying a large tool input (an agent Write of a
whole file) exceeds asyncio's default 64 KiB ``StreamReader`` line cap. Before
the ``limit=`` fix, that raised ``ValueError`` mid-stream and tore the container
down before it wrote ``task.json`` -- the whole task was lost and the host
recorded a bare ERROR with no per-task report (blank dashboard page).

See ``docker_runner.STDOUT_LINE_LIMIT_BYTES`` and the explicit readline loop in
``DockerRunner.run`` that degrades past it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from coder_eval.isolation.docker_runner import STDOUT_LINE_LIMIT_BYTES, DockerRunner
from coder_eval.streaming.events import AgentStartEvent
from coder_eval.streaming.wire import serialize_event


_DEFAULT_ASYNCIO_LIMIT = 2**16  # asyncio.streams._DEFAULT_LIMIT (64 KiB)


async def _read_one_line(limit: int, payload: bytes) -> bytes:
    reader = asyncio.StreamReader(limit=limit)
    reader.feed_data(payload + b"\n")
    reader.feed_eof()
    return await reader.readline()


def test_default_limit_rejects_large_wire_line():
    """Demonstrates the regression: asyncio's 64 KiB default rejects a big line."""
    big = b"STREAM_EVENT:" + b"x" * _DEFAULT_ASYNCIO_LIMIT
    with pytest.raises(ValueError):
        asyncio.run(_read_one_line(_DEFAULT_ASYNCIO_LIMIT, big))


def test_runner_limit_reads_large_wire_line_as_one_line():
    """The runner's limit reads a realistic large tool-call event as a single line."""
    big = b"STREAM_EVENT:" + b"x" * (4 * 1024 * 1024)  # ~4 MiB single event
    line = asyncio.run(_read_one_line(STDOUT_LINE_LIMIT_BYTES, big))
    assert line == big + b"\n"


def test_runner_limit_is_generous():
    """A whole-file Write can be megabytes; the cap must leave real headroom."""
    assert STDOUT_LINE_LIMIT_BYTES >= 16 * 1024 * 1024


def test_reader_resyncs_after_overrun():
    """An over-limit line drops, then the reader resyncs at the next newline.

    This is the contract ``DockerRunner.run`` relies on when it catches
    ``ValueError`` and continues instead of letting the read loop die.
    """

    async def drive() -> bytes:
        reader = asyncio.StreamReader(limit=_DEFAULT_ASYNCIO_LIMIT)
        reader.feed_data(b"x" * (128 * 1024) + b"\n" + b"STREAM_EVENT:ok\n")
        reader.feed_eof()
        with pytest.raises(ValueError):
            await reader.readline()
        return await reader.readline()

    assert asyncio.run(drive()) == b"STREAM_EVENT:ok\n"


# ── Direct unit test for the extracted _stream_container_output seam ──
#
# The decomposition of DockerRunner.run lifts the stdout streaming loop into
# _stream_container_output(proc, log_fh) -> returncode. This pins its three-way
# line split + over-limit resync + returncode threading directly, without a real
# container (fake proc.stdout.readline() + fake log_fh).

_RAISE = object()  # sentinel: scripted readline raises ValueError (over-limit line)


class _FakeStdout:
    """Scripted ``proc.stdout``: each readline pops the next item — bytes are
    returned, the ``_RAISE`` sentinel raises ValueError (an over-limit line), and an
    empty list yields ``b""`` (EOF)."""

    def __init__(self, items: list) -> None:
        self._items = list(items)

    async def readline(self) -> bytes:
        if not self._items:
            return b""
        item = self._items.pop(0)
        if item is _RAISE:
            raise ValueError("line exceeded the stream limit")
        return item


class _FakeProc:
    def __init__(self, stdout: _FakeStdout, returncode: int) -> None:
        self.stdout = stdout
        self._returncode = returncode

    async def wait(self) -> int:
        return self._returncode


class _RecordingCallback:
    def __init__(self) -> None:
        self.events: list = []

    def on_event(self, event) -> None:
        self.events.append(event)


class _FakeLogFh:
    def __init__(self) -> None:
        self.written: list[str] = []

    def write(self, s: str) -> None:
        self.written.append(s)

    def flush(self) -> None:
        pass


async def test_stream_container_output_splits_lines_and_threads_returncode():
    """The streaming helper: emits wire events, logs plain lines, swallows an
    over-limit line (one dropped, streaming continues), and returns proc.wait()."""
    wire_line = serialize_event(AgentStartEvent(task_id="stream-test")).encode("utf-8") + b"\n"
    stdout = _FakeStdout(
        [
            _RAISE,  # over-limit line: dropped, streaming continues
            b"plain container log line\n",  # no prefix -> docker.log
            wire_line,  # wire prefix + parses -> StreamCallback
            b"",  # EOF
        ]
    )
    proc = _FakeProc(stdout, returncode=0)

    rt = MagicMock()
    rt.task.task_id = "stream-test"
    runner = DockerRunner(rt, stream_callback=_RecordingCallback())
    log_fh = _FakeLogFh()

    returncode = await runner._stream_container_output(proc, log_fh)

    # (a) wire-prefixed line emitted as an event via the stream callback
    assert len(runner.stream_callback.events) == 1
    assert isinstance(runner.stream_callback.events[0], AgentStartEvent)
    # (b) plain line written to the log (wire line NOT echoed to the log)
    assert log_fh.written == ["plain container log line\n"]
    # (c) the over-limit ValueError was swallowed — both later lines still processed
    # (d) returncode threaded from proc.wait()
    assert returncode == 0
