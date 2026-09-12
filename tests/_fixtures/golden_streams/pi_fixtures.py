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


def _tool_start(call_id: str, name: str, args: dict[str, Any]) -> str:
    return json.dumps({"type": "tool_execution_start", "toolCallId": call_id, "toolName": name, "args": args})


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

    No ``expects`` knob: every scenario here replays cleanly. The crash and
    timeout paths live in the agent's own test module, which asserts on the
    exception rather than on a snapshot.
    """

    name: str
    lines: list[str]


async def run_pi_scenario(scenario: PiScenario, working_dir: str) -> dict[str, Any]:
    """Replay one scenario and return the resulting record as a plain dump."""
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
                json.dumps({"type": "text", "text": "All done."}),
                _turn_end(inp=100, out=20, cache_read=64, cost=0.001),
            ],
        )
    )

    # (b) the captured live stream: three turns with resolved tool calls.
    scenarios.append(PiScenario(name="b_tool_call_resolved", lines=list(HAPPY_STREAM)))

    return scenarios


PI_SCENARIOS: list[PiScenario] = _build_catalogue()
