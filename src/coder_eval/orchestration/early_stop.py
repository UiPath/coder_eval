"""Early-stop-on-criterion: resolution-time validation + runtime watcher.

Opt-in. Arming lives ENTIRELY on the criterion — there is no run-level master
switch. A ``stop_early:`` block on a ``LiveSuccessCriterion`` (so arming an
unobservable criterion is unrepresentable) alone activates the run's watcher;
``run_limits.stop_early: false`` is the run-level KILL SWITCH, and
``stop_early: true`` — the removed master arm — is rejected at resolution.

* ``stop_early: {}`` — the block's PRESENCE is the arming, carrying one implicit
  trigger: a native live FAIL may end the run.
* ``stop_early: {on_pass: stop}`` — a live PASS may also end it.
* ``stop_early: {decide_within: N}`` — still undecided after N tool-call steps
  latches an effective FAIL, fed through the same rule.

A trigger whose polarity an instance can never decide is INERT BY DESIGN.

This module owns the whole feature: ``validate_early_stop`` (resolution-time
guardrails) and ``EarlyStopWatcher`` (the runtime ``StreamCallback``).

Rationale: .claude/notes/orchestration.md § Early stop on criterion
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from coder_eval.models import (
    DEFAULT_STOP_EARLY_GATE_THRESHOLD,
    EarlyStopInfo,
    EarlyStopReason,
    LivePolarity,
    LiveSuccessCriterion,
    StopEarlyPolicy,
)
from coder_eval.streaming.collector import EventCollector
from coder_eval.streaming.events import (
    AgentStartEvent,
    StreamEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
    TurnStartEvent,
)


if TYPE_CHECKING:
    from coder_eval.criteria.base import BaseCriterion, LiveVerdict
    from coder_eval.models import CommandTelemetry, TaskDefinition

    # In the TYPE_CHECKING block (only lazy annotations reference it), so the
    # names are real references rather than strings analyzers cannot resolve.
    _ArmedPair = tuple[LiveSuccessCriterion, BaseCriterion[Any]]


logger = logging.getLogger(__name__)


class EarlyStopConfigError(ValueError):
    """Raised when a task arms early-stop in a way v1 cannot honor.

    Subclasses ``ValueError`` so the run path's resolve -> ``typer.BadParameter``
    conversion covers it transparently; the ``plan`` command catches this
    subclass specifically to flip its exit code (generic per-variant resolution
    failures intentionally stay soft).
    """


def early_stop_active(task: TaskDefinition) -> bool:
    """True iff this run should build a watcher: >= 1 armed criterion, kill switch not thrown.

    The single arming predicate the orchestrator consults. Deliberately ignores
    ``run_limits.stop_early is True`` (the removed master arm) — that value is
    rejected by ``validate_early_stop``, which every path runs before watcher
    creation, so it can never reach a live run.
    """
    limits = task.run_limits
    if limits is not None and limits.stop_early is False:
        return False
    return any(c.is_stop_armed for c in task.success_criteria)


def validate_early_stop(task: TaskDefinition) -> None:
    """Validate an armed early-stop task at resolution time; no-op when unarmed.

    Called after the config layers have merged, and defensively in
    ``Orchestrator._setup``. ``run_limits.stop_early: true`` is always rejected;
    everything else is skipped unless the task is actually armed, so default runs
    — and runs force-disarmed by the kill switch — are entirely unaffected.

    RAISE ORDER matters for which error a multiply-invalid task reports first:

      1. ``run_limits.stop_early: true`` (master arm removed)
      2. armed together with ``simulation.enabled``
      3. agent does not declare ``supports_cooperative_stop``
      4. degenerate ``stop_early_gate_threshold`` (``<= 0.0``)

    There are deliberately NO per-instance polarity guards, and no armed-but-empty
    guard.

    Rationale: .claude/notes/orchestration.md § Inert triggers are by design, and the watcher fails open

    Raises:
        EarlyStopConfigError: on any unsupported armed configuration.
    """
    limits = task.run_limits
    # (1) The master arm no longer exists; arming moved onto the criteria. A
    # hard error (not a deprecation no-op) because a task author writing
    # `stop_early: true` expects arming to happen — silently ignoring it would
    # run the full task and gate it differently than they intended.
    if limits is not None and limits.stop_early is True:
        raise EarlyStopConfigError(
            "run_limits.stop_early: true has been removed — arming is per-criterion now. "
            + "Put a stop_early: block on the live-observable criterion instead "
            + "(e.g. stop_early: {} / {on_pass: stop} / {decide_within: N}); "
            + "run_limits.stop_early: false remains available as the run-level kill switch."
        )

    if not early_stop_active(task):
        return

    # (2) Simulation/dialog mode has its own criteria-driven stop.
    if task.simulation is not None and task.simulation.enabled:
        raise EarlyStopConfigError(
            "criterion-level stop_early arming is not supported together with simulation.enabled "
            + "(early-stop v1 is single-shot only); use simulation.stop_on_criteria_pass "
            + "for dialog-mode criteria stopping, or disarm with run_limits.stop_early: false."
        )

    # (3) The agent must honor the cooperative interrupt. Lazily import the
    # registry + plugin loader so this module stays free of runtime coder_eval
    # imports at load time.
    from coder_eval.agents.registry import AgentRegistry
    from coder_eval.plugins import ensure_plugins_loaded

    ensure_plugins_loaded()
    agent_type = str(task.agent.type) if task.agent is not None and task.agent.type is not None else None
    if agent_type is None:
        # Distinct from an unregistered type: there is no agent block at all,
        # so pointing at plugin loading would send the user the wrong way.
        raise EarlyStopConfigError(
            "criterion-level stop_early arming requires an agent block with a registered type; "
            + "this task resolves without one. "
            + "Disarm with run_limits.stop_early: false to bypass this check."
        )
    registration = AgentRegistry.get(agent_type)
    if registration is None:
        # Not the same failure as an agent that opted out of cooperative stop:
        # an unregistered type usually means a plugin is not installed/loaded.
        raise EarlyStopConfigError(
            f"criterion-level stop_early arming requires a registered agent type; {agent_type!r} is "
            + "not registered (is the providing plugin installed and loaded?). "
            + "Disarm with run_limits.stop_early: false to bypass this check."
        )
    if not registration.agent_class.supports_cooperative_stop:
        supporting = ", ".join(
            kind
            for kind in AgentRegistry.list_kinds()
            if (reg := AgentRegistry.get(kind)) is not None and reg.agent_class.supports_cooperative_stop
        )
        raise EarlyStopConfigError(
            "criterion-level stop_early arming requires an agent that supports cooperative stopping "
            + f"({supporting}); agent type {agent_type!r} does not. "
            + "Disarm with run_limits.stop_early: false to run this agent anyway."
        )

    # (4) A threshold of exactly 0 trivially satisfies both the pass-stop floor and
    # the final weighted gate however the armed criteria decided, neutralizing the
    # gate with one YAML line. Checked here, not on RunLimits: an
    # EarlyStopConfigError flips the plan exit code and aborts run, where a plain
    # ValueError lands in the CLI's generic "resolution failed" branch, which prints
    # red text but does not flip the exit code.
    # Rationale: .claude/notes/orchestration.md § Early stop on criterion
    if limits is not None and limits.stop_early_gate_threshold <= 0.0:
        raise EarlyStopConfigError(
            f"run_limits.stop_early_gate_threshold ({limits.stop_early_gate_threshold}) must be "
            + "> 0.0 on an armed task (a threshold of 0 trivially passes the armed gate "
            + "regardless of whether any armed criterion actually decided)."
        )


class EarlyStopWatcher:
    """Observes the agent event stream and trips the cooperative interrupt.

    A ``StreamCallback`` composed into the agent's callback chain. It maintains its
    OWN ``EventCollector``, so each ``live_verdict`` sees a fresh single-element
    partial trajectory. On every tool call it evaluates the armed criteria still
    undecided and applies the stop rule; once a stop fires the decision is LATCHED
    and further events are ignored.

    The orchestrator polls ``should_stop`` and afterwards reads ``info``.

    FAIL-OPEN: a raising ``live_verdict`` disarms the watcher and degrades to a
    full run, which can never produce a FALSE early stop.

    Rationale: .claude/notes/orchestration.md § Inert triggers are by design, and the watcher fails open
    """

    def __init__(
        self,
        task_id: str,
        armed: list[_ArmedPair],
        *,
        max_turns: int | None,
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
                raise ValueError(f"criterion {criterion.type!r} passed to EarlyStopWatcher without a stop_early block")
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
        if not any(self._pass_trigger) and not any(self._fail_trigger) and all(b is None for b in self._budget):
            # Legal for a fanned row whose armed lines are all inert for its role,
            # but on a non-fanned task this is dead config.
            logger.warning("[%s] all armed stop triggers are inert for this row; run cannot stop early", task_id)
        self._max_turns = max_turns
        self._collector = EventCollector()
        self._sdk_turn_index = 0
        self._tool_call_index = 0
        self._started_monotonic: float | None = None
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
        self._disarmed = False

    @classmethod
    def for_task(cls, task: TaskDefinition) -> EarlyStopWatcher:
        """Build a watcher for an armed task (instantiates the armed criteria's checkers).

        The criteria registry is imported lazily here — it is not initialized at
        module import time. Checker classes take no ctor args. Only
        ``LiveSuccessCriterion`` instances can be armed (the trigger fields
        exist nowhere else), so the ``isinstance`` filter is a pyright
        narrowing aid, not a behavioral guard.
        """
        from coder_eval.criteria import CriterionRegistry, init_criteria

        init_criteria(validate=False)
        armed: list[_ArmedPair] = [
            (c, CriterionRegistry.get_checker(c.type)())
            for c in task.success_criteria
            if isinstance(c, LiveSuccessCriterion) and c.is_stop_armed
        ]
        max_turns = task.run_limits.max_turns if task.run_limits is not None else None
        gate_threshold = (
            task.run_limits.stop_early_gate_threshold
            if task.run_limits is not None
            else DEFAULT_STOP_EARLY_GATE_THRESHOLD
        )
        return cls(task.task_id, armed, max_turns=max_turns, gate_threshold=gate_threshold)

    def on_event(self, event: StreamEvent) -> None:
        """Fail-open wrapper around ``_on_event_impl``: any unexpected exception
        anywhere in the round — the collector reduction included, not just the
        verdict-collection loop — disarms the watcher and degrades to a full
        run. The agent-side ``safe_emit`` swallows callback exceptions, so
        without disarming here a raising collector would leave the watcher
        silently evaluating a corrupted partial trajectory on every subsequent
        event with ``_disarmed`` still False.
        """
        if self._info is not None or self._disarmed:
            return
        try:
            self._on_event_impl(event)
        except Exception:
            self._disarmed = True
            logger.error(
                "[%s] early-stop event handling raised unexpectedly; disarming watcher, run degrades to a full run",
                self._task_id,
                exc_info=True,
            )

    def _on_event_impl(self, event: StreamEvent) -> None:
        """Forward the event to the internal collector; evaluate on each tool call.

        Counts ``TurnStartEvent`` for ``sdk_turn_index`` and each dispatched call
        for the 1-based ``tool_call_index``, stamping the wall-clock origin at the
        FIRST ``AgentStartEvent`` only, so a retry does not reset it.

        The decision is evaluated on the tool CALL. It is not in the collector yet
        (which reduces commands from ``ToolEndEvent``), so it is passed in as the
        in-flight command; ``tool_call_index`` increments on the resolved end, so
        it stays a count of COMPLETED calls.

        UNRESOLVED tool ends are RECORDED but never counted or evaluated on — they
        must still land in the collector, or the watcher would reduce a strictly
        smaller command set than the authoritative check.

        Rationale: .claude/notes/orchestration.md § Verdicts latch, and the decision happens on the CALL
        """
        if isinstance(event, AgentStartEvent):
            if self._started_monotonic is None:
                self._started_monotonic = time.monotonic()
        elif isinstance(event, TurnStartEvent):
            self._sdk_turn_index += 1
        elif isinstance(event, ToolStartEvent):
            # Decide on the call, evaluating with it appended as the in-flight
            # command (it has no ToolEnd to count yet, so report it as +1).
            self._evaluate_impl(in_flight=event.tool)
            return
        elif isinstance(event, ToolEndEvent):
            if event.status == ToolEndStatus.UNRESOLVED:
                # Trajectory parity with the agent's collector: record, but do
                # not count a round or evaluate.
                self._collector.on_event(event)
                return
            self._tool_call_index += 1
            self._collector.on_event(event)
            self._evaluate_impl()
            return
        self._collector.on_event(event)

    def should_stop(self) -> bool:
        """The cooperative interrupt the agent polls after each dispatched message."""
        return self._info is not None

    @property
    def info(self) -> EarlyStopInfo | None:
        """The recorded stop info, or ``None`` if no stop fired (incl. after disarm)."""
        return self._info

    @property
    def disarmed(self) -> bool:
        """True once a ``live_verdict`` raised and the watcher degraded to a full run."""
        return self._disarmed

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
        turns_remaining = None if self._max_turns is None else max(self._max_turns - self._sdk_turn_index, 0)
        self._info = EarlyStopInfo(
            reason=reason,
            deciding_criterion_type=criterion.type,
            deciding_criterion_description=criterion.description,
            armed_criteria=[f"{c.type}: {c.description}" for c, _ in self._armed],
            sdk_turn_index=self._sdk_turn_index,
            tool_call_index=tool_call_index,
            elapsed_seconds=elapsed,
            turns_remaining_at_stop=turns_remaining,
            gate_threshold=self._gate_threshold,
        )
        logger.info(
            "[%s] early-stop fired: reason=%s deciding=%s sdk_turn=%d tool_call=%d elapsed=%.2fs",
            self._task_id,
            reason.value,
            criterion.type,
            self._sdk_turn_index,
            tool_call_index,
            elapsed,
        )
