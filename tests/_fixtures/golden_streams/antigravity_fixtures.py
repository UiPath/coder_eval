"""Antigravity golden-master scenarios: recorded Step streams + a runner.

The agent drains ``conversation.receive_steps()``, so a scenario is an ordered
list of ``Step``-shaped ``SimpleNamespace`` objects (or a list of batches, one
per successive drain). No SDK is needed: ``test_antigravity_agent``'s own
docstring notes the module does not require ``google-antigravity``, and the
driver here is the same ``SimpleNamespace`` fake.

The ``_step`` / ``_usage`` / ``_tc`` / ``_FakeConversation`` / ``_agent_with_steps``
helpers live HERE and are imported back into ``test_antigravity_agent`` — one
definition, two consumers.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from coder_eval.agents import antigravity_agent
from coder_eval.agents.antigravity_agent import AntigravityAgent
from coder_eval.models import parse_agent_config


def _usage(prompt: int, cached: int, candidates: int, thoughts: int) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_token_count=prompt,
        cached_content_token_count=cached,
        candidates_token_count=candidates,
        thoughts_token_count=thoughts,
        total_token_count=prompt + candidates + thoughts,
    )


def _tc(name: str, tid: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(name=name, id=tid, args=args)


def _step(
    stype,
    status,
    *,
    source="MODEL",
    target="TARGET_USER",
    tool_calls=None,
    content="",
    content_delta="",
    thinking="",
    thinking_delta="",
    usage=None,
    complete=None,
    error="",
    step_index=0,
    trajectory_id="",
):
    # Plain strings stand in for the SDK's str-enums (_enum_value passes them through).
    return SimpleNamespace(
        type=stype,
        status=status,
        source=source,
        target=target,
        tool_calls=tool_calls or [],
        content=content,
        content_delta=content_delta,
        thinking=thinking,
        thinking_delta=thinking_delta,
        usage_metadata=usage,
        is_complete_response=complete,
        error=error,
        step_index=step_index,
        trajectory_id=trajectory_id,
    )


class _FakeConversation:
    """Scriptable fake SDK conversation.

    ``steps`` is either a flat list (one batch, yielded on the first
    ``receive_steps()`` call) or a list of batches (one per successive
    ``receive_steps()`` call — the shape a poll loop drains repeatedly). Once
    the authored batches are exhausted, further calls yield an EMPTY batch —
    this mirrors the real SDK's local connection, which drains a queue and
    returns immediately with nothing once idle; it never replays already-
    yielded steps. A test standing in for a background job that never
    resolves should author one batch that opens the orphan and let
    exhaustion naturally fall through to empty polls, not repeat itself.
    """

    def __init__(self, steps):
        self._batches = list(steps) if steps and isinstance(steps[0], list) else [steps]
        self._batch_index = 0
        self.last_response = ""
        self.receive_steps_call_count = 0
        self.cancel_call_count = 0

    async def send(self, prompt, **kwargs):
        return None

    async def receive_steps(self):
        self.receive_steps_call_count += 1
        batch = self._batches[self._batch_index] if self._batch_index < len(self._batches) else []
        self._batch_index += 1
        for s in batch:
            yield s

    async def cancel(self):
        self.cancel_call_count += 1


def _agent_with_steps(steps):
    agent = AntigravityAgent(parse_agent_config(type="antigravity", model="gemini-3.5-flash"))
    agent.working_directory = pathlib.Path("/tmp")
    agent._sdk_agent = SimpleNamespace(conversation=_FakeConversation(steps), is_started=True)
    return agent


async def _no_sleep(_seconds: float) -> None:
    """Stand-in for asyncio.sleep in the poll loop — no real wait."""
    return None


@dataclass
class AntigravityScenario:
    """One recorded Step stream.

    No ``expects`` knob: every scenario here replays cleanly. The crash and
    timeout paths are covered by ``test_antigravity_agent``'s own tests, which
    need to assert on the exception rather than on a snapshot. Add the branch
    back with the first scenario that needs it, not before.
    """

    name: str
    steps: list[Any]


async def run_antigravity_scenario(scenario: AntigravityScenario, working_dir: str) -> dict[str, Any]:
    """Replay one scenario and return the resulting record as a plain dump."""
    agent = _agent_with_steps(scenario.steps)
    agent.working_directory = pathlib.Path(working_dir)
    # Neutralize the orphan poll loop's real 5s sleeps. `d_orphaned_tool`
    # leaves a tool ACTIVE on purpose, which is exactly the path that waits —
    # up to 120 cycles, i.e. ten minutes of wall clock in a unit test. The
    # loop's LOGIC is what the scenario records; the waiting is not.
    with patch.object(antigravity_agent.asyncio, "sleep", _no_sleep):
        record = await agent.communicate("do it")
    return record.model_dump(mode="json")


def _build_catalogue() -> list[AntigravityScenario]:
    """One scenario per behaviour that has actually bitten."""
    scenarios: list[AntigravityScenario] = []

    # (a) a single text response with usage -> one assistant message.
    scenarios.append(
        AntigravityScenario(
            name="a_single_text_turn",
            steps=[
                _step(
                    "TEXT_RESPONSE",
                    "DONE",
                    content="All done.",
                    content_delta="All done.",
                    complete=True,
                    usage=_usage(100, 0, 20, 0),
                ),
            ],
        )
    )

    # (b) a tool call that resolves -> command telemetry with both bounds.
    scenarios.append(
        AntigravityScenario(
            name="b_tool_call_resolved",
            steps=[
                _step(
                    "TOOL_CALL",
                    "ACTIVE",
                    target="TARGET_ENVIRONMENT",
                    tool_calls=[_tc("run_command", "t1", {"command_line": "echo hi"})],
                ),
                _step(
                    "TOOL_CALL",
                    "DONE",
                    target="TARGET_ENVIRONMENT",
                    tool_calls=[
                        _tc("run_command", "t1", {"command_line": "echo hi", "exit_code": 0, "combined_output": "hi"})
                    ],
                ),
                _step(
                    "TEXT_RESPONSE",
                    "DONE",
                    content="done",
                    content_delta="done",
                    complete=True,
                    usage=_usage(120, 0, 15, 0),
                ),
            ],
        )
    )

    # (c) thinking and a tool inside ONE generation — the interleaved shape the
    # generation-window subtraction exists for.
    scenarios.append(
        AntigravityScenario(
            name="c_thinking_and_tool_same_generation",
            steps=[
                _step("THINKING", "DONE", thinking="planning"),
                _step(
                    "TOOL_CALL",
                    "ACTIVE",
                    target="TARGET_ENVIRONMENT",
                    tool_calls=[_tc("view_file", "t1", {"file_path": "a.py"})],
                ),
                _step(
                    "TOOL_CALL",
                    "DONE",
                    target="TARGET_ENVIRONMENT",
                    tool_calls=[_tc("view_file", "t1", {"file_path": "a.py", "output": "x = 1"})],
                ),
                _step(
                    "TEXT_RESPONSE",
                    "DONE",
                    content="read it",
                    content_delta="read it",
                    complete=True,
                    usage=_usage(200, 0, 40, 10),
                ),
            ],
        )
    )

    # (d) a tool left ACTIVE -> force-closed unresolved, honestly untimed.
    scenarios.append(
        AntigravityScenario(
            name="d_orphaned_tool",
            steps=[
                _step(
                    "TOOL_CALL",
                    "ACTIVE",
                    target="TARGET_ENVIRONMENT",
                    tool_calls=[_tc("run_command", "bg1", {"command_line": "sleep 999"})],
                ),
                _step(
                    "TEXT_RESPONSE",
                    "DONE",
                    content="backgrounded",
                    content_delta="backgrounded",
                    complete=True,
                    usage=_usage(90, 0, 10, 0),
                ),
            ],
        )
    )

    # (e) three generations -> three chained windows.
    scenarios.append(
        AntigravityScenario(
            name="e_multi_generation",
            steps=[
                _step("THINKING", "DONE", thinking="first", usage=_usage(100, 0, 10, 5)),
                _step("THINKING", "DONE", thinking="second", usage=_usage(110, 0, 12, 6)),
                _step(
                    "TEXT_RESPONSE",
                    "DONE",
                    content="third",
                    content_delta="third",
                    complete=True,
                    usage=_usage(120, 0, 14, 0),
                ),
            ],
        )
    )

    return scenarios


ANTIGRAVITY_SCENARIOS: list[AntigravityScenario] = _build_catalogue()
