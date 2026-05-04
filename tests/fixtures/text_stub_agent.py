"""Lightweight ``Agent`` stub that returns canned text responses.

Used by simulator tests to bypass the real Claude Code SDK: pass an instance
to ``UserSimulator(agent_override=...)`` and each ``next_user_message`` call
drains one response off the queue.
"""

from __future__ import annotations

from pathlib import Path

from coder_eval.agent import Agent, AgentState
from coder_eval.models import TurnRecord


class TextStubAgent(Agent):
    """Canned-response Agent fake. Records every ``communicate`` prompt."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []
        self._iteration = 0
        self._state = AgentState.WORKING
        self.working_directory: Path | None = None
        self.started = False
        self.stopped = False

    async def start(self, working_directory: str, *, env_path_prepend: list[str] | None = None) -> None:
        self.working_directory = Path(working_directory)
        self._state = AgentState.WORKING
        self.started = True

    async def stop(self) -> None:
        self._state = AgentState.FINISHED
        self.stopped = True

    def get_state(self) -> AgentState:
        return self._state

    async def communicate(self, user_input: str, **kwargs: object) -> TurnRecord:
        self._iteration += 1
        self.calls.append(user_input)
        text = self._responses.pop(0) if self._responses else ""
        return TurnRecord(
            iteration=self._iteration,
            user_input=user_input,
            agent_output=text,
            files_changed=[],
        )
