"""Pi golden-master scenarios: recorded CLI event lines + a runner.

The agent shells out to ``pi -p --mode json`` and reduces its newline-delimited
JSON, so a scenario is an ordered list of event LINES and the driver is a fake
process that replays them. No Python package to guard on: ``pyproject.toml``
declares ``pi = []``.

The event helpers, the ``HAPPY_STREAM`` sample (read from
``tests/fixtures/pi_happy_stream.jsonl``) and the ``_FakeProcess`` /
``_RunningProcess`` fakes live HERE and are imported back into
``test_pi_agent`` — one definition, two consumers. The ``patch_exec`` pytest
FIXTURE stays in that module (it needs ``monkeypatch``); the runner below does
the same patching with a plain context manager.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

from coder_eval.agents.pi_agent import PiAgent
from coder_eval.errors import AgentCrashError
from coder_eval.models import PiAgentConfig


# tests/_fixtures/golden_streams/ -> tests/fixtures/ (this module moved two
# directories deeper than the test file the path was written for).
_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "pi_happy_stream.jsonl"
HAPPY_STREAM = _FIXTURE.read_text(encoding="utf-8").splitlines()

# Derived from the fixture's three `turn_end` usages (per-generation, summed):
#   input   406 + 512 + 79   = 997
#   output  (69+8)+(49+3)+(28+3) = 160   (reasoning folds into the output rate)
#   cacheRead 1024 + 1024 + 1536 = 3584
#   cost.total 0.002177194 + 0.002192494 + 0.000951666 = 0.005321354
EXPECTED_INPUT = 997
EXPECTED_OUTPUT = 160
EXPECTED_CACHE_READ = 3584
EXPECTED_COST = 0.005321354


def _turn_start() -> str:
    return json.dumps({"type": "turn_start"})


def _turn_end(*, inp: int, out: int, cache_read: int = 0, cache_write: int = 0, reasoning: int = 0, cost: float = 0.0):
    """A `turn_end` event carrying that step's own (per-generation) usage."""
    usage: dict[str, Any] = {
        "input": inp,
        "output": out,
        "cacheRead": cache_read,
        "cacheWrite": cache_write,
        "reasoning": reasoning,
        "totalTokens": inp + out + cache_read + cache_write,
        "cost": {"total": cost},
    }
    return json.dumps(
        {"type": "turn_end", "message": {"role": "assistant", "usage": usage, "stopReason": "stop"}, "toolResults": []}
    )


