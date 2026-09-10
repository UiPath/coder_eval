"""OpenCode golden-master scenarios: recorded CLI event lines + a runner.

The agent shells out to ``opencode run --format json`` and reduces its
newline-delimited JSON, so a scenario is an ordered list of event LINES and the
driver is a fake process that replays them. No Python package to guard on:
``pyproject.toml`` declares ``opencode = []``.

The event helpers, the ``HAPPY_STREAM`` sample and the ``_FakeProcess`` /
``_RunningProcess`` fakes live HERE and are imported back into
``test_opencode_agent`` — one definition, two consumers. The ``patch_exec``
pytest FIXTURE stays in that module (it needs ``monkeypatch``); the runner
below does the same patching with a plain context manager.

The lines mirror events CAPTURED FROM A LIVE run — the CLI's own compact
vocabulary (``step_start`` / ``step_finish`` / ``text`` / ``tool_use``, payload
under ``part``). Do NOT "correct" them toward the ``session.next.*`` names in
the server's OpenAPI schema: those describe ``opencode serve``'s SSE surface,
and an earlier version of this harness parsed them and silently captured zero
telemetry on a real run.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

from coder_eval.agents.opencode_agent import OpenCodeAgent
from coder_eval.models import OpenCodeAgentConfig


SESSION = "ses_test123"


def _evt(event_type: str, part: dict[str, Any]) -> str:
    """One CLI event line: payload under ``part``, sessionID on the envelope."""
    return json.dumps(
        {"type": event_type, "timestamp": 1786663016802, "sessionID": SESSION, "part": {"sessionID": SESSION, **part}}
    )


def _tokens(inp: int, out: int, *, write: int = 0, read: int = 0, reasoning: int = 0) -> dict[str, Any]:
    """Token payload in the NESTED convention (total = input+output+reasoning, cache
    counted inside `input`); see TestTokenShapeIsObservable for the flat one."""
    return {
        "total": inp + out + reasoning,
        "input": inp,
        "output": out,
        "reasoning": reasoning,
        "cache": {"write": write, "read": read},
    }


HAPPY_STREAM = [
    _evt("step_start", {"id": "prt_1", "messageID": "msg_1", "type": "step-start"}),
    _evt(
        "tool_use",
        {
            "id": "prt_2",
            "messageID": "msg_1",
            "type": "tool",
            "tool": "read",
            "callID": "call_1",
            "state": {
                "status": "completed",
                "input": {"filePath": "main.py"},
                "output": "print('hi')",
                "time": {"start": 1786663018214, "end": 1786663018231},
            },
        },
    ),
    _evt(
        "step_finish",
        {
            "id": "prt_3",
            "messageID": "msg_1",
            "reason": "tool-calls",
            "cost": 0.001,
            "tokens": _tokens(100, 20, write=5, read=10),
        },
    ),
    _evt("step_start", {"id": "prt_4", "messageID": "msg_2", "type": "step-start"}),
    _evt("text", {"id": "prt_5", "messageID": "msg_2", "type": "text", "text": "Created the file."}),
    _evt(
        "step_finish",
        {
            "id": "prt_6",
            "messageID": "msg_2",
            "reason": "stop",
            "cost": 0.002,
            "tokens": _tokens(50, 30, read=40, reasoning=7),
        },
    ),
]


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
    """A process that stays alive until it is explicitly terminated or killed.

    Needed for teardown assertions: the plain fake reports an exit code as soon
    as ``wait()`` is awaited, so ``kill()`` would (correctly) skip ``terminate()``
    on an already-dead process and the test would prove nothing.
    """

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


def _agent() -> OpenCodeAgent:
    return OpenCodeAgent(OpenCodeAgentConfig(type="opencode", model="deepseek/deepseek-v4-pro"), task_id="t1")


@dataclass
class OpenCodeScenario:
    """One recorded CLI event stream.

    No ``expects`` knob: every scenario here replays cleanly. The crash and
    timeout paths live in the agent's own test module, which asserts on the
    exception rather than on a snapshot.
    """

    name: str
    lines: list[str]


async def run_opencode_scenario(scenario: OpenCodeScenario, working_dir: str) -> dict[str, Any]:
    """Replay one scenario and return the resulting record as a plain dump."""
    proc = _FakeProcess(scenario.lines)

    async def fake_exec(*_argv: str, **_kwargs: Any) -> _FakeProcess:
        proc.stderr = proc  # type: ignore[assignment]
        return proc

    agent = _agent()
    with (
        patch.object(asyncio, "create_subprocess_exec", fake_exec),
        patch("shutil.which", lambda _name: "/usr/local/bin/opencode"),
        patch.object(os, "killpg", lambda _pgid, _sig: None, create=True),
    ):
        await agent.start(working_dir)
        record = await agent.communicate("do it")
    return record.model_dump(mode="json")


def _build_catalogue() -> list[OpenCodeScenario]:
    scenarios: list[OpenCodeScenario] = []

    # (a) one step producing text, then a step_finish carrying the usage.
    scenarios.append(
        OpenCodeScenario(
            name="a_single_text_turn",
            lines=[
                _evt("step_start", {"id": "prt_1", "messageID": "msg_1"}),
                _evt("text", {"id": "prt_2", "messageID": "msg_1", "text": "All done."}),
                _evt(
                    "step_finish",
                    {
                        "id": "prt_3",
                        "messageID": "msg_1",
                        "reason": "stop",
                        "cost": 0.001,
                        "tokens": _tokens(100, 20),
                    },
                ),
            ],
        )
    )

    # (b) a resolved tool call inside a step.
    scenarios.append(
        OpenCodeScenario(name="b_tool_call_resolved", lines=list(HAPPY_STREAM)),
    )

    return scenarios


OPENCODE_SCENARIOS: list[OpenCodeScenario] = _build_catalogue()
