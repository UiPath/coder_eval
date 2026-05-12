"""Termination predicate for simulated dialogs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from coder_eval.models import SimulationConfig


class DialogStopReason(StrEnum):
    """Why a simulated dialog ended."""

    CRITERIA_PASSED = "criteria_passed"
    STOP_TOKEN = "stop_token"
    MAX_TURNS = "max_turns"
    BUDGET = "budget"
    ERROR = "error"
    RUN_LIMIT_EXCEEDED = "run_limit_exceeded"


@dataclass(frozen=True)
class StopDecision:
    """Outcome of a termination check.

    ``stop=False`` means "keep dialoguing"; ``reason`` is meaningful only when
    ``stop=True``.
    """

    stop: bool
    reason: DialogStopReason | None = None


def evaluate_stop(
    *,
    config: SimulationConfig,
    turns_completed: int,
    total_tokens_used: int,
    criteria_all_passed: bool,
) -> StopDecision:
    """Decide whether the current dialog should terminate.

    Precedence (first matching wins):
      1. criteria passed AND ``stop_on_criteria_pass`` — CRITERIA_PASSED
      2. ``turns_completed >= max_turns`` — MAX_TURNS
      3. ``max_total_tokens`` exceeded — BUDGET
      4. otherwise — keep going

    Stop-token detection is NOT handled here: the orchestrator checks
    ``SimulatorResult.stop_requested`` directly on the fresh simulator output
    each turn, which is strictly stronger than re-scanning the previous turn's
    message. Keeping stop-token logic in one place avoids a redundant branch.

    Args:
        config: Simulation configuration.
        turns_completed: Number of user↔agent exchanges completed so far.
            A turn is considered "completed" once the agent has replied to a
            user prompt.
        total_tokens_used: Running total of input+output tokens consumed by
            both the simulator and the coding agent across the dialog.
        criteria_all_passed: Whether all task success criteria currently
            evaluate to a passing score (meaningful only when the caller
            checks criteria at this point — callers using
            ``check_criteria='end_of_dialog'`` must pass False).
    """
    if config.stop_on_criteria_pass and criteria_all_passed:
        return StopDecision(stop=True, reason=DialogStopReason.CRITERIA_PASSED)

    if turns_completed >= config.max_turns:
        return StopDecision(stop=True, reason=DialogStopReason.MAX_TURNS)

    if config.max_total_tokens is not None and total_tokens_used >= config.max_total_tokens:
        return StopDecision(stop=True, reason=DialogStopReason.BUDGET)

    return StopDecision(stop=False)


def strip_stop_token(message: str, stop_token: str) -> str:
    """Remove the stop token from a simulator message, preserving surrounding text.

    The stop token is a terminator, not part of the user's utterance — the
    agent should never see it. Returns the message with the token (and any
    adjacent whitespace that would be left stranded) removed.
    """
    if stop_token not in message:
        return message
    cleaned = message.replace(stop_token, "")
    return cleaned.strip()
