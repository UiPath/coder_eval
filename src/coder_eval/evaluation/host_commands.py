"""Shared execution of ``pre_run``/``post_run`` shell command lists.

The core loop is a free function :func:`run_command_list` so it can run both
in-process (via ``Orchestrator._run_command_list``, which delegates here) AND
host-side over a copied-out workspace (under ``--driver docker``, where the
graders/helper scripts live only on the host and post-run teardown must run
after the container exits). Keeping the loop in one place preserves the exact
semantics — ``PreRunCommand.fail_on_error`` abort, ``PostRunCommand``
informational/non-fatal, per-command timeout, output truncation, line-by-line
streaming to a logger, and a caller-supplied ``cwd`` — regardless of caller.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path

from ..models import PostRunCommand, PostRunResult, PreRunCommand


logger = logging.getLogger("coder_eval.orchestrator")

# Truncate captured stdout/stderr to 100KB per stream.
DEFAULT_MAX_OUTPUT = 100_000
# StreamReader per-line buffer (256KB).
DEFAULT_STREAM_LIMIT = 262_144


async def _pump_stream(
    stream: asyncio.StreamReader | None,
    log_fn: Callable[..., None],
    label: str,
    chunks: list[str],
) -> None:
    """Read ``stream`` line-by-line, log each non-empty line via ``log_fn``,
    and accumulate the raw text into ``chunks`` for later capture.

    Forwards subprocess output to the logger in real time while preserving it
    for ``PostRunResult``. If a single line exceeds the StreamReader buffer
    (rare — only for binary-ish or malformed output), it is drained as a chunk
    and logged as a partial.
    """
    if stream is None:
        return
    while True:
        try:
            raw = await stream.readline()
        except asyncio.LimitOverrunError as e:
            # Single line larger than the buffer; drain the buffered bytes so
            # readline() can make progress on the next iteration.
            raw = await stream.readexactly(e.consumed)
            text = raw.decode(errors="replace")
            chunks.append(text)
            log_fn("[%s] (partial line, %d bytes)", label, len(raw))
            continue
        if not raw:
            break
        text = raw.decode(errors="replace")
        chunks.append(text)
        line = text.rstrip()
        if line:
            log_fn("[%s] %s", label, line)


async def run_command_list(
    commands: list[PreRunCommand] | list[PostRunCommand],
    results: list[PostRunResult],
    label: str,
    *,
    cwd: Path | str,
    max_output: int = DEFAULT_MAX_OUTPUT,
    stream_limit: int = DEFAULT_STREAM_LIMIT,
) -> None:
    """Run a list of shell commands with ``cwd``, capturing output.

    stdout/stderr are streamed line-by-line to the orchestrator logger and
    accumulated into ``results`` for the report (truncated to ``max_output``
    per stream). ``label`` is used in stream/log labels (e.g. ``"pre_run"`` ->
    ``[pre_run stdout]``).

    For commands carrying ``fail_on_error=True`` (PreRunCommand only), a
    non-zero exit, timeout, or exception appends the failure result and then
    raises ``RuntimeError``, aborting the loop. PostRunCommand never has
    ``fail_on_error`` set, so failures are warning-logged and the loop
    continues — preserving existing post-run "informational only" semantics.
    """
    if not commands:
        return

    cwd_str = str(cwd)
    human = label.replace("_", "-").capitalize()  # "pre_run" -> "Pre-run"

    for cmd in commands:
        fail_on_error = isinstance(cmd, PreRunCommand) and cmd.fail_on_error
        start = time.time()
        logger.info("Running %s command: %s", human.lower(), cmd.command)

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd.command,
                cwd=cwd_str,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=stream_limit,
            )  # nosec B602,B604 - commands come from task YAML, not user input

            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []

            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        _pump_stream(proc.stdout, logger.info, f"{label} stdout", stdout_chunks),
                        _pump_stream(proc.stderr, logger.warning, f"{label} stderr", stderr_chunks),
                        proc.wait(),
                    ),
                    timeout=cmd.timeout,
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                results.append(
                    PostRunResult(
                        command=cmd.command,
                        stdout="".join(stdout_chunks)[:max_output],
                        stderr="".join(stderr_chunks)[:max_output],
                        error=f"Timed out after {cmd.timeout}s",
                        duration_seconds=time.time() - start,
                    )
                )
                if fail_on_error:
                    raise RuntimeError(f"{human} command timed out after {cmd.timeout}s: {cmd.command!r}") from None
                logger.warning("%s command '%s' timed out after %ds", human, cmd.command, cmd.timeout)
                continue

            stdout_text = "".join(stdout_chunks)[:max_output]
            stderr_text = "".join(stderr_chunks)[:max_output]
            results.append(
                PostRunResult(
                    command=cmd.command,
                    exit_code=proc.returncode,
                    stdout=stdout_text,
                    stderr=stderr_text,
                    duration_seconds=time.time() - start,
                )
            )
            if proc.returncode != 0:
                if fail_on_error:
                    raise RuntimeError(f"{human} command failed (exit {proc.returncode}): {cmd.command!r}")
                logger.warning(
                    "%s command '%s' exited with code %d: %s",
                    human,
                    cmd.command,
                    proc.returncode,
                    stderr_text[:200],
                )
        except RuntimeError:
            # Propagate abort signal from fail_on_error=True branches unchanged;
            # otherwise the catch-all below would re-wrap it as a new RuntimeError.
            raise
        except Exception as e:
            results.append(
                PostRunResult(
                    command=cmd.command,
                    error=str(e),
                    duration_seconds=time.time() - start,
                )
            )
            if fail_on_error:
                raise RuntimeError(f"{human} command failed: {cmd.command!r}") from e
            logger.warning("%s command '%s' failed: %s", human, cmd.command, e)
