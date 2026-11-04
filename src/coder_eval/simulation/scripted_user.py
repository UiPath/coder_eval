"""Scripted user for dialogs whose turns are authored, not simulated.

Stands in for ``UserSimulator`` in the dialog loop with the same
``start`` / ``next_user_message`` / ``stop`` / ``model`` surface, and makes no
model call, so a scripted dialog needs no ``claude`` CLI, no credentials and
costs the user side nothing.
"""

from __future__ import annotations

from collections.abc import Sequence

from coder_eval.simulation.user_simulator import SimulatorResult


class ScriptedUser:
    """Replays ``SimulationConfig.turns`` in order, byte for byte.

    The next turn is the one after the exchanges already completed, so the dialog
    loop's own history is the only cursor. Asking past the last turn raises
    ``IndexError``; the loop stops first, on ``DialogStopReason.SCRIPT_EXHAUSTED``.
    """

    model: str | None = None

    def __init__(self, turns: Sequence[str]) -> None:
        self._turns = tuple(turns)

    async def start(self) -> None:
        """Nothing to start: there is no simulator agent."""

    async def stop(self) -> None:
        """Nothing to stop: there is no simulator agent."""

    async def next_user_message(self, dialog_pairs: list[tuple[str, str]]) -> SimulatorResult:
        """The scripted turn that follows ``dialog_pairs`` (one pair per completed exchange)."""
        text = self._turns[len(dialog_pairs)]
        return SimulatorResult(text=text, raw_text=text, stop_requested=False, input_tokens=None, output_tokens=None)
