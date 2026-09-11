"""ATIF Trajectory -> coder-eval ``TurnRecord`` list (the hydrate direction).

The reverse of ``atif_emit``: given a trajectory that was produced OUTSIDE this
process (a Harbor agent ran ``coder-eval execute --format harbor``, which wrote
``trajectory.json`` next to ``task.json``), reconstruct enough of coder-eval's
own trajectory shape to let the criteria checkers that read it
(``command_executed``, ``cli_called``, ``commands_efficiency``,
``skill_triggered``, ``llm_judge``'s transcript) work against it during a
separate ``coder-eval evaluate --format harbor`` invocation.

Every criterion checker receives ``turn_records: list[TurnRecord] | None`` —
in a normal run this is ``EvaluationResult.iterations`` — and reads only
``TurnRecord.commands`` (tool calls) and ``TurnRecord.messages`` (for judge
transcripts); see ``criteria/command_executed.py``, ``criteria/skill_triggered.py``,
``criteria/commands_efficiency.py``. Those two fields are what this module
reconstructs. It does NOT attempt a lossless round-trip of ``atif_emit``'s
mapping:

- Per-generation token buckets (``AssistantMessage.input_tokens`` etc.) are
  NOT recovered from ``Step.metrics`` — ``TurnRecord.token_usage`` is left
  unset. Cost/token reporting for a hydrated result is therefore incomplete;
  only trajectory-shaped criteria are the target here.
- Sub-agent nesting is flattened: ``subagent_trajectories`` steps are appended
  to the parent turn's commands (via their tool_calls) rather than
  reconstructing a nested ``parent_tool_use_id`` relationship.
- Turn boundaries are recovered by splitting on ``source="user"`` steps
  (mirroring ``atif_emit``'s "one synthetic user step per turn" convention),
  not by any explicit iteration marker ATIF carries.
"""

from __future__ import annotations

from datetime import UTC, datetime

from coder_eval.harbor.atif_models import ObservationResult, Step, ToolCall, Trajectory
from coder_eval.models import CommandTelemetry, EvaluationResult, FinalStatus, TurnRecord


def _tool_call_to_command(
    call: ToolCall,
    results_by_id: dict[str, ObservationResult],
    *,
    assistant_turn_index: int,
) -> CommandTelemetry:
    result = results_by_id.get(call.tool_call_id)
    content = result.content if result is not None else None
    result_summary = content if isinstance(content, str) else (None if content is None else str(content))
    # The mirror of atif_emit's `_tool_calls_for`: `result_status` rides on the
    # ToolCall's own `extra` (call_extra) and `duration_ms` on its matching
    # ObservationResult's `extra` (result_extra) -- read both back, or
    # `command_executed(require_success: true)` silently scores a successful
    # command 0.0 on every hydrated trajectory (result_status defaults to None,
    # not "success").
    call_extra = call.extra or {}
    result_extra = (result.extra or {}) if result is not None else {}
    return CommandTelemetry(
        tool_name=call.function_name,
        tool_id=call.tool_call_id,
        timestamp=datetime.now(UTC),
        parameters=call.arguments,
        result_summary=result_summary,
        result_status=call_extra.get("result_status"),
        duration_ms=result_extra.get("duration_ms"),
        assistant_turn_index=assistant_turn_index,
        sequence_number=assistant_turn_index,
    )


def _step_text(step: Step) -> str:
    if isinstance(step.message, str):
        return step.message
    return "\n".join(part.text for part in step.message if part.type == "text" and part.text)


def _commands_for_step(step: Step, assistant_turn_index: int) -> list[CommandTelemetry]:
    if not step.tool_calls:
        return []
    results_by_id = {
        r.source_call_id: r
        for r in (step.observation.results if step.observation is not None else [])
        if r.source_call_id
    }
    return [
        _tool_call_to_command(call, results_by_id, assistant_turn_index=assistant_turn_index)
        for call in step.tool_calls
    ]


def _turn_from_steps(iteration: int, steps: list[Step]) -> TurnRecord:
    """Build one TurnRecord from a contiguous run of steps starting at a user step."""
    user_step = steps[0] if steps and steps[0].source == "user" else None
    agent_steps = [s for s in steps if s.source == "agent"]

    commands: list[CommandTelemetry] = []
    assistant_index = -1
    for step in steps:
        if step.source != "agent":
            continue
        assistant_index += 1
        commands.extend(_commands_for_step(step, assistant_index))

    return TurnRecord(
        iteration=iteration,
        user_input=_step_text(user_step) if user_step is not None else "",
        agent_output=_step_text(agent_steps[-1]) if agent_steps else "",
        commands=commands,
    )


def trajectory_to_turn_records(trajectory: Trajectory) -> list[TurnRecord]:
    """Split ``trajectory.steps`` into per-turn groups and reconstruct ``TurnRecord``s.

    Sub-agent trajectories are flattened in: each embedded child's tool calls
    are appended onto whichever main-thread turn contains the spawning tool
    call (matched by ``tool_call_id``), falling back to the last turn when no
    spawning call is found (an orphaned embed, mirroring ``atif_emit``'s own
    orphan-tolerant behavior on the way out).
    """
    groups: list[list[Step]] = []
    for step in trajectory.steps:
        if step.source == "user" or not groups:
            groups.append([step])
        else:
            groups[-1].append(step)

    turns = [_turn_from_steps(i + 1, steps) for i, steps in enumerate(groups)]

    for sub in trajectory.subagent_trajectories or []:
        sub_commands: list[CommandTelemetry] = []
        assistant_index = -1
        for step in sub.steps:
            if step.source != "agent":
                continue
            assistant_index += 1
            sub_commands.extend(_commands_for_step(step, assistant_index))
        if not sub_commands:
            continue
        target = turns[-1] if turns else None
        for turn, steps in zip(turns, groups, strict=True):
            if any(sub.trajectory_id in {tc.tool_call_id for tc in (s.tool_calls or [])} for s in steps):
                target = turn
                break
        if target is not None:
            target.commands.extend(sub_commands)

    return turns


def seed_from_atif_trajectory(
    trajectory: Trajectory,
    *,
    task_id: str,
    task_description: str = "",
    variant_id: str = "harbor",
) -> EvaluationResult:
    """Build a minimal ``EvaluationResult`` from an ATIF trajectory for detached grading.

    Only the fields ``evaluate --format harbor``'s grading path actually reads
    are populated meaningfully: ``iterations`` (via
    :func:`trajectory_to_turn_records`) and ``iteration_count``.
    ``final_status``/``weighted_score`` are placeholders — grading recomputes
    them; this object exists only to carry trajectory context into
    ``Orchestrator(prior_result=...)``, exactly as a re-graded run directory's
    own ``task.json`` does.
    """
    turns = trajectory_to_turn_records(trajectory)
    return EvaluationResult(
        task_id=task_id,
        task_description=task_description,
        variant_id=variant_id,
        agent_type=trajectory.agent.name,
        model_used=trajectory.agent.model_name,
        started_at=datetime.now(UTC),
        final_status=FinalStatus.NOT_GRADED,
        iteration_count=len(turns),
        iterations=turns,
    )


__all__ = ["seed_from_atif_trajectory", "trajectory_to_turn_records"]
