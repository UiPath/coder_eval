"""Join proxy-captured ACTUAL per-call cost/cache onto a run's turns.

For the open-weight (LiteLLM) backend the Claude binary's Anthropic transport
drops OpenRouter's real ``usage.cost`` + per-call cache before Python can see it,
so a proxy-side callback (``litellm/cost_logger.py``) writes one JSONL record per
call to ``LITELLM_COST_LOG``. This module reads those records back and joins them
onto the matching turns at the TURN level:

* the turn's ``token_usage.total_cost_usd`` is overridden with the SUM of its
  calls' real cost (the bill), replacing the static rate-card estimate;
* the per-call breakdown is attached as ``TurnRecord.provider_call_costs`` — a
  deterministic audit record (one row per real proxy call, with its real cost +
  cache buckets + provider) that the evalboard renders as a per-call table.

Token buckets are LEFT UNTOUCHED (SDK-authoritative): the join only writes cost,
so the ``EventCollector`` remains the single writer of the message token-bucket
invariant. There is deliberately NO per-generation distribution — matching a proxy
call to a transcript generation has no deterministic key (only positional /
output-token heuristics), so that view lives in the per-call table off
``provider_call_costs`` instead of being guessed onto the message stream.

Coverage. A turn's cost is overridden only when every call that reported usage is
priced. Degenerate calls that report NO usage at all (no cost AND no tokens — seen
occasionally on some providers) are ignored, so one of them can't revert a whole
turn to the static estimate. A call that reports usage but no cost is a genuine
gap: the turn keeps its static estimate (overriding would bill it at $0), no
breakdown is attached, and a warning names the unpriced ids. A turn with no
matching record keeps its static estimate too.

Retry safety: multiple ``TurnRecord``s can share an ``iteration`` (a crashed
attempt + its retry), and both attempts' proxy calls carry that iteration tag. An
iteration's calls are credited to a single survivor turn (the last with that
iteration THAT HAS GENERATIONS); earlier siblings are zeroed. Crucially the
credit/zero decision is made TOGETHER: a sibling is only zeroed when the survivor
is actually credited with real cost — if the survivor falls back to static, the
sibling keeps its static estimate too, so the iteration's spend is never dropped.

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

from coder_eval.models import AssistantMessage, ProviderCallCost, TokenUsage


if TYPE_CHECKING:
    from coder_eval.models import EvaluationResult, TurnRecord


logger = logging.getLogger(__name__)


def load_cost_records(path: str | Path) -> list[dict[str, Any]]:
    """Read the proxy's per-call JSONL cost log; tolerant of a missing file,
    blank lines, and partial/garbled lines (returns only well-formed dict rows).

    Streamed line-by-line rather than slurped whole, so a long-lived shared log is
    not loaded into memory as one giant string. NOTE the log is append-only and, by
    default (see ``start-litellm.sh``), shared across runs — it is not rotated here,
    so each task's join re-reads the file; scope ``LITELLM_COST_LOG`` to a per-run
    path (or rotate it) if it grows large. ``apply_actual_cost`` filters to the
    current run/task/attempt, so stale rows are ignored, only re-read.
    """
    p = Path(path)
    if not p.is_file():
        return []
    records: list[dict[str, Any]] = []
    with p.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
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
        provider=record.get("provider"),
        cost_usd=record.get("cost"),
        input_tokens=record.get("input"),
        cache_read_tokens=record.get("cache_read"),
        cache_write_tokens=record.get("cache_write"),
        output_tokens=record.get("output"),
    )


def _reports_no_usage(call: ProviderCallCost) -> bool:
    """A degenerate call: no cost AND no tokens at all (a null/empty record some
    providers occasionally emit). Ignored so it can't block an otherwise-priced turn."""
    return call.cost_usd is None and not any(
        (call.input_tokens, call.cache_read_tokens, call.cache_write_tokens, call.output_tokens)
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
            not re-match, and double-count, a prior attempt's rows. ``None`` disables
            the check (records without the tag still join).
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
    matched_iterations: set[str] = set()
    creditable: dict[str, list[ProviderCallCost]] = {}  # iteration -> usable calls (only when fully priced)
    for iteration, recs in by_iteration.items():
        matched_iterations.add(iteration)
        calls = [_to_call(record) for record in recs]
        usable = [c for c in calls if not _reports_no_usage(c)]  # drop degenerate no-usage calls
        unpriced = [c.call_id for c in usable if c.cost_usd is None]
        if usable and not unpriced:
            creditable[iteration] = usable
        else:
            reason = (
                f"{len(unpriced)}/{len(usable)} call(s) report usage but no cost {unpriced}"
                if usable
                else "no usable call"
            )
            logger.warning(
                "LiteLLM actual-cost: iteration=%s not fully priced (%s); keeping the static estimate",
                iteration,
                reason,
            )

    # Phase 2 — MUTATE. Only iterations that are creditable touch anything; a
    # sibling is zeroed ONLY when its survivor is credited (so an un-creditable
    # iteration leaves BOTH the survivor and its crashed sibling on static — the
    # spend is never dropped).
    applied = 0
    for i, turn in enumerate(turns):
        iteration = str(turn.iteration)
        calls = creditable.get(iteration)
        if calls is None:
            continue
        if survivor_index[iteration] != i:
            if turn.token_usage is not None:
                turn.token_usage.total_cost_usd = 0.0  # credited to the survivor instead
            continue
        turn.provider_call_costs = calls
        total = sum(c.cost_usd for c in calls if c.cost_usd is not None)
        if turn.token_usage is None:
            turn.token_usage = TokenUsage(total_cost_usd=total)
        else:
            turn.token_usage.total_cost_usd = total
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
