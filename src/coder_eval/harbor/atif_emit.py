"""EvaluationResult → ATIF Trajectory converter (the emit direction).

Maps coder_eval's persisted trajectory (``EvaluationResult.iterations`` — the
``TurnRecord`` envelope over the per-generation ``messages`` stream) onto the
vendored ATIF models, so every run can be consumed by ``harbor view``, Harbor
Hub, and ATIF-based SFT/RL pipelines.

Mapping highlights (full table in c/2026-07-20-adopt-atif-trajectory-emit.md):

- ``UserMessage`` → ``Step(source="user")``; ``AssistantMessage`` (one per LLM
  generation) → ``Step(source="agent")`` with per-generation ``Metrics``.
- ``CommandTelemetry`` joins its generation via ``assistant_turn_index`` and
  becomes that step's ``tool_calls`` + ``observation``.
- Sub-agent generations (``parent_tool_use_id`` set) are NESTED into embedded
  ``subagent_trajectories`` — flattening them into the main thread would
  corrupt SFT data derived from the trajectory.
- ``ReconciliationMessage`` entries never become steps: their residuals are
  recorded in ``Trajectory.extra["reconciliation"]`` and are already included
  in the authoritative ``FinalMetrics`` totals (``total_token_usage``).
- Turns with no message stream (legacy task.json, minimal agents) degrade to
  one synthetic user step + one agent step carrying all the turn's commands.

The converter is a PURE function of the models: no I/O, no agent-type
branching, no mutation of the input result.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from coder_eval.harbor.atif_models import (
    AtifAgent,
    FinalMetrics,
    Metrics,
    Observation,
    ObservationResult,
    Step,
    SubagentTrajectoryRef,
    ToolCall,
    Trajectory,
)
from coder_eval.models import (
    AssistantMessage,
    CommandTelemetry,
    EvaluationResult,
    ReconciliationMessage,
    TokenUsage,
    TurnRecord,
    UserMessage,
)
from coder_eval.path_utils import atomic_write_text


if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def _message_text(msg: AssistantMessage) -> str:
    """Concatenate the message's text blocks in emission order."""
    parts = [b.text for b in msg.content_blocks if b.block_type == "text" and b.text]
    return "\n".join(parts)


def _reasoning_text(msg: AssistantMessage) -> str | None:
    """Concatenate the message's thinking blocks, or None when there are none."""
    parts = [b.thinking for b in msg.content_blocks if b.block_type == "thinking" and b.thinking]
    return "\n".join(parts) if parts else None


def _metrics_for(msg: AssistantMessage) -> Metrics | None:
    """Per-generation Metrics; None when the message carries no token buckets."""
    if not any((msg.input_tokens, msg.output_tokens, msg.cache_creation_tokens, msg.cache_read_tokens)):
        return None
    # SSOT: the "three prompt buckets sum to the full prompt" rule lives in
    # TokenUsage.input_tokens (a computed field) — do not hand-add them here.
    usage = TokenUsage(
        uncached_input_tokens=msg.input_tokens,
        cache_creation_input_tokens=msg.cache_creation_tokens,
        cache_read_input_tokens=msg.cache_read_tokens,
        output_tokens=msg.output_tokens,
    )
    return Metrics(
        prompt_tokens=usage.input_tokens,
        completion_tokens=msg.output_tokens,
        cached_tokens=msg.cache_read_tokens,
    )


def _assistant_extra(msg: AssistantMessage, iteration: int) -> dict[str, Any]:
    """Step.extra for one generation: iteration tag + fidelity ATIF has no slot for."""
    extra: dict[str, Any] = {"iteration": iteration}
    if msg.cache_creation_tokens:
        extra["cache_creation_tokens"] = msg.cache_creation_tokens
    if msg.reasoning_tokens:
        extra["reasoning_tokens"] = msg.reasoning_tokens
    if msg.message_id is not None:
        extra["message_id"] = msg.message_id
    if msg.stop_reason is not None:
        extra["stop_reason"] = msg.stop_reason
    return extra


def _tool_calls_for(commands: list[CommandTelemetry]) -> tuple[list[ToolCall], Observation] | tuple[None, None]:
    """Build a step's tool_calls + observation from its commands (None when empty)."""
    if not commands:
        return None, None
    calls: list[ToolCall] = []
    results: list[ObservationResult] = []
    for cmd in commands:
        call_extra: dict[str, Any] = {}
        if cmd.result_status is not None:
            call_extra["result_status"] = cmd.result_status
        calls.append(
            ToolCall(
                tool_call_id=cmd.tool_id,
                function_name=cmd.tool_name,
                arguments=cmd.parameters,
                extra=call_extra or None,
            )
        )
        result_extra: dict[str, Any] = {}
        if cmd.duration_ms is not None:
            result_extra["duration_ms"] = cmd.duration_ms
        results.append(
            ObservationResult(
                source_call_id=cmd.tool_id,
                content=cmd.result_summary,
                extra=result_extra or None,
            )
        )
    return calls, Observation(results=results)


