"""Join proxy-captured ACTUAL per-call cost/cache onto a run's turns.

For the open-weight (LiteLLM) backend the Claude binary's Anthropic transport
drops OpenRouter's real ``usage.cost`` + per-call cache before Python can see it,
so a proxy-side callback (``litellm/cost_logger.py``) writes one JSONL record per
call to ``LITELLM_COST_LOG``. This module reads those records back and stitches
them onto the matching turns:

* the turn's ``token_usage.total_cost_usd`` is overridden with the SUM of its
  calls' real cost (the bill), replacing the static rate-card estimate;
* the per-call breakdown is attached as ``TurnRecord.provider_call_costs`` (for
  the evalboard's per-call cache/cost view).

Reconciliation rule (see the plan): cost comes from the proxy actuals; token
buckets are left untouched (SDK-authoritative), so any reconciliation-row token
residual carries $0 rather than being re-priced at the rate card. A turn with no
matching record keeps its static estimate (whole-turn fallback).

Retry safety: multiple ``TurnRecord``s can share an ``iteration`` (a crashed
attempt + its retry), and the proxy calls of both carry that same iteration tag.
To avoid double-counting at the run level, an iteration's calls are credited to a
single survivor turn (the last with that iteration); earlier siblings are zeroed.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coder_eval.models import AssistantMessage, ProviderCallCost, ReconciliationMessage, TokenUsage


if TYPE_CHECKING:
    from coder_eval.models import EvaluationResult


def load_cost_records(path: str | Path) -> list[dict[str, Any]]:
    """Read the proxy's per-call JSONL cost log; tolerant of a missing file,
    blank lines, and partial/garbled lines (returns only well-formed dict rows)."""
    p = Path(path)
    if not p.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def _to_call(record: dict[str, Any]) -> ProviderCallCost:
    return ProviderCallCost(
        call_id=record.get("call_id"),
        cost_usd=record.get("cost"),
        input_tokens=record.get("input"),
        cache_read_tokens=record.get("cache_read"),
        cache_write_tokens=record.get("cache_write"),
        output_tokens=record.get("output"),
    )


def apply_actual_cost(
    result: EvaluationResult,
    *,
    run_id: str,
    task_id: str,
    records: list[dict[str, Any]],
) -> int:
    """Override each turn's cost with the proxy's real per-call cost and attach the
    per-call breakdown. Mutates ``result`` in place; call BEFORE the run-level token
    aggregation so the run total re-derives from the corrected per-turn costs.

    Args:
        result: the run's ``EvaluationResult`` (its ``iterations`` are the turns).
        run_id / task_id: the correlation tags the agent stamped (``x-ce-run-id`` /
            ``x-ce-task-id``); only records matching BOTH are joined.
        records: rows from :func:`load_cost_records`.

    Returns:
        The number of turns that received real cost (0 => everything kept its
        static estimate, e.g. an empty/mismatched log).
    """
    mine = [r for r in records if r.get("run_id") == run_id and r.get("task_id") == task_id]
    if not mine:
        return 0

    by_iteration: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in mine:
        by_iteration[str(record.get("iteration"))].append(record)

    turns = result.iterations
    # Survivor = the turn a given iteration's calls are credited to. Multiple
    # TurnRecords can share an iteration — a crash+retry, or (seen on multi-turn
    # runs) a trailing empty/model=None turn — so pick the LAST turn with that
    # iteration THAT HAS GENERATIONS (assistant messages); the calls belong to
    # whichever turn actually generated, and crediting an empty turn would strand
    # them off their generations. Fall back to the last turn if none have any.
    survivor_index: dict[str, int] = {}
    for i, turn in enumerate(turns):
        key = str(turn.iteration)
        has_generations = any(isinstance(m, AssistantMessage) for m in turn.messages)
        if key not in survivor_index:
            survivor_index[key] = i
        else:
            prev_has_generations = any(isinstance(m, AssistantMessage) for m in turns[survivor_index[key]].messages)
            if has_generations or not prev_has_generations:
                survivor_index[key] = i

    applied = 0
    for i, turn in enumerate(turns):
        key = str(turn.iteration)
        turn_records = by_iteration.get(key)
        if not turn_records:
            continue  # no proxy data for this iteration → keep the static estimate
        if survivor_index[key] != i:
            # Earlier crashed sibling of a retried iteration: its calls are credited
            # to the survivor, so zero it here to keep the run aggregate exact.
            if turn.token_usage is not None:
                turn.token_usage.total_cost_usd = 0.0
            continue

        calls = [_to_call(record) for record in turn_records]
        turn.provider_call_costs = calls
        costs = [c.cost_usd for c in calls if c.cost_usd is not None]
        if costs:
            total = sum(costs)
            if turn.token_usage is None:
                turn.token_usage = TokenUsage(total_cost_usd=total)
            else:
                turn.token_usage.total_cost_usd = total
        _distribute_onto_messages(turn, calls)
        applied += 1
    return applied


def _distribute_onto_messages(turn: Any, calls: list[ProviderCallCost]) -> None:
    """Attribute each proxy call's real tokens + cost onto its generation in the
    turn's transcript and reconcile the residual, so the message timeline shows
    real per-call cache/cost.

    A generation is one ``message_id`` group; it's matched to a proxy call by
    ``output_tokens`` — both sides carry the real per-call output (the CLI just
    splits one call's output across block emissions, so the group sums back to the
    call total). The match is an ORDER-RESPECTING walk: the transcript and the
    proxy log are both chronological, so each generation binds to the next call
    whose output matches, and a call matching no pending generation (an auxiliary
    small-model call — no ``message_id`` in the transcript, e.g. Claude Code's
    background haiku/init call) is skipped into reconcile. Distribution is applied
    ONLY if every generation bound cleanly, in order; otherwise (sub-agent runs
    whose transcript vs proxy orderings diverge, or any output disagreement) the
    stream is left sparse — we never present a guessed per-generation split.
    Empirically clean on 14/15 observed runs (all but the sub-agent one).

    The reconciliation row is ALWAYS set to the residual tokens + the real cost not
    attributed to a generation (the whole total when distribution was skipped), so
    the timeline reconciles to the bill and the price is visible there, not blank.
    """
    assistants = [m for m in turn.messages if isinstance(m, AssistantMessage)]
    reconciliation = next((m for m in turn.messages if isinstance(m, ReconciliationMessage)), None)

    # Group generations by message_id (in first-seen order). A missing id disables
    # grouping (→ distribution skipped; the reconcile step below still runs).
    groups: dict[str, list[AssistantMessage]] = {}
    order: list[str] = []
    groupable = True
    for message in assistants:
        if message.message_id is None:
            groupable = False
            break
        if message.message_id not in groups:
            groups[message.message_id] = []
            order.append(message.message_id)
        groups[message.message_id].append(message)

    if groupable and order:
        gen_outputs = [sum(m.output_tokens for m in groups[mid]) for mid in order]
        # Order-respecting walk: both the transcript and the proxy log are
        # chronological, so bind each generation, in order, to the NEXT call whose
        # output matches; a call that matches no pending generation is an aux /
        # unpaired call and is skipped (it lands in reconcile). Unlike a
        # match-anywhere greedy this can't grab a same-output call out of sequence.
        matched: list[tuple[str, ProviderCallCost]] = []
        gi = 0
        for call in calls:
            if gi < len(order) and (call.output_tokens or 0) == gen_outputs[gi]:
                matched.append((order[gi], call))
                gi += 1

        # Distribute ONLY when every generation bound cleanly, in order. Otherwise
        # — sub-agent runs where the transcript vs proxy orderings diverge, or any
        # output disagreement — leave the stream sparse and let the reconcile step
        # carry the real total, rather than present a guessed per-generation split.
        if gi == len(order):
            for message_id, call in matched:
                members = groups[message_id]
                cache_read = call.cache_read_tokens or 0
                cache_write = call.cache_write_tokens or 0
                rep = members[0]  # the CLI records a generation's billing on its first emission
                rep.input_tokens = max(0, (call.input_tokens or 0) - cache_read - cache_write)  # uncached slice
                rep.cache_read_tokens = cache_read
                rep.cache_creation_tokens = cache_write
                rep.cost_usd = call.cost_usd
                for other in members[1:]:
                    other.input_tokens = 0
                    other.cache_read_tokens = 0
                    other.cache_creation_tokens = 0
                    other.cost_usd = None

    if reconciliation is None or turn.token_usage is None:
        return
    # Reconcile ALWAYS: the residual tokens keep the four-bucket sum equal to the
    # authoritative turn total (the invariant), and the residual cost = the real
    # total minus what landed on generations (the whole total when distribution was
    # skipped) — so the timeline shows the correct price even in the fallback case.
    usage = turn.token_usage
    reconciliation.input_tokens = max(0, usage.uncached_input_tokens - sum(m.input_tokens for m in assistants))
    reconciliation.cache_read_tokens = max(
        0, usage.cache_read_input_tokens - sum(m.cache_read_tokens for m in assistants)
    )
    reconciliation.cache_creation_tokens = max(
        0, usage.cache_creation_input_tokens - sum(m.cache_creation_tokens for m in assistants)
    )
    reconciliation.output_tokens = max(0, usage.output_tokens - sum(m.output_tokens for m in assistants))
    if usage.total_cost_usd is not None:
        reconciliation.cost_usd = usage.total_cost_usd - sum(m.cost_usd or 0.0 for m in assistants)
    else:
        reconciliation.cost_usd = None
