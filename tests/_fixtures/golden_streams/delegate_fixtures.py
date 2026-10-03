"""Delegate golden-master scenarios: scripted ``delegate-stdio`` stdout frames + a runner.

The agent drives the ``@uipath/delegate-stdio`` host over newline-delimited JSON,
so a scenario is an ordered list of host stdout FRAMES and the driver is a fake
process that replays them after the ``init_ok`` ack.

The frame builders and the ``_FakeProcess`` fake live HERE and are imported back
into ``test_delegate_agent`` — one definition, two consumers. The ``patch_exec``
pytest FIXTURE stays in that module (it needs ``monkeypatch``); the runner below
does the same patching with a plain context manager.

Rationale: .claude/notes/agents.md § Delegate agent golden-master coverage
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

from coder_eval.agents import delegate_agent as agent_module
from coder_eval.agents.delegate_agent import DelegateAgent
from coder_eval.errors import AgentCrashError
from coder_eval.models import AgentKind, DelegateAgentConfig


def _line(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj) + "\n").encode("utf-8")


def _ev(**event: Any) -> bytes:
    """One ``event`` frame wrapping an SDK event, as ``delegate-stdio`` writes it."""
    return _line({"type": "event", "event": event})


def _result(**fields: Any) -> bytes:
    return _line({"type": "result", **fields})


def _totals(input_tokens: int, output_tokens: int, *, cache_read: int = 0, cache_write: int = 0) -> dict[str, int]:
    """The host's per-call usage object: all four buckets, ``input_tokens`` exclusive of cache."""
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": cache_read,
    }


def _usage(input_tokens: int, output_tokens: int, *, cache_read: int = 0, cache_write: int = 0) -> bytes:
    """One model call's ``usage`` frame, written before any tool result that call caused."""
    usage = _totals(input_tokens, output_tokens, cache_read=cache_read, cache_write=cache_write)
    return _line({"type": "usage", "usage": usage})


def _tool_call(tool_id: str, name: str = "shell", /, **args: Any) -> bytes:
    return _ev(type="tool_call", toolId=tool_id, toolName=name, toolArgs=args, toolStatus="pending")


def _tool_result(tool_id: str, content: str = "ok", *, status: str = "completed") -> bytes:
    return _ev(
        type="tool_result",
        toolId=tool_id,
        toolName="shell",
        toolResult={"responseType": "success", "content": content},
        toolStatus=status,
    )


class _FakeStreamReader:
    def __init__(self, lines: list[bytes], *, hang_after: bool = False) -> None:
        self._lines = list(lines)
        self._hang_after = hang_after

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        if self._hang_after:
            # Never resolves -- for exercising a timeout that elapses WHILE
            # blocked reading, not just the loop's own top-of-iteration
            # pre-check.
            await asyncio.Event().wait()
        return b""


class _FakeStdin:
    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(json.loads(data.decode("utf-8").strip()))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(
        self, stdout_lines: list[bytes], stderr_lines: list[bytes] | None = None, *, hang_after: bool = False
    ) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStreamReader(stdout_lines, hang_after=hang_after)
        self.stderr = _FakeStreamReader(stderr_lines or [])
        self.returncode: int | None = None
        self.pid = 4242
        self._killed = False
        self.spawn_args: tuple[Any, ...] = ()
        self.spawn_kwargs: dict[str, Any] = {}

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0.01)
        return self.returncode

    def kill(self) -> None:
        self._killed = True
        self.returncode = -9

    def terminate(self) -> None:
        self.kill()


def _host_result(response: str, calls: list[dict[str, int]], **fields: Any) -> bytes:
    """A ``result`` frame as the host writes it: ``usage`` is the sum of ``turnUsages``, one entry per call."""
    usage = {bucket: sum(call[bucket] for call in calls) for bucket in calls[0]}
    return _result(
        response=response,
        durationMs=1200,
        assistantStepCount=len(calls),
        maxStepsReached=False,
        usage=usage,
        turnUsages=calls,
        model="virtuoso-1-5",
        **fields,
    )


def _billed_round_trip(n: int) -> list[bytes]:
    """A round-trip as the host writes it: the call's usage frame precedes its tool call and result."""
    return [
        _ev(type="message", content="", isStepStart=True),
        _usage(10 * (n + 1), n + 1),
        _tool_call(f"t{n}", command=f"step {n}"),
        _tool_result(f"t{n}", f"out {n}"),
    ]


