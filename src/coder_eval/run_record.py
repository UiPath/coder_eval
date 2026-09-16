"""The run.json task-row serializer.

``eval_result_to_task_dict`` projects one finished ``EvaluationResult`` into the
row that lands in ``run.json``. It is a **run-record serializer, not a report**:
the batch runner writes these rows during a run, and the reporters read them
afterwards. Its previous home inside the experiment reporter was the only reason
``orchestration/batch.py`` imported from the reports layer at all.

The keys this function writes are a contract — the evalboard and every archived
run read them — so changing one is a breaking change, not a rename. See
``docs/REPORT_SCHEMA.md``.
"""

from __future__ import annotations

from typing import Any

from coder_eval.errors import truncate_crash_message
from coder_eval.models import EvaluationResult, FinalStatus, judge_cost_usd, simulator_cost_usd, sum_costs
from coder_eval.result_metrics import expected_turns_overage, turn_time_buckets, visible_turn_count
from coder_eval.result_metrics import has_final_reply as _has_final_reply


# Cap on the ``error_message`` carried into each run.json row: enough to identify
# a failure without fetching the task artifact, short enough that a wholly-errored
# run doesn't bloat run.json. The untruncated message stays on task.json.
_ROW_ERROR_MESSAGE_MAX_CHARS = 400


def _cost_complete(result: EvaluationResult) -> bool:
    """Whether this row's recorded agent spend accounts for everything it spent.

    False means the costs on the row are a floor, not the bill. Two ways in:

    1. A turn burned tokens the rate card could not price. The card is the fallback
       for anything the backend did not price itself, so with no rate those tokens
       book no money.
    2. The task was hard-killed by the task-level timeout. Keyed on the status
       rather than on emptiness: the watchdog fires while the evaluation loop is
       running, so a TIMEOUT row always lost an in-flight turn, even one that
       completed earlier turns that do carry costs.

    True for a row that burned nothing: an error before the agent ran genuinely
    cost zero, and a slow setup failure is as free as a fast one.
    """
    if result.final_status is FinalStatus.TIMEOUT:
        return False
    return all(
        usage.total_cost_usd is not None
        for t in result.iterations
        if (usage := t.token_usage) is not None and not usage.is_empty()
    )


