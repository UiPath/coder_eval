"""Join proxy-captured ACTUAL per-call cost/cache onto a run's turns.

For the open-weight (LiteLLM) backend the Claude binary's Anthropic transport
drops OpenRouter's real ``usage.cost`` + per-call cache before Python can see it,
so a proxy-side callback (``litellm/cost_logger.py``) writes one JSONL record per
call to ``LITELLM_COST_LOG``. This module reads those records back and stitches
them onto the matching turns:

* the turn's ``token_usage.total_cost_usd`` is overridden with the SUM of its
  calls' real cost (the bill), replacing the static rate-card estimate;
* the per-call breakdown is attached as ``TurnRecord.provider_call_costs``.

Ownership rule (see the plan). Cost always comes from the proxy actuals. Token
buckets have a single owner *per turn*:

* when every generation binds cleanly to a call (the common case), the proxy is
  authoritative for that turn — each generation's buckets are rewritten from its
  call AND ``token_usage`` is recomputed from the summed calls, so the invariant
  ``Σ(four buckets over messages) == token_usage`` holds EXACTLY (the reconcile
  residual is zero, never clamped). This is required because on the LiteLLM route
  the SDK reports ``cache_read_input_tokens == 0`` while the proxy carries the
  real cache read — so leaving the SDK buckets would break the invariant the
  evalboard's ``selectTokenTotals`` relies on;
* when the walk cannot bind every generation (sub-agent runs whose transcript vs
  proxy orderings diverge), the SDK token buckets are left untouched
  (SDK-authoritative, invariant already holds) and only the whole-turn cost is
  attributed onto the reconciliation row.

Partial coverage. A turn's cost is overridden ONLY when every one of its calls
reports a cost. If any call is unpriced (e.g. a Bedrock-served model on the same
proxy returns no OpenRouter ``usage.cost``), overriding would bill those calls at
$0 and understate the turn — so the turn keeps its static estimate, no breakdown
is attached, and a warning names the unpriced call ids. A turn with no matching
record keeps its static estimate too (whole-turn fallback).

Retry safety: multiple ``TurnRecord``s can share an ``iteration`` (a crashed
attempt + its retry), and the proxy calls of both carry that same iteration tag.
To avoid double-counting at the run level, an iteration's calls are credited to a
single survivor turn (the last with that iteration THAT HAS GENERATIONS); earlier
siblings are zeroed.

Transactional: the full plan is computed before any turn is mutated, so a
malformed record (which raises while building the per-call breakdown) aborts the
whole join with the run left untouched — matching the caller's "keeping static
pricing" contract.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coder_eval.models import AssistantMessage, ProviderCallCost, ReconciliationMessage, TokenUsage


if TYPE_CHECKING:
    from coder_eval.models import EvaluationResult, TurnRecord


logger = logging.getLogger(__name__)


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
    attempt: str | None = None,
    records: list[dict[str, Any]],
) -> int:
    """Override each turn's cost with the proxy's real per-call cost and attach the
    per-call breakdown. Mutates ``result`` in place; call BEFORE the run-level token
    aggregation so the run total re-derives from the corrected per-turn costs.

    Args:
        result: the run's ``EvaluationResult`` (its ``iterations`` are the turns).
        run_id / task_id: the correlation tags the agent stamped (``x-ce-run-id`` /
            ``x-ce-task-id``); only records matching BOTH are joined.
        attempt: the per-attempt nonce (``x-ce-attempt``). When given, records must
            also match it — so a re-run into the same ``--run-dir`` (same run_id) does
            not re-match, and double-count, a prior attempt's rows in the append-only
            log. ``None`` disables the check (records without the tag still join).
        records: rows from :func:`load_cost_records`.

    Returns:
        The number of turns that received real cost (0 => everything kept its
        static estimate, e.g. an empty/mismatched log).
    """
    mine = [
        r
        for r in records
        if r.get("run_id") == run_id
        and r.get("task_id") == task_id
        and (attempt is None or r.get("attempt") == attempt)
    ]
    if not mine:
        return 0

    by_iteration: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in mine:
        by_iteration[str(record.get("iteration"))].append(record)

    turns = result.iterations
    survivor_index = _survivor_index(turns)

    # Phase 1 — COMPUTE the plan without mutating any turn. Building the per-call
    # breakdown (_to_call) validates each record, so a malformed row raises HERE,
    # before any mutation, and the caller keeps static pricing for the whole run
    # rather than persisting a half-joined mix.
    to_credit: list[tuple[TurnRecord, list[ProviderCallCost]]] = []
    to_zero: list[TurnRecord] = []
    matched_iterations: set[str] = set()
    for i, turn in enumerate(turns):
        key = str(turn.iteration)
        turn_records = by_iteration.get(key)
        if not turn_records:
            continue  # no proxy data for this iteration → keep the static estimate
        matched_iterations.add(key)
        if survivor_index[key] != i:
            to_zero.append(turn)  # earlier crashed sibling of a retried iteration
            continue
        to_credit.append((turn, [_to_call(record) for record in turn_records]))

    # Phase 2 — MUTATE. Pure attribute assignment below; nothing raises.
    for turn in to_zero:
        # Credited to the survivor instead, so zero it to keep the run aggregate exact.
        if turn.token_usage is not None:
            turn.token_usage.total_cost_usd = 0.0

    applied = 0
    for turn, calls in to_credit:
        unpriced = [c.call_id for c in calls if c.cost_usd is None]
        if unpriced:
            # Partial (or total) unpriced coverage: overriding would bill the
            # null-cost calls at $0 and understate the turn, so keep the static
            # estimate and attach no misleading breakdown. Loud, not silent.
            logger.warning(
                "LiteLLM actual-cost: iteration=%s has %d/%d unpriced call(s) %s; keeping the static estimate",
                turn.iteration,
                len(unpriced),
                len(calls),
                unpriced,
            )
            continue
        turn.provider_call_costs = calls
        total = sum(c.cost_usd for c in calls if c.cost_usd is not None)
        if turn.token_usage is None:
            turn.token_usage = TokenUsage(total_cost_usd=total)
        else:
            turn.token_usage.total_cost_usd = total
        _distribute_onto_messages(turn, calls)
        applied += 1

    orphans = sorted(set(by_iteration) - matched_iterations)
    if orphans:
        logger.warning(
            "LiteLLM actual-cost: %d cost-record iteration(s) %s matched no turn (run=%s task=%s); spend unbooked",
            len(orphans),
            orphans,
            run_id,
            task_id,
        )
    return applied


def _survivor_index(turns: list[TurnRecord]) -> dict[str, int]:
    """Map each iteration to the turn its calls are credited to. Multiple
    ``TurnRecord``s can share an iteration — a crash+retry, or (multi-turn runs) a
    trailing empty/model=None turn — so pick the LAST turn with that iteration THAT
    HAS GENERATIONS; the calls belong to whichever turn actually generated, and
    crediting an empty turn would strand them off their generations. Fall back to
    the last turn if none have any."""
    survivor: dict[str, int] = {}
    for i, turn in enumerate(turns):
        key = str(turn.iteration)
        has_generations = any(isinstance(m, AssistantMessage) for m in turn.messages)
        if key not in survivor:
            survivor[key] = i
        else:
            prev_has_generations = any(isinstance(m, AssistantMessage) for m in turns[survivor[key]].messages)
            if has_generations or not prev_has_generations:
                survivor[key] = i
    return survivor


def _distribute_onto_messages(turn: TurnRecord, calls: list[ProviderCallCost]) -> None:
    """Attribute each proxy call's real tokens + cost onto its generation in the
    turn's transcript and reconcile the residual, so the message timeline shows
    real per-call cache/cost while the four-bucket invariant is preserved.

    A generation is one ``message_id`` group; it's matched to a proxy call by an
    ORDER-RESPECTING walk on ``output_tokens`` (both the transcript and the proxy
    log are chronological, so each generation binds to the next call whose output
    matches; a call matching no pending generation — an auxiliary small-model call
    with no transcript ``message_id`` — is skipped into reconcile).

    On a clean bind (every generation matched, in order) the proxy is authoritative
    for this turn: each generation's buckets + cost are rewritten from its call and
    ``token_usage`` is recomputed from the summed calls, so the reconcile residual
    is exactly zero (no clamp). Otherwise (sub-agent orderings diverge) the SDK
    token buckets are left untouched — invariant already holds — and only the
    whole-turn cost lands on the reconciliation row.
    """
    assistants = [m for m in turn.messages if isinstance(m, AssistantMessage)]
    reconciliation = next((m for m in turn.messages if isinstance(m, ReconciliationMessage)), None)
    usage = turn.token_usage
    if usage is None:
        return

    # Group generations by message_id (first-seen order). A missing id disables
    # grouping → distribution is skipped (bail path); the cost still reconciles.
    groups: dict[str, list[AssistantMessage]] = {}
    order: list[str] = []
    for message in assistants:
        if message.message_id is None:
            groups, order = {}, []
            break
        if message.message_id not in groups:
            groups[message.message_id] = []
            order.append(message.message_id)
        groups[message.message_id].append(message)

    distributed = False
    if order:
        gen_outputs = [sum(m.output_tokens for m in groups[mid]) for mid in order]
        matched: list[tuple[str, ProviderCallCost]] = []
        gi = 0
        for call in calls:
            if gi < len(order) and (call.output_tokens or 0) == gen_outputs[gi]:
                matched.append((order[gi], call))
                gi += 1
        if gi == len(order):
            # Clean bind → proxy-authoritative for this turn.
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
            # Recompute the turn totals from the SAME calls the generations now
            # carry, so Σ(message buckets) == token_usage EXACTLY (residual zero).
            usage.uncached_input_tokens = sum(
                max(0, (c.input_tokens or 0) - (c.cache_read_tokens or 0) - (c.cache_write_tokens or 0)) for c in calls
            )
            usage.cache_read_input_tokens = sum(c.cache_read_tokens or 0 for c in calls)
            usage.cache_creation_input_tokens = sum(c.cache_write_tokens or 0 for c in calls)
            usage.output_tokens = sum(c.output_tokens or 0 for c in calls)
            distributed = True

    if reconciliation is None:
        return
    if distributed:
        # Residual is zero by construction (usage recomputed from the same calls);
        # set it explicitly as a plain subtraction — never max(0, …)-clamped, which
        # would silently destroy the invariant when the two sources disagreed.
        reconciliation.input_tokens = usage.uncached_input_tokens - sum(m.input_tokens for m in assistants)
        reconciliation.cache_read_tokens = usage.cache_read_input_tokens - sum(m.cache_read_tokens for m in assistants)
        reconciliation.cache_creation_tokens = usage.cache_creation_input_tokens - sum(
            m.cache_creation_tokens for m in assistants
        )
        reconciliation.output_tokens = usage.output_tokens - sum(m.output_tokens for m in assistants)
    # On the bail path the SDK token reconciliation (set by EventCollector) is left
    # untouched — the invariant already holds there. Cost is attributed either way:
    # zero residual on the clean path, the whole turn cost on the bail path.
    if usage.total_cost_usd is not None:
        reconciliation.cost_usd = usage.total_cost_usd - sum(m.cost_usd or 0.0 for m in assistants)
    else:
        reconciliation.cost_usd = None
