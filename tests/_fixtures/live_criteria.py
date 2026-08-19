"""Shared builders for live-criterion trajectories (early-stop tests + CE036).

``tests/test_early_stop.py`` (the watcher's behavioral suite) and
``tests/lint/live_verdict_contract.py`` (the CE036 contract-replay fixtures) both
hand-build ``CommandTelemetry``/``TurnRecord`` trajectories for the same two live
checkers. The primitives live here so a telemetry field addition is threaded
through once; the *criterion* builders deliberately stay in each file — they
encode different defaults (armed with ``stop_early`` blocks vs unarmed contract
instances) and sharing them would just move the divergence into keyword soup.

The timestamp is frozen: CE036's determinism replay requires fixtures that carry
no nondeterminism of their own, and the watcher tests never read wall-clock off
telemetry either.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from coder_eval.models import CommandTelemetry, TurnRecord


FROZEN_TS = datetime(2026, 1, 1, 0, 0, 0)


def make_command(
    tool_name: str,
    parameters: dict[str, Any],
    *,
    tool_id: str | None = None,
    sequence_number: int = 0,
    result_status: Literal["success", "error", "unknown"] = "success",
) -> CommandTelemetry:
    """One recorded tool call. ``tool_id`` defaults to ``tool-<sequence_number>``."""
    return CommandTelemetry(
        tool_name=tool_name,
        tool_id=tool_id if tool_id is not None else f"tool-{sequence_number}",
        timestamp=FROZEN_TS,
        parameters=parameters,
        result_status=result_status,
        sequence_number=sequence_number,
    )


def make_turn(*commands: CommandTelemetry, iteration: int = 1) -> TurnRecord:
    return TurnRecord(iteration=iteration, user_input="", agent_output="", commands=list(commands))