def eval_result_to_task_dict(
    result: EvaluationResult,
    *,
    variant_id: str | None = None,
    tags: list[str] | None = None,
    task_path: str | None = None,
    duration_override: float | None = None,
    replicate_index: int | None = None,
) -> dict[str, Any]:
    """Convert an EvaluationResult to the task_result dict format used by ReportGenerator.

    Args:
        result: The evaluation result to convert.
        variant_id: Optional variant ID to include in the dict.
        tags: Optional tags list (defaults to []).
        task_path: Optional path of the task YAML (as supplied to the runner) —
            lets downstream consumers (evalboard) derive groupings like skill
            from the source folder structure instead of guessing from tags.
        duration_override: Optional duration value (defaults to result.duration_seconds).
        replicate_index: Replicate index of this row (the ``<variant>/<task>/<NN>``
            sub-dir). Repeated runs of the same task share a ``task_id``, so
            without this the row is indistinguishable from its siblings and
            downstream consumers (evalboard) collapse them to one. ``None`` when
            the caller doesn't track replicates (repeats disabled / legacy).
    """

    ref_similarity: float | None = None
    for cr in result.success_criteria_results:
        if cr.criterion_type == "reference_comparison":
            ref_similarity = cr.score
            break

    overage = expected_turns_overage(result)

    total_turns = sum((t.num_turns or 0) for t in result.iterations)

    # Carried as a row-level boolean so the evalboard can compute the visible turn
    # count without re-reading per-task content.
    has_reply = _has_final_reply(result)

    agent_cost = result.total_token_usage.total_cost_usd if result.total_token_usage else None
    judge_cost = judge_cost_usd(result)
    simulator_cost = simulator_cost_usd(result)
    row_total_cost = sum_costs(agent_cost, judge_cost, simulator_cost)

    expected_turns_value: int | None = None
    if result.task_config is not None:
        rl = (result.task_config.resolved or {}).get("run_limits") or {}
        if isinstance(rl, dict):
            raw = rl.get("expected_turns")
            if isinstance(raw, int) and raw >= 1:
                expected_turns_value = raw

    _buckets = turn_time_buckets(result)

    d: dict[str, Any] = {
        "task_id": result.task_id,
        "replicate_index": replicate_index,
        "status": result.final_status,
        "weighted_score": result.weighted_score,
        "duration": duration_override if duration_override is not None else result.duration_seconds,
        "iteration_count": result.iteration_count,
        "tags": tags if tags is not None else [],
        "task_path": task_path,
        "iterations": [
            {
                "iteration": t.iteration,
                "duration_seconds": t.duration_seconds,
                "command_count": len(t.commands),
                "assistant_turn_count": t.assistant_turn_count,
                "crashed": t.crashed,
                "crash_reason": t.crash_reason,
            }
            for t in result.iterations
        ],
        # Computed ONCE through `turn_time_buckets` and carried as TASK-level keys.
        # `iterations` below is a deliberate 6-key projection, so no renderer can
        # re-derive them. Each stays `float | None` (CE049).
        # Rationale: .claude/notes/reporting.md § Read the stored value, do not re-derive it
        "startup_ms": _buckets.startup_ms,
        "generation_ms": _buckets.generation_ms,
        "tool_ms": _buckets.tool_ms,
        "teardown_ms": _buckets.teardown_ms,
        "model_used": result.model_used,
        "reference_similarity": ref_similarity,
        # The UNCACHED slice, not TokenUsage.input_tokens; evalboard/lib/runs.ts
        # depends on that reading and the run.json contract fixes the name.
        "input_tokens": (result.total_token_usage.uncached_input_tokens if result.total_token_usage else None),
        "output_tokens": (result.total_token_usage.output_tokens if result.total_token_usage else None),
        "cache_creation_input_tokens": (
            result.total_token_usage.cache_creation_input_tokens if result.total_token_usage else None
        ),
        "cache_read_input_tokens": (
            result.total_token_usage.cache_read_input_tokens if result.total_token_usage else None
        ),
        "total_tokens": (result.total_token_usage.total_tokens if result.total_token_usage else None),
        # agent + judge + simulator. `total_cost_usd` means the whole bill on every
        # surface. None when nothing was priced at all.
        "total_cost_usd": row_total_cost,
        # Subject-agent spend alone: judge cost is identical across harnesses, so
        # leaving it in would make two look closer than they are.
        "agent_cost_usd": agent_cost,
        # False when the agent spend above is missing money, so it is a floor.
        # Rolled up as RunSummary.tasks_cost_incomplete / cost_complete.
        "cost_complete": _cost_complete(result),
        # The two halves of the eval-machinery bill, rolled up as
        # RunSummary.eval_overhead_cost_usd.
        "judge_cost_usd": judge_cost,
        "simulator_cost_usd": simulator_cost,
        # Errors count as misses, so the rollup has to say why it lost those points.
        # Without these, triaging an errored run needs one task.json fetch per row.
        "error_message": (
            truncate_crash_message(result.error_message, limit=_ROW_ERROR_MESSAGE_MAX_CHARS)
            if result.error_message
            else None
        ),
        "error_category": (result.error_details or {}).get("error_category"),
        "expected_commands": result.expected_commands,
        "actual_commands": result.actual_commands,
        "commands_efficiency": result.commands_efficiency,
        "agent_config": (result.agent_config.model_dump() if result.agent_config else None),
        "sdk_options": result.sdk_options,
        "installed_tools": result.environment_info.get("installed_tools"),
        "max_turns_exhausted": result.max_turns_exhausted,
        "expected_turns_overage": list(overage) if overage is not None else None,
        "total_turns": total_turns,
        # "Visible turns" (tool calls + final reply) -- what the "within expected
        # turns" metric compares against. Distinct from total_turns (SDK num_turns).
        "visible_turns": visible_turn_count(result),
        "expected_turns": expected_turns_value,
        "has_final_reply": has_reply,
        # None/False on the default path, so downstream analysis never confuses a
        # truncated run with a full one.
        "stopped_early": result.early_stop is not None,
        "early_stop_reason": (result.early_stop.reason.value if result.early_stop is not None else None),
        "turns_remaining_at_stop": (
            result.early_stop.turns_remaining_at_stop if result.early_stop is not None else None
        ),
        # The threshold in effect for this stop, so a sweep that varies it can tell
        # which weighted-gate value produced a given verdict.
        "gate_threshold": (result.early_stop.gate_threshold if result.early_stop is not None else None),
    }
    d["variant_id"] = variant_id
    return d