def _text(delta: str) -> str:
    """One streamed assistant text delta.

    The CLI's real shape, which is `message_update` carrying an
    `assistantMessageEvent` of type `text_delta` — NOT a bare `{"type": "text"}`
    line. `_handle_line` dispatches on the outer `type`, so a bare `text` line
    is unrecognized vocabulary: it reaches no handler, appends no text, and
    leaves `agent_output` empty while the scenario still passes.
    `a_single_text_turn` was written that way and asserted nothing about the
    text capture its own name claims.
    """
    return json.dumps({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": delta}})


def _tool_start(call_id: str, name: str, args: dict[str, Any]) -> str:
    return json.dumps({"type": "tool_execution_start", "toolCallId": call_id, "toolName": name, "args": args})


def _turn_end_error(message: str) -> str:
    """A `turn_end` whose `stopReason` is the provider error pi could not retry away.

    `pi -p` exits 0 after exhausting its internal retries, so this is the only
    signal that the turn died — `_settle_turn` crashes on it precisely so the
    row does not book as a clean failure and silently depress the pass rate.
    """
    return json.dumps(
        {
            "type": "turn_end",
            "message": {
                "role": "assistant",
                "usage": {"input": 0, "output": 0, "cost": {"total": 0.0}},
                "stopReason": "error",
                "errorMessage": message,
            },
            "toolResults": [],
        }
    )


def _tool_end(call_id: str, name: str, text: str, *, is_error: bool = False) -> str:
    return json.dumps(
        {
            "type": "tool_execution_end",
            "toolCallId": call_id,
            "toolName": name,
            "result": {"content": [{"type": "text", "text": text}]},
            "isError": is_error,
        }
    )


class _FakeProcess:
    def __init__(self, lines: list[str], returncode: int = 0, stderr: bytes = b"") -> None:
        self._lines = [f"{line}\n".encode() for line in lines]
        self.returncode: int | None = None
        self._final_returncode = returncode
        self._stderr = stderr
        self.pid = 4242
        self.terminated = False
        self.killed = False
        self.stdout = self

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        self.returncode = self._final_returncode
        return b""

    async def read(self) -> bytes:
        return self._stderr

    async def wait(self) -> int:
        self.returncode = self._final_returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = self._final_returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = self._final_returncode


class _RunningProcess(_FakeProcess):
    """A process that stays alive until it is explicitly terminated or killed."""

    def __init__(self, lines: list[str], **kwargs: Any) -> None:
        super().__init__(lines, **kwargs)
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        await self._exited.wait()
        self.returncode = self._final_returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self._exited.set()

    def kill(self) -> None:
        self.killed = True
        self._exited.set()


class _ExplodingRunningProcess(_RunningProcess):
    """Raises from ``readline`` mid-stream AND stays alive, like the real CLI.

    ``_ExplodingProcess`` inherits the plain fake's ``wait()``, which reports an
    exit code the instant it is awaited — so it can never model the case that
    matters for teardown: the read loop dying while the CLI is still streaming.
    """

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        raise ValueError("Separator is not found, and chunk exceed the limit")


def _agent() -> PiAgent:
    return PiAgent(PiAgentConfig(type="pi", model="openrouter/moonshotai/kimi-k3"), task_id="t1")


@dataclass
class PiScenario:
    """One recorded CLI event stream.

    ``expects`` names the exception a scenario is supposed to raise, and the
    runner then snapshots ``pending_turn`` instead of the returned record —
    the same knob ``ClaudeScenario`` carries, for the same reason: the partial
    a crash preserves is a real capture path, and one nobody was comparing
    against a snapshot on this harness.
    """

    name: str
    lines: list[str]
    expects: type[BaseException] | None = None


async def run_pi_scenario(scenario: PiScenario, working_dir: str) -> dict[str, Any]:
    """Replay one scenario and return the resulting record as a plain dump."""
    import pytest

    proc = _FakeProcess(scenario.lines)

    async def fake_exec(*_argv: str, **_kwargs: Any) -> _FakeProcess:
        proc.stderr = proc  # type: ignore[assignment]
        return proc

    agent = _agent()
    with (
        patch.object(asyncio, "create_subprocess_exec", fake_exec),
        patch("shutil.which", lambda _name: "/usr/local/bin/pi"),
        patch.object(os, "killpg", lambda _pgid, _sig: None, create=True),
    ):
        await agent.start(working_dir)
        if scenario.expects is not None:
            with pytest.raises(scenario.expects):
                await agent.communicate("do it")
            record = agent.pending_turn
            assert record is not None, f"{scenario.name}: pending_turn was not set on the failure path"
        else:
            record = await agent.communicate("do it")
    return record.model_dump(mode="json")


def _build_catalogue() -> list[PiScenario]:
    scenarios: list[PiScenario] = []

    # (a) one turn producing text, with its own per-generation usage.
    scenarios.append(
        PiScenario(
            name="a_single_text_turn",
            lines=[
                _turn_start(),
                _text("All done."),
                _turn_end(inp=100, out=20, cache_read=64, cost=0.001),
            ],
        )
    )

    # (b) the captured live stream: three turns with resolved tool calls.
    scenarios.append(PiScenario(name="b_tool_call_resolved", lines=list(HAPPY_STREAM)))

    # (c) two generations with a tool resolving between them. The TILING case:
    # the second window opens at the first `turn_end`, not at its own
    # `turn_start`, so the wall clock between the two turns — the model time
    # that produced the second one — lands inside a window rather than in no
    # bucket at all. Pi was the harness that shipped that defect, and it had a
    # unit test but no golden. Every stamp here comes from the reducer's own
    # TurnClock, so this scenario stays inside the identity check.
    scenarios.append(
        PiScenario(
            name="c_multi_turn_tiling",
            lines=[
                _turn_start(),
                _tool_start("call_1", "bash", {"command": "ls"}),
                _tool_end("call_1", "bash", "main.py"),
                _turn_end(inp=100, out=20, cost=0.001),
                _turn_start(),
                _text("Listed it."),
                _turn_end(inp=50, out=30, cost=0.002),
            ],
        )
    )

    # (d) a tool the CLI opens and never resolves — force-closed as `unresolved`
    # by the orphan sweep at finalization.
    #
    # READ THE SNAPSHOT: it carries a `duration_ms` and BOTH execution bounds,
    # and that span is subtracted from the generation window. Its
    # `execution_completed_at` is the instant the sweep ran, not a completion
    # anybody observed, so the duration is manufactured — and `_close_tool`'s
    # own comment ("Only a RESOLVED tool contributes: one force-closed without
    # a result was never timed") describes a guard it does not have: the test
    # is `execution_started_at is not None`, which an orphan passes.
    # claude-code's `_finalize_commands` deliberately leaves `duration_ms`
    # None in exactly this case, and says why. Captured rather than fixed:
    # this scenario is what makes it visible.
    scenarios.append(
        PiScenario(
            name="d_orphaned_tool",
            lines=[
                _turn_start(),
                _tool_start("call_1", "bash", {"command": "sleep 600"}),
                _text("Waiting."),
                _turn_end(inp=100, out=20, cost=0.001),
            ],
        )
    )

    # (e) the provider error pi's internal retries could not clear, AFTER a
    # complete generation. The CLI still exits 0, so `_settle_turn` crashes on
    # `stopReason=error` alone — and the partial `pending_turn` must still carry
    # that generation and its head/tail. A crash does not un-measure what was
    # measured before it.
    scenarios.append(
        PiScenario(
            name="e_error_after_generation",
            lines=[
                _turn_start(),
                _text("Starting."),
                _turn_end(inp=100, out=20, cost=0.001),
                _turn_start(),
                _turn_end_error("provider returned 529 after 5 retries"),
            ],
            expects=AgentCrashError,
        )
    )

    # (f) a duplicate `turn_end` with no `turn_start` between — a transport
    # hiccup this reducer explicitly promises to survive, since pi retries
    # internally. A spent `turn_started_at` left in place reopens the next
    # window at the PREVIOUS turn's start and republishes that whole span:
    # reproduced as 3000 ms of generation for a 2000 ms turn. It had a unit test
    # and no golden.
    #
    # READ THE SNAPSHOT: it records that the TIMING half of that reset is fixed
    # and the CONTENT half is not. `turn_text_parts` / `turn_tool_ids` are
    # cleared in `on_turn_start` only, so the second `turn_end` publishes the
    # first turn's text a second time, as its own assistant message. The
    # argument `on_turn_end`'s comment makes for moving `turn_started_at` out of
    # `on_turn_start` applies to those two lists unchanged. Captured here rather
    # than fixed: this scenario is what makes it visible at all.
    scenarios.append(
        PiScenario(
            name="f_duplicate_turn_end",
            lines=[
                _turn_start(),
                _text("First."),
                _turn_end(inp=100, out=20, cost=0.001),
                _turn_end(inp=10, out=5, cost=0.0001),
            ],
        )
    )

    return scenarios


PI_SCENARIOS: list[PiScenario] = _build_catalogue()