class _StepBuilder:
    """Accumulates main-thread and per-sub-agent step lists with sequential ids."""

    def __init__(self) -> None:
        self.main: list[Step] = []
        # parent_tool_use_id -> child steps, insertion-ordered.
        self.subagents: dict[str, list[Step]] = {}

    def append(self, parent_tool_use_id: str | None, **step_fields: Any) -> Step:
        target = self.main if parent_tool_use_id is None else self.subagents.setdefault(parent_tool_use_id, [])
        step = Step(step_id=len(target) + 1, **step_fields)
        target.append(step)
        return step

    def last_main_agent_step(self, start: int = 0) -> Step | None:
        """Most recent main-thread agent step at or after index ``start``.

        ``start`` scopes the search to the current turn (leftover-command
        attribution must never cross turn boundaries — a command attached to a
        previous iteration's step would mislabel its ``extra["iteration"]``).
        """
        for step in reversed(self.main[start:]):
            if step.source == "agent":
                return step
        return None


def _extend_step_commands(step: Step, commands: list[CommandTelemetry]) -> None:
    """Attach extra commands to an already-built step (fallback attribution)."""
    calls, observation = _tool_calls_for(commands)
    if calls is None or observation is None:
        return
    step.tool_calls = [*(step.tool_calls or []), *calls]
    existing = step.observation.results if step.observation is not None else []
    step.observation = Observation(results=[*existing, *observation.results])


def _emit_turn(builder: _StepBuilder, turn: TurnRecord, reconciliation: list[dict[str, Any]]) -> None:
    """Emit one TurnRecord's steps into the builder (main thread + sub-agent groups)."""
    turn_start = len(builder.main)
    turn_extra: dict[str, Any] = {"iteration": turn.iteration}
    if turn.crashed:
        turn_extra["crashed"] = True

    assistant_msgs = [m for m in turn.messages if isinstance(m, AssistantMessage)]

    # Group commands by their generation; collect the unattributable ones.
    commands_by_index: dict[int, list[CommandTelemetry]] = {}
    leftover_commands: list[CommandTelemetry] = []
    for cmd in turn.commands:
        idx = cmd.assistant_turn_index
        if idx is not None and 0 <= idx < len(assistant_msgs):
            commands_by_index.setdefault(idx, []).append(cmd)
        else:
            if idx is not None:
                logger.debug("assistant_turn_index %s out of range for turn %s", idx, turn.iteration)
            leftover_commands.append(cmd)

    first_step_of_turn = True

    def _step_extra(base: dict[str, Any]) -> dict[str, Any]:
        nonlocal first_step_of_turn
        extra = {**turn_extra, **base}
        if first_step_of_turn and turn.crashed and turn.crash_reason is not None:
            extra["crash_reason"] = turn.crash_reason
        first_step_of_turn = False
        return extra

    # Single-shot runs carry no UserMessage in the stream — synthesize the
    # iteration's user step from user_input (exactly one per iteration).
    if not any(isinstance(m, UserMessage) for m in turn.messages):
        builder.append(None, source="user", message=turn.user_input, extra=_step_extra({}))

    if not turn.messages:
        # Legacy / minimal-agent turn (EMPTY message stream — a stream that has
        # user/reconciliation entries but no generations takes the normal path
        # below so those entries survive): one agent step from agent_output
        # carrying ALL the turn's commands and the turn-level token usage.
        metrics = None
        if turn.token_usage is not None and not turn.token_usage.is_empty():
            metrics = Metrics(
                prompt_tokens=turn.token_usage.input_tokens,
                completion_tokens=turn.token_usage.output_tokens,
                cached_tokens=turn.token_usage.cache_read_input_tokens,
            )
        calls, observation = _tool_calls_for(turn.commands)
        builder.append(
            None,
            source="agent",
            message=turn.agent_output,
            model_name=turn.model_used,
            metrics=metrics,
            tool_calls=calls,
            observation=observation,
            extra=_step_extra({}),
        )
        return

    assistant_index = -1
    for msg in turn.messages:
        if isinstance(msg, ReconciliationMessage):
            reconciliation.append(
                {
                    "iteration": turn.iteration,
                    "input_tokens": msg.input_tokens,
                    "output_tokens": msg.output_tokens,
                    "cache_creation_tokens": msg.cache_creation_tokens,
                    "cache_read_tokens": msg.cache_read_tokens,
                    "note": msg.note,
                }
            )
            continue
        if isinstance(msg, UserMessage):
            # Every UserMessage is a genuine user utterance today (constructed
            # only for simulator turns / pinned openers). If a tool-result
            # UserMessage variant ever gains a producing code path (its
            # docstring reserves one), the converter must learn to SKIP those
            # here — tool results already live in step observations.
            builder.append(
                None,
                source="user",
                message=msg.text,
                timestamp=msg.completed_at.isoformat() if msg.completed_at is not None else None,
                extra=_step_extra({}),
            )
            continue
        assistant_index += 1
        calls, observation = _tool_calls_for(commands_by_index.get(assistant_index, []))
        builder.append(
            msg.parent_tool_use_id,
            source="agent",
            message=_message_text(msg),
            reasoning_content=_reasoning_text(msg),
            model_name=msg.model,
            timestamp=msg.completed_at.isoformat(),
            metrics=_metrics_for(msg),
            tool_calls=calls,
            observation=observation,
            extra=_step_extra(_assistant_extra(msg, turn.iteration)),
        )

    if leftover_commands:
        # Documented fallback: unattributable commands ride the turn's last
        # main-thread agent step; synthesize one from agent_output if none exists.
        target = builder.last_main_agent_step(start=turn_start)
        if target is None:
            target = builder.append(None, source="agent", message=turn.agent_output, extra=_step_extra({}))
        _extend_step_commands(target, leftover_commands)