@dataclass
class DelegateScenario:
    """One scripted host stdout stream, replayed after the ``init_ok`` ack.

    ``expects`` names the exception a scenario is supposed to raise; the runner
    then snapshots ``pending_turn``, the crash partial, instead of the returned
    record. ``max_turns`` is passed to ``communicate()``.
    """

    name: str
    frames: list[bytes]
    expects: type[BaseException] | None = None
    max_turns: int | None = None


async def run_delegate_scenario(scenario: DelegateScenario, working_dir: str) -> dict[str, Any]:
    """Replay one scenario and return the resulting record as a plain dump."""
    import pytest

    proc = _FakeProcess([_line({"type": "init_ok"}), *scenario.frames])

    async def fake_exec(*_argv: str, **_kwargs: Any) -> _FakeProcess:
        return proc

    agent = DelegateAgent(DelegateAgentConfig(type=AgentKind.DELEGATE, model="virtuoso-1-5"), task_id="t1")
    with (
        patch.object(asyncio, "create_subprocess_exec", fake_exec),
        patch("shutil.which", lambda _name: "/usr/local/bin/node"),
        patch.object(agent_module, "_resolve_host_bundle", lambda: Path("/opt/delegate_stdio.mjs")),
        patch.object(os, "killpg", lambda _pgid, _sig: None, create=True),
    ):
        await agent.start(working_dir)
        if scenario.expects is not None:
            with pytest.raises(scenario.expects):
                await agent.communicate("do it", max_turns=scenario.max_turns)
            record = agent.pending_turn
            assert record is not None, f"{scenario.name}: pending_turn was not set on the failure path"
        else:
            record = await agent.communicate("do it", max_turns=scenario.max_turns)
    return record.model_dump(mode="json")


def _build_catalogue() -> list[DelegateScenario]:
    scenarios: list[DelegateScenario] = []

    # (a) two calls with a resolved tool between them. The result's usage is the turn
    # total and replaces the frame sum; its turnUsages length becomes num_turns.
    scenarios.append(
        DelegateScenario(
            name="a_tool_call",
            frames=[
                _ev(type="session_start", sessionId="sess-1"),
                _ev(type="thinking", content="list the files first"),
                _ev(type="message", content="", isStepStart=True),
                _usage(100, 20, cache_read=40),
                _tool_call("tool-1", "Bash", command="ls"),
                _tool_result("tool-1", "main.py"),
                _ev(type="message", content="Listed ", isStepStart=True),
                _ev(type="message", content="it.", isStepStart=False),
                _usage(50, 30, cache_write=10),
                _ev(type="done", sessionId="sess-1"),
                _host_result(
                    "Listed it.",
                    [_totals(100, 20, cache_read=40), _totals(50, 30, cache_write=10)],
                    sessionId="sess-1",
                ),
            ],
        )
    )

    # (b) a max_turns cut. The host gets no chance to send a result, so the record
    # keeps the usage frames of the calls under the cap and drops the call that crossed it.
    scenarios.append(
        DelegateScenario(
            name="b_max_turns_cut",
            frames=[
                *_billed_round_trip(0),
                *_billed_round_trip(1),
                *_billed_round_trip(2),
                _host_result("done", [_totals(10, 1), _totals(20, 2), _totals(30, 3)]),
            ],
            max_turns=2,
        )
    )

    # (c) a host `error` frame after a finished call. The partial `pending_turn` must
    # still carry that call's tool, usage and generation window.
    scenarios.append(
        DelegateScenario(
            name="c_error_frame_crash",
            frames=[
                *_billed_round_trip(0),
                _ev(type="message", content="Starting.", isStepStart=True),
                _line({"type": "error", "message": "Delegate backend error: HTTP 422"}),
            ],
            expects=AgentCrashError,
        )
    )

    # (d) a tool call the host opens and never resolves before its result. The orphan
    # sweep at finalization closes it with an `unknown` status: a start, but no end
    # and no duration.
    scenarios.append(
        DelegateScenario(
            name="d_orphaned_tool",
            frames=[
                _ev(type="message", content="", isStepStart=True),
                _usage(100, 20),
                _tool_call("tool-1", "Bash", command="sleep 600"),
                _ev(type="message", content="Waiting.", isStepStart=True),
                _host_result("Waiting.", [_totals(100, 20)]),
            ],
        )
    )

    return scenarios


DELEGATE_SCENARIOS: list[DelegateScenario] = _build_catalogue()
