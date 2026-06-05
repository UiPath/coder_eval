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

import pytest

from coder_eval.isolation.docker_runner import STDOUT_LINE_LIMIT_BYTES


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
