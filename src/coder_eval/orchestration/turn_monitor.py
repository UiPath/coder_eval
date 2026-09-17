"""The run's single ``should_stop`` answerer: armed early stop, the tool-call and model-turn caps and the budgets.

``TurnMonitor`` is a ``StreamCallback`` composed into the agent's callback chain
for the whole task. It owns ONE ``EventCollector`` across every retry attempt and
every dialog turn, so every count it answers from is cumulative per task. The agent
polls ``should_stop`` at its safe boundaries; the first non-None ``StopReason`` is
latched and final.

Precedence on one round: ``EARLY_CRITERION``, ``TOOL_CALL_CAP``, ``MODEL_TURN_CAP``,
``TOKEN_BUDGET``, ``USD_BUDGET``. A budget breach seen mid-turn latches its reason and its figures, so a
turn that stopped on a budget always finalizes as that budget's status.

FAIL-OPEN covers the armed criteria only: any exception while reducing an event or
evaluating them disarms them and the run degrades to a full run. The caps read
counters, so they never disarm.

Rationale: .claude/notes/orchestration.md § Early stop on criterion
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from coder_eval.errors import BudgetExceededError, BudgetUnenforceableError
from coder_eval.models import (
    DEFAULT_STOP_EARLY_GATE_THRESHOLD,
    EarlyStopInfo,
    EarlyStopReason,
    LivePolarity,
    LiveSuccessCriterion,
    RunLimits,
    StopEarlyPolicy,
    TokenUsage,
)
from coder_eval.orchestration.early_stop import early_stop_active
from coder_eval.pricing import price_turn
from coder_eval.streaming.collector import EventCollector
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentStartEvent,
    StopReason,
    StreamEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
    TurnEndEvent,
    TurnStartEvent,
)


if TYPE_CHECKING:
    from coder_eval.criteria.base import BaseCriterion, LiveVerdict
    from coder_eval.models import CommandTelemetry, TaskDefinition

    _ArmedPair = tuple[LiveSuccessCriterion, BaseCriterion[Any]]


logger = logging.getLogger(__name__)

_DISARMED = "armed criteria disarmed, run degrades to a full run"


def _reports_cost(task: TaskDefinition) -> bool:
    from coder_eval.agents.registry import AgentRegistry
    from coder_eval.plugins import ensure_plugins_loaded

    if task.agent is None or task.agent.type is None:
        return False
    ensure_plugins_loaded()
    registration = AgentRegistry.get(str(task.agent.type))
    return registration is not None and registration.agent_class.contract.reports_cost


class TurnMonitor:
    """Observes the agent event stream and answers the cooperative ``should_stop`` poll.

    The orchestrator builds one per task, hands the same instance to every
    ``communicate()`` call, polls nothing itself, and afterwards reads
    ``stop_reason`` and ``info``.

    Rationale: .claude/notes/orchestration.md § Inert triggers are by design, and the watcher fails open
    """

    def __init__(
        self,
        task_id: str,
        armed: list[_ArmedPair],
        *,
        limits: RunLimits | None,
        model: str | None = None,
        reports_cost: bool = False,
        gate_threshold: float = DEFAULT_STOP_EARLY_GATE_THRESHOLD,
    ) -> None:
        self._task_id = task_id
        self._armed = armed
        self._gate_threshold = gate_threshold
        self._armed_weight = sum(c.weight for c, _ in armed)
        # Per-instance decidable polarities, aligned with `_armed`, static for the
        # run. A trigger whose polarity the instance cannot decide is inert.
        self._decidable: list[frozenset[LivePolarity]] = [
            criterion.live_decidable_polarities() for criterion, _checker in armed
        ]
        # Effective per-instance triggers (inert ones already resolved away).
        # Every armed pair carries a stop_early block by construction
        # (is_stop_armed == block presence); an explicit raise (not an assert,
        # which -O strips) keeps a blockless pair from slipping through.
        blocks: list[StopEarlyPolicy] = []
        for criterion, _checker in armed:
            if criterion.stop_early is None:
                raise ValueError(f"criterion {criterion.type!r} passed to TurnMonitor without a stop_early block")
            blocks.append(criterion.stop_early)
        self._pass_trigger: list[bool] = [
            block.on_pass == "stop" and "pass" in pol for block, pol in zip(blocks, self._decidable, strict=True)
        ]
        # IMPLICIT in arming: an armed criterion's native live-fail may always
        # stop the run (ceiling-gated). Inert when it cannot live-fail.
        self._fail_trigger: list[bool] = ["fail" in pol for pol in self._decidable]
        # A timeout only means anything for an instance waiting to observe a
        # PASS: a fail-only instance's 'undecided' IS its success state.
        self._budget: list[int | None] = [
            block.decide_within if "pass" in pol else None for block, pol in zip(blocks, self._decidable, strict=True)
        ]
        if (
            armed
            and not any(self._pass_trigger)
            and not any(self._fail_trigger)
            and all(b is None for b in self._budget)
        ):
            # Legal for a fanned row whose armed lines are all inert for its role,
            # but on a non-fanned task this is dead config.
            logger.warning("[%s] all armed stop triggers are inert for this row; run cannot stop early", task_id)
        self._limits = limits
        self._model = model
        self._start_model: str | None = None
        self._reported_model: str | None = None
        self._collector = EventCollector()
        self._resolved_tool_ids: set[str] = set()
        self._sdk_turn_index = 0
        self._call_turn_ids: set[str] = set()
        self._tool_call_index = 0
        self._started_monotonic: float | None = None
        self._committed = TokenUsage()
        self._committed_cost = 0.0
        self._unpriced_turn = False
        self._reports_cost = reports_cost
        self._unpriced_in_flight = False
        self._in_flight = TokenUsage()
        self._budget_breach: tuple[str, float, float] | None = None
        # Once an entry leaves "undecided" on a RESOLVED round its checker is
        # never polled again. `_budget_expired` marks a latched fail as
        # timeout-driven, reported as DECISION_BUDGET_EXCEEDED.
        # Rationale: .claude/notes/orchestration.md § Verdicts latch, and the decision happens on the CALL
        self._latched: list[LiveVerdict] = ["undecided"] * len(armed)
        self._budget_expired: list[bool] = [False] * len(armed)
        # For the "which criterion flipped to pass" attribution. Reassigned ONLY
        # at the end of a non-firing evaluation, so it always holds the PREVIOUS
        # round when a stop fires.
        self._prev_verdicts: list[LiveVerdict] = ["undecided"] * len(armed)
        self._info: EarlyStopInfo | None = None
        self._stop_reason: StopReason | None = None
        self._disarmed = False

    @classmethod
    def for_task(cls, task: TaskDefinition, *, arm: bool) -> TurnMonitor:
        """Build the task's monitor; its criteria are armed only when ``arm`` and the task arms any.

        ``arm`` is the grading switch: under ``execute`` the trajectory is the
        deliverable, so no criterion may truncate it, but the cap still applies.
        The criteria registry is imported lazily; only ``LiveSuccessCriterion``
        instances can be armed, so the ``isinstance`` filter only narrows types.
        """
        armed: list[_ArmedPair] = []
        if arm and early_stop_active(task):
            from coder_eval.criteria import CriterionRegistry, init_criteria

            init_criteria(validate=False)
            armed = [
                (c, CriterionRegistry.get_checker(c.type)())
                for c in task.success_criteria
                if isinstance(c, LiveSuccessCriterion) and c.is_stop_armed
            ]
        limits = task.run_limits
        gate_threshold = limits.stop_early_gate_threshold if limits is not None else DEFAULT_STOP_EARLY_GATE_THRESHOLD
        model = task.agent.model if task.agent is not None else None
        return cls(
            task.task_id,
            armed,
            limits=limits,
            model=model,
            reports_cost=_reports_cost(task),
            gate_threshold=gate_threshold,
        )

    def on_event(self, event: StreamEvent) -> None:
        """Reduce one event; an unexpected exception disarms the criteria and never stops the counters."""
        try:
            self._on_event_impl(event)
        except Exception:
            self._disarmed = True
            logger.error("[%s] turn monitor event handling raised; %s", self._task_id, _DISARMED, exc_info=True)

    def _on_event_impl(self, event: StreamEvent) -> None:
        """Forward the event to the collector, count, and evaluate the stop conditions.

        The armed criteria are evaluated on the tool CALL, with the call passed in
        as the in-flight command, and again on its resolved end. ``tool_call_index``
        increments on each resolved end. UNRESOLVED tool ends are RECORDED but never
        counted or evaluated on — they must still land in the collector, or the
        monitor would reduce a strictly smaller command set than the authoritative
        check. The cap counts distinct resolved tool ids. A main-thread turn start counts once
        per turn id per ``communicate()``; the model-turn cap latches when turn N+1 starts.

        A nested (sub-agent) event is main-thread-scoped out: its tool end is recorded
        but never counted or evaluated, its turn start sets no model, and only its
        turn-end tokens count, toward the budgets.

        Rationale: .claude/notes/orchestration.md § Verdicts latch, and the decision happens on the CALL
        """
        if event.parent_thread_id is not None:
            self._on_nested_event(event)
            return
        if isinstance(event, AgentStartEvent):
            if self._started_monotonic is None:
                self._started_monotonic = time.monotonic()
            self._in_flight = TokenUsage()
            self._start_model = event.model or self._start_model
            self._call_turn_ids = set()
        elif isinstance(event, TurnStartEvent):
            if event.turn_id not in self._call_turn_ids:
                self._call_turn_ids.add(event.turn_id)
                self._sdk_turn_index += 1
                self._evaluate_model_turn_cap()
            self._reported_model = event.model or self._reported_model
        elif isinstance(event, TurnEndEvent):
            if event.tokens is not None:
                self._in_flight += event.tokens
                self._evaluate_budgets()
        elif isinstance(event, AgentEndEvent):
            self._reported_model = event.model_used or self._reported_model
            self._commit(event.usage)
            self._evaluate_budgets()
        elif isinstance(event, ToolStartEvent):
            self._evaluate_armed(in_flight=event.tool)
            return
        elif isinstance(event, ToolEndEvent):
            self._collector.on_event(event)
            if event.status == ToolEndStatus.UNRESOLVED:
                return
            self._tool_call_index += 1
            self._resolved_tool_ids.add(event.tool.tool_id)
            self._evaluate_armed()
            self._evaluate_cap()
            return
        self._collector.on_event(event)

    def _on_nested_event(self, event: StreamEvent) -> None:
        if isinstance(event, ToolEndEvent):
            self._collector.on_event(event)
        elif isinstance(event, TurnEndEvent) and event.tokens is not None:
            self._in_flight += event.tokens
            self._evaluate_budgets()

    def should_stop(self) -> StopReason | None:
        """The cooperative poll the agent calls at each safe boundary."""
        return self._stop_reason

    @property
    def stop_reason(self) -> StopReason | None:
        """The latched reason, or ``None`` while nothing asked the agent to stop."""
        return self._stop_reason

    @property
    def info(self) -> EarlyStopInfo | None:
        """The early-stop record; set only when the latched reason is ``EARLY_CRITERION``."""
        return self._info

    @property
    def armed(self) -> bool:
        """True when the task armed at least one criterion for this run."""
        return bool(self._armed)

    @property
    def disarmed(self) -> bool:
        """True once event handling raised and the armed criteria degraded to a full run."""
        return self._disarmed

    @property
    def tool_calls(self) -> int:
        """Distinct resolved tool calls across the whole task."""
        return len(self._resolved_tool_ids)

    @property
    def model_turns(self) -> int:
        """Main-thread model turns started across the whole task, each turn id once per communicate()."""
        return self._sdk_turn_index

    @property
    def usage(self) -> TokenUsage:
        """Committed usage from every finished ``communicate()`` plus the in-flight deltas."""
        return self._committed + self._in_flight

    def cost_usd(self) -> float | None:
        """Cumulative USD: every finished turn priced on its own, plus the priceable in-flight deltas.

        A turn is priced by ``pricing.price_turn`` with the models ``agent.model``, the
        model the agent resolved at start, and the last model a message reported, in
        that order; a turn with no usage and no reported cost costs 0.
        ``None`` once any finished turn could be priced none of these ways.
        """
        if self._unpriced_turn:
            return None
        return self._committed_cost + (self._price(self._in_flight) or 0.0)

    def raise_if_over_budget(self, *, iteration: int) -> None:
        """Raise when the task is over a budget, or its ``max_usd`` could not be enforced.

        Raises:
            BudgetExceededError: a budget reason latched mid-turn (with the figures
                from that moment), or the finished turns' totals breach a budget.
            BudgetUnenforceableError: ``max_usd`` is set and a finished turn was unpriceable,
                or in-flight usage was unpriceable on a harness that does not report cost.
        """
        breach = self._budget_breach if self._budget_breach is not None else self._breach()
        if breach is not None:
            name, actual, limit = breach
            raise BudgetExceededError(name, actual=actual, limit=limit, task_id=self._task_id, iteration=iteration)
        if (
            self._limits is not None
            and self._limits.max_usd is not None
            and (self._unpriced_turn or self._unpriced_in_flight)
        ):
            raise BudgetUnenforceableError(
                "run_limits.max_usd could not be enforced: the harness reported no cost and "
                + f"agent.model {self._model!r} (reported {self._reported_model!r}) has no rate in "
                + "coder_eval.pricing (register one with "
                + "register_pricing, pin a priced model, or remove max_usd)"
            )

    def _commit(self, usage: TokenUsage) -> None:
        self._committed += usage
        cost = self._price(usage)
        if cost is None:
            self._unpriced_turn = True
        else:
            self._committed_cost += cost
        self._in_flight = TokenUsage()

    def _price(self, usage: TokenUsage) -> float | None:
        if usage.is_empty():
            return price_turn(usage, ()) or 0.0
        return price_turn(usage, (self._model, self._start_model, self._reported_model))

    def _breach(self) -> tuple[str, float, float] | None:
        """The first budget over its cap as ``(budget name, actual, limit)``: input, output, total, usd."""
        limits = self._limits
        if limits is None:
            return None
        input_tokens, output_tokens, total_tokens = limits.budgeted_tokens(self.usage)
        for name, actual, limit in (
            ("input_tokens", input_tokens, limits.max_input_tokens),
            ("output_tokens", output_tokens, limits.max_output_tokens),
            ("total_tokens", total_tokens, limits.max_total_tokens),
        ):
            if limit is not None and actual > limit:
                return name, actual, limit
        cost = self.cost_usd() if limits.max_usd is not None else None
        if limits.max_usd is not None and cost is not None and cost > limits.max_usd:
            return "usd", cost, limits.max_usd
        return None

    def _evaluate_budgets(self) -> None:
        if self._stop_reason is not None:
            return
        breach = self._breach()
        if breach is None:
            self._evaluate_in_flight_priceable()
            return
        self._budget_breach = breach
        name, actual, limit = breach
        logger.info("[%s] %s budget reached: %g > %g", self._task_id, name, actual, limit)
        self._latch(StopReason.USD_BUDGET if name == "usd" else StopReason.TOKEN_BUDGET)

    def _evaluate_in_flight_priceable(self) -> None:
        """Latch ``USD_BUDGET`` when ``max_usd`` is set and in-flight usage has no price.

        A harness that reports cost prices the turn at its end, so its in-flight usage is exempt.
        """
        if self._limits is None or self._limits.max_usd is None or self._reports_cost:
            return
        if self._in_flight.is_empty() or self._price(self._in_flight) is not None:
            return
        self._unpriced_in_flight = True
        logger.error("[%s] usd budget cannot be enforced: the in-flight usage has no price", self._task_id)
        self._latch(StopReason.USD_BUDGET)

    def _latch(self, reason: StopReason) -> None:
        if self._stop_reason is None:
            self._stop_reason = reason

    def _evaluate_armed(self, in_flight: CommandTelemetry | None = None) -> None:
        if not self._armed or self._disarmed or self._stop_reason is not None:
            return
        try:
            self._evaluate_impl(in_flight)
        except Exception:
            self._disarmed = True
            logger.error("[%s] early-stop evaluation raised; %s", self._task_id, _DISARMED, exc_info=True)

    def _evaluate_cap(self) -> None:
        cap = self._limits.max_tool_calls if self._limits is not None else None
        if cap is not None and self.tool_calls >= cap:
            if self._stop_reason is None:
                logger.info(
                    "[%s] tool-call cap reached: %d resolved tool calls (cap %d)", self._task_id, self.tool_calls, cap
                )
            self._latch(StopReason.TOOL_CALL_CAP)

    def _evaluate_model_turn_cap(self) -> None:
        cap = self._limits.max_turns if self._limits is not None else None
        if cap is not None and self._sdk_turn_index > cap:
            if self._stop_reason is None:
                logger.info(
                    "[%s] model-turn cap reached: model turn %d started (cap %d)",
                    self._task_id,
                    self._sdk_turn_index,
                    cap,
                )
            self._latch(StopReason.MODEL_TURN_CAP)

    def _ceiling(self, verdicts: list[LiveVerdict]) -> float:
        """Best-case weighted score over the WHOLE armed set, given current verdicts.

        Every already-failed criterion (native live-fail or expired budget) is
        pinned at 0 (a monotonic ``live_verdict`` guarantees it stays failed);
        every ``pass`` or still ``undecided`` criterion is credited its full
        weight (the optimistic assumption that it could still end up scoring
        1.0). This is the same weighting ``EvaluationResult.
        armed_criteria_passed`` uses for the real, final gate, so ``ceiling <
        gate_threshold`` means the gate is mathematically guaranteed to fail no
        matter how the trajectory continues.
        """
        if self._armed_weight <= 0.0:
            # Fails closed, as `armed_criteria_passed` does for the same unreachable case.
            return 0.0
        return sum(c.weight for (c, _checker), v in zip(self._armed, verdicts, strict=True) if v != "fail") / (
            self._armed_weight
        )

    def _floor(self, verdicts: list[LiveVerdict], indices: list[int]) -> float | None:
        """Worst-case weighted score over the given armed-index subset, given current verdicts.

        Mirrors ``_ceiling`` for the opposite direction: every still-undecided
        (or already-``fail``) criterion in ``indices`` is credited nothing (the
        pessimistic assumption that it could still end up scoring 0); only an
        already-``pass`` criterion contributes its weight. Returns ``None``
        when the subset's total weight is 0 (the vacuous case — nothing to
        bound), so callers don't have to special-case an empty numerator over
        an empty denominator.
        """
        weight = sum(self._armed[i][0].weight for i in indices)
        if weight <= 0.0:
            return None
        return sum(self._armed[i][0].weight for i in indices if verdicts[i] == "pass") / weight

    def _collect_verdicts(self, in_flight: CommandTelemetry | None, tool_call_index: int) -> list[LiveVerdict]:
        """One round of effective verdicts, latching decided ones on resolved rounds.

        A latched (non-``undecided``) verdict is returned as-is — its checker is
        never polled again (the checkers' monotonicity contract makes re-polling
        pure waste). A fresh ``undecided`` on a pass-capable instance whose
        ``decide_within`` budget has expired becomes an *effective*
        ``fail`` (marked in ``_budget_expired`` for reason attribution).

        Latching only happens on RESOLVED rounds (``in_flight is None``): an
        in-flight round's verdict may fire a stop this round, but is not
        persisted — a dispatched call that never resolves (crashed attempt)
        must not leave a stale verdict behind across retries. The verdict is
        recomputed from the collector's resolved commands on the next round.
        """
        record = self._collector.build_turn_record()
        if in_flight is not None:
            # No ToolEnd yet, so the collector has not captured it. Append and
            # re-sort by sequence to keep the partial trajectory in order.
            record.commands = sorted([*record.commands, in_flight], key=lambda c: c.sequence_number)
        records = [record]
        verdicts: list[LiveVerdict] = []
        for i, (criterion, checker) in enumerate(self._armed):
            if self._latched[i] != "undecided":
                verdicts.append(self._latched[i])
                continue
            try:
                verdict: LiveVerdict = checker.live_verdict(criterion, records)
            except Exception:
                # Log WHICH criterion raised before re-raising to on_event's
                # generic handler, where that context would be lost.
                logger.error(
                    "[%s] early-stop live_verdict raised for criterion %r",
                    self._task_id,
                    criterion.type,
                    exc_info=True,
                )
                raise
            budget = self._budget[i]
            budget_expired = verdict == "undecided" and budget is not None and tool_call_index >= budget
            if budget_expired:
                verdict = "fail"
            if in_flight is None and verdict != "undecided":
                self._latched[i] = verdict
                self._budget_expired[i] = budget_expired
            verdicts.append(verdict)
        return verdicts

    def _budget_drove(self, index: int, verdicts: list[LiveVerdict], tool_call_index: int) -> bool:
        """True when ``index``'s ``fail`` is timeout-driven rather than a native live-fail.

        Reads the persistent ``_budget_expired`` latch when set; for a
        transient (in-flight, not-yet-latched) fail it re-derives: a fail on an
        instance that cannot natively live-fail, with an expired budget, can
        only have come from the timeout. A native live-fail on an instance
        whose budget also happens to be expired reports as a native fail —
        ``_collect_verdicts`` only converts the verdict when the checker itself
        returned ``undecided``.
        """
        if self._budget_expired[index]:
            return True
        budget = self._budget[index]
        return (
            verdicts[index] == "fail"
            and self._latched[index] == "undecided"
            and budget is not None
            and tool_call_index >= budget
            and "fail" not in self._decidable[index]
        )

    def _evaluate_impl(self, in_flight: CommandTelemetry | None = None) -> None:
        # An in-flight call has not been counted by a ToolEnd yet, so report it as
        # the next (1-based) tool call.
        tool_call_index = self._tool_call_index + (1 if in_flight is not None else 0)
        verdicts = self._collect_verdicts(in_flight, tool_call_index)

        # RECALL DEFERRAL: a fail-stop is HELD while any pass-capable armed
        # criterion is still undecided and within budget. A row with zero
        # pass-capable criteria defers nothing.
        # Rationale: .claude/notes/orchestration.md § Precision is traded, recall is not
        pass_capable_undecided = any(
            v == "undecided" and "pass" in pol for v, pol in zip(verdicts, self._decidable, strict=True)
        )

        # A criterion whose effective verdict is "fail" is a CANDIDATE; the stop
        # fires only once the ceiling bound can no longer reach `gate_threshold`.
        # Rationale: .claude/notes/orchestration.md § The ceiling and floor bounds
        if not pass_capable_undecided:
            # Deterministic precedence: a native live-fail candidate always wins
            # over a budget-driven one, so the persisted/telemetry reason cannot
            # flip between CRITERION_FAILED and DECISION_BUDGET_EXCEEDED on a
            # mere reorder of ``success_criteria`` when both resolve on the same
            # round. Within each class, first criteria-order match wins.
            native_fails = [
                i
                for i, v in enumerate(verdicts)
                if v == "fail" and self._fail_trigger[i] and not self._budget_drove(i, verdicts, tool_call_index)
            ]
            budget_fails = [
                i for i, v in enumerate(verdicts) if v == "fail" and self._budget_drove(i, verdicts, tool_call_index)
            ]
            candidate_index = native_fails[0] if native_fails else (budget_fails[0] if budget_fails else None)
            if candidate_index is not None and self._ceiling(verdicts) < self._gate_threshold:
                reason = EarlyStopReason.CRITERION_FAILED if native_fails else EarlyStopReason.DECISION_BUDGET_EXCEEDED
                self._fire(reason, self._armed[candidate_index][0], tool_call_index=tool_call_index)
                return

        # Pass-stop: the on_pass=stop subset's FLOOR already meets
        # ``gate_threshold``. Distractors are excluded from both the numerator and
        # the denominator; no on_pass=stop criteria at all returns None. HELD while
        # any pass-capable armed criterion OUTSIDE the subset is undecided --
        # cutting there would freeze a sibling's expected signal out of the run.
        # Rationale: .claude/notes/orchestration.md § The ceiling and floor bounds
        pass_stop_indices = [i for i, armed_pass in enumerate(self._pass_trigger) if armed_pass]
        outside_pass_capable_undecided = any(
            v == "undecided" and "pass" in pol and not armed_pass
            for v, pol, armed_pass in zip(verdicts, self._decidable, self._pass_trigger, strict=True)
        )
        if not outside_pass_capable_undecided:
            floor = self._floor(verdicts, pass_stop_indices)
            if floor is not None and floor >= self._gate_threshold:
                # Deciding criterion = the last on_pass=stop (criteria order) whose
                # verdict flipped vs the previous round; fall back to the last one.
                deciding = self._armed[pass_stop_indices[-1]][0]
                for i in pass_stop_indices:
                    if verdicts[i] != self._prev_verdicts[i]:
                        deciding = self._armed[i][0]
                self._fire(EarlyStopReason.CRITERION_PASSED, deciding, tool_call_index=tool_call_index)
                return

        # No stop this round — record the verdicts so the next round can detect
        # flips. Resolved rounds only: an in-flight round's verdicts are
        # deliberately not latched (the call may never resolve), so persisting
        # them here would let a transient round mask the real flip attribution.
        if in_flight is None:
            self._prev_verdicts = verdicts

    def _fire(self, reason: EarlyStopReason, criterion: LiveSuccessCriterion, *, tool_call_index: int) -> None:
        elapsed = 0.0
        if self._started_monotonic is not None:
            elapsed = max(time.monotonic() - self._started_monotonic, 0.0)
        cap = self._limits.max_tool_calls if self._limits is not None else None
        self._info = EarlyStopInfo(
            reason=reason,
            deciding_criterion_type=criterion.type,
            deciding_criterion_description=criterion.description,
            armed_criteria=[f"{c.type}: {c.description}" for c, _ in self._armed],
            sdk_turn_index=self._sdk_turn_index,
            tool_call_index=tool_call_index,
            elapsed_seconds=elapsed,
            tool_calls_remaining_at_stop=None if cap is None else max(cap - tool_call_index, 0),
            gate_threshold=self._gate_threshold,
        )
        self._latch(StopReason.EARLY_CRITERION)
        logger.info(
            "[%s] early-stop fired: reason=%s deciding=%s sdk_turn=%d tool_call=%d elapsed=%.2fs",
            self._task_id,
            reason.value,
            criterion.type,
            self._sdk_turn_index,
            tool_call_index,
            elapsed,
        )