def _attach_subagent_refs(main_steps: list[Step], parent_ids: list[str]) -> None:
    """Point each spawning tool call's observation at its embedded child trajectory.

    Only when a main-thread ToolCall with ``tool_call_id == parent_id`` exists —
    never fabricate a tool call for an orphaned sub-agent group.
    """
    for parent_id in parent_ids:
        for step in main_steps:
            if not step.tool_calls or all(tc.tool_call_id != parent_id for tc in step.tool_calls):
                continue
            ref = SubagentTrajectoryRef(trajectory_id=parent_id)
            results = list(step.observation.results) if step.observation is not None else []
            for result in results:
                if result.source_call_id == parent_id:
                    result.subagent_trajectory_ref = [ref]
                    break
            else:
                results.append(ObservationResult(source_call_id=parent_id, subagent_trajectory_ref=[ref]))
            step.observation = Observation(results=results)
            break


def _total_usage(result: EvaluationResult) -> TokenUsage | None:
    """The authoritative run total, falling back to summing per-turn usage."""
    if result.total_token_usage is not None:
        return result.total_token_usage
    usages = [t.token_usage for t in result.iterations if t.token_usage is not None]
    if not usages:
        return None
    total = usages[0]
    for usage in usages[1:]:
        total = total + usage
    return total


def evaluation_result_to_trajectory(result: EvaluationResult) -> Trajectory | None:
    """Convert a finished EvaluationResult into an ATIF Trajectory.

    Returns None when no steps would result (e.g. evaluate-only runs) — ATIF
    requires at least one step.
    """
    from coder_eval import __version__

    builder = _StepBuilder()
    reconciliation: list[dict[str, Any]] = []
    for turn in result.iterations:
        _emit_turn(builder, turn, reconciliation)

    if not builder.main:
        return None

    _attach_subagent_refs(builder.main, list(builder.subagents))

    agent = AtifAgent(name=result.agent_type, version=__version__, model_name=result.model_used)
    session_id = f"{result.task_id}/{result.variant_id}"
    subagent_trajectories = [
        Trajectory(session_id=session_id, trajectory_id=parent_id, agent=agent, steps=steps)
        for parent_id, steps in builder.subagents.items()
    ]

    final_metrics: FinalMetrics | None = None
    total = _total_usage(result)
    if total is not None:
        final_metrics = FinalMetrics(
            total_prompt_tokens=total.input_tokens,
            total_completion_tokens=total.output_tokens,
            total_cached_tokens=total.cache_read_input_tokens,
            total_cost_usd=total.total_cost_usd,
            total_steps=len(builder.main),
        )

    return Trajectory(
        session_id=session_id,
        agent=agent,
        steps=builder.main,
        final_metrics=final_metrics,
        extra={"reconciliation": reconciliation} if reconciliation else None,
        subagent_trajectories=subagent_trajectories or None,
    )


def write_trajectory_json_strict(result: EvaluationResult, path: Path) -> Path | None:
    """Convert and atomically write ``trajectory.json``; RAISES on failure.

    Returns the written path, or None ONLY for the legitimate zero-step skip
    (ATIF requires >= 1 step; e.g. evaluate-only runs). Conversion or write
    errors propagate — for callers that must distinguish "nothing to emit"
    from "emission failed" (the report backfill).
    """
    trajectory = evaluation_result_to_trajectory(result)
    if trajectory is None:
        logger.debug("No trajectory steps for %s — skipping trajectory.json", result.task_id)
        return None
    atomic_write_text(path, trajectory.model_dump_json(indent=2, exclude_none=True) + "\n")
    return path


def write_trajectory_json(result: EvaluationResult, path: Path) -> Path | None:
    """Convert and atomically write ``trajectory.json``; never raises.

    Returns the written path, or None when the result yields no steps or the
    conversion/write fails (logged at WARNING — a trajectory failure must
    never mask the run outcome). The orchestrator's finalize path uses this
    variant; see :func:`write_trajectory_json_strict` for the raising one.
    """
    try:
        return write_trajectory_json_strict(result, path)
    except Exception:
        logger.warning("Failed to write trajectory.json for %s", result.task_id, exc_info=True)
        return None
