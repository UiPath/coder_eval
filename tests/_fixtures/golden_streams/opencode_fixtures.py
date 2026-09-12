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
from datetime import datetime
from typing import Any
from unittest.mock import patch

from coder_eval.agents.opencode_agent import OpenCodeAgent
from coder_eval.errors import AgentCrashError
from coder_eval.models import OpenCodeAgentConfig


SESSION = "ses_test123"


# Base epoch milliseconds for the recorded stream. A BASE, not a wall-clock
# claim: `_rebase_lines` shifts the whole timeline onto the replay's own clock
# before the scenario runs, so these stamps and the agent's own `datetime.now()`
# event stamps are commensurable. Left absolute they sit a month away from the
# replay, which puts the recorded tool interval outside every measured window.
_T0_MS = 1_786_663_016_802

# How far after the replay's start the rebased timeline begins — small, but
# non-zero so the first window opens after the AgentStartEvent.
_REPLAY_LEAD_MS = 2


def _evt(event_type: str, part: dict[str, Any]) -> str:
    """One CLI event line: payload under ``part``, sessionID on the envelope."""
    return json.dumps(
        {"type": event_type, "timestamp": _T0_MS, "sessionID": SESSION, "part": {"sessionID": SESSION, **part}}
    )


def _rebase_lines(lines: list[str]) -> list[str]:
    """Shift every recorded stamp from ``_T0_MS`` onto the replay's own clock.

    Keeps every DERIVED duration exact (a 17 ms tool stays 17 ms) and fixes
    only the era, so the head and tail the collector records against the
    agent's `datetime.now()` stamps are meaningful rather than a month wide.
    """
    offset = int(datetime.now().timestamp() * 1000) - _T0_MS + _REPLAY_LEAD_MS

    def shift(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: (v + offset if k in _STAMP_KEYS and isinstance(v, int) else shift(v)) for k, v in node.items()}
        if isinstance(node, list):
            return [shift(v) for v in node]
        return node

    return [json.dumps(shift(json.loads(line))) for line in lines]


# Millisecond-epoch keys anywhere in an event payload: the envelope's own
# stamp, and a tool's `state.time` bounds.
_STAMP_KEYS = frozenset({"timestamp", "start", "end"})


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

    ``expects`` names the exception a scenario is supposed to raise, and the
    runner then snapshots ``pending_turn`` instead of the returned record —
    the same knob ``ClaudeScenario`` carries, for the same reason: the partial
    a crash preserves is a real capture path, and one nobody was comparing
    against a snapshot on this harness.
    """

    name: str
    lines: list[str]
    expects: type[BaseException] | None = None


async def run_opencode_scenario(scenario: OpenCodeScenario, working_dir: str) -> dict[str, Any]:
    """Replay one scenario and return the resulting record as a plain dump."""
    import pytest

    proc = _FakeProcess(_rebase_lines(scenario.lines))

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
        if scenario.expects is not None:
            with pytest.raises(scenario.expects):
                await agent.communicate("do it")
            record = agent.pending_turn
            assert record is not None, f"{scenario.name}: pending_turn was not set on the failure path"
        else:
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

    # (c) two generations with a tool resolving between them. The TILING case:
    # the second window opens at the first `step_finish`, not at its own
    # `step_start`, so the wall clock between the two steps — the model time
    # that produced the second one — lands inside a window rather than in no
    # bucket at all. That is the defect this harness shipped with, and it had
    # a unit test but no golden.
    scenarios.append(
        OpenCodeScenario(
            name="c_multi_step_tiling",
            lines=[
                _evt("step_start", {"id": "prt_1", "messageID": "msg_1"}),
                _evt(
                    "tool_use",
                    {
                        "id": "prt_2",
                        "messageID": "msg_1",
                        "tool": "bash",
                        "callID": "call_1",
                        "state": {
                            "status": "completed",
                            "input": {"command": "ls"},
                            "output": "main.py",
                            "time": {"start": _T0_MS, "end": _T0_MS + 5},
                        },
                    },
                ),
                _evt(
                    "step_finish",
                    {"id": "prt_3", "messageID": "msg_1", "reason": "tool-calls", "tokens": _tokens(100, 20)},
                ),
                _evt("step_start", {"id": "prt_4", "messageID": "msg_2"}),
                _evt("text", {"id": "prt_5", "messageID": "msg_2", "text": "Listed it."}),
                _evt(
                    "step_finish",
                    {"id": "prt_6", "messageID": "msg_2", "reason": "stop", "tokens": _tokens(50, 30)},
                ),
            ],
        )
    )

    # (d) a tool the CLI opens and never resolves — force-closed as `unresolved`
    # by the orphan sweep at finalization. It carries NO `state.time`, which is
    # the honest shape for a call that never returned: with no
    # `execution_started_at` there is no `duration_ms` and no span.
    #
    # READ THE SNAPSHOT: the sweep still stamps `execution_completed_at`, which
    # it does on every close path, so the record holds an end with no
    # beginning. Compare `pi_d_orphaned_tool`, where the start IS stamped and a
    # manufactured duration follows from it.
    scenarios.append(
        OpenCodeScenario(
            name="d_orphaned_tool",
            lines=[
                _evt("step_start", {"id": "prt_1", "messageID": "msg_1"}),
                _evt(
                    "tool_use",
                    {
                        "id": "prt_2",
                        "messageID": "msg_1",
                        "tool": "bash",
                        "callID": "call_1",
                        "state": {"status": "pending", "input": {"command": "sleep 600"}},
                    },
                ),
                _evt("text", {"id": "prt_3", "messageID": "msg_1", "text": "Waiting."}),
                _evt(
                    "step_finish",
                    {"id": "prt_4", "messageID": "msg_1", "reason": "stop", "tokens": _tokens(100, 20)},
                ),
            ],
        )
    )

    # (e) the CLI's own structured error AFTER a complete generation. `_settle_turn`
    # crashes on it, and the partial `pending_turn` must still carry that
    # generation and its head/tail — a crash does not un-measure what was
    # measured before it.
    scenarios.append(
        OpenCodeScenario(
            name="e_error_after_generation",
            lines=[
                _evt("step_start", {"id": "prt_1", "messageID": "msg_1"}),
                _evt("text", {"id": "prt_2", "messageID": "msg_1", "text": "Starting."}),
                _evt(
                    "step_finish",
                    {"id": "prt_3", "messageID": "msg_1", "reason": "stop", "tokens": _tokens(100, 20)},
                ),
                json.dumps(
                    {
                        "type": "error",
                        "sessionID": SESSION,
                        "error": {"name": "ProviderAuthError", "data": {"message": "401 from the provider"}},
                    }
                ),
            ],
            expects=AgentCrashError,
        )
    )

    return scenarios


OPENCODE_SCENARIOS: list[OpenCodeScenario] = _build_catalogue()
