"""Early-stop-on-criterion: resolution-time validation + runtime watcher.

Opt-in mechanism (``run_limits.stop_early``) that ends a single-shot run as soon
as its *armed* criteria (those with ``stop_when`` set) are decided mid-run — on
pass or on a definitive fail. This module owns the whole feature:

* ``validate_early_stop`` — resolution-time guardrails. Rejects every
  configuration v1 cannot honor as a hard error, so an unsupported arming is
  never a silent no-op.
* ``EarlyStopWatcher`` — the runtime observer. A ``StreamCallback`` composed
  into the agent's event stream that maintains its own ``EventCollector``,
  evaluates every armed criterion's ``live_verdict`` on each tool *call* (and on
  its result), applies the stop rule, and exposes ``should_stop()`` (the
  cooperative interrupt the agent polls) plus ``info`` (the ``EarlyStopInfo`` the
  orchestrator records). Fail-open: a raising ``live_verdict`` disarms the
  watcher and degrades to a full run — a verdict bug can never cause a *false*
  early stop.

Deciding on the tool *call* (``ToolStartEvent``), not the result, is what makes
the stop robust: for an observable criterion the verdict is fully determined by
the call's inputs (which skill / which command), so the watcher can latch the
instant the call is dispatched — before a cut-short turn (e.g. a timeout) can
strip the result and leave the call unresolved. The agent polls ``should_stop``
immediately after dispatching each message, so a latch on the call breaks the
loop before the result message is ever pulled.

Live verdicts only *trigger* the stop; the authoritative scores always come
from the standard ``check_all_async`` on the frozen trajectory after the cut.

Weighting: both the stop rule and the post-hoc gate
(``EvaluationResult.armed_criteria_passed``) consult ``run_limits.
stop_early_gate_threshold`` (default ``1.0``) rather than treating every armed
criterion's pass/fail as equally decisive. A fail-stop fires once the armed
subset's CEILING (best case: every still-``undecided``/``pass`` criterion
scores 1.0, every live-``fail``ed one scores 0) can no longer reach the
threshold — i.e. the gate is mathematically guaranteed to fail regardless of
how the trajectory continues. A pass-stop fires once the pass-armed subset's
FLOOR (worst case: every still-undecided one scores 0) already meets it. At the
default threshold of 1.0 both bounds collapse exactly to "any single armed
criterion's live-fail stops the run" / "every pass-armed criterion has
live-passed" — byte-for-byte the pre-weighting behavior, since the only
live-observable criteria score binary 0/1. Lowering the threshold lets a
low-weight armed criterion's failure be absorbed without truncating the run.

Precision trade-off: a pass-stop cuts the run the instant the pass-armed floor
locks in, so a *fail-armed* criterion (e.g. a distractor) that would only
misfire on a LATER tool call is never observed — the frozen trajectory then
scores that row as a clean pass. This is an intentional precision-for-budget
trade of the opt-in "smoke" flavor; the authoritative
precision/recall must come from a non-early-stop (``stop_early: false``) run.

Recall, by contrast, is never truncated: the fail-stop is DEFERRED while any
*pass-armed* criterion is still undecided, so a distractor misfire on an early
tool call cannot cut a positive row before its expected signal has had the
chance to appear (which would freeze a would-be TP as an FN and deflate
recall/F1). The misfire is not lost — the observable criteria latch
monotonically, so the deferred fail fires the moment every pass-armed criterion
decides (fail-stop is evaluated before pass-stop each round), and if none ever
decides the run simply continues to the cap. A row with zero pass-armed
criteria (e.g. a negative row stacking only distractors) has nothing to defer
for and fail-stops on the first misfire.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Literal, assert_never

from coder_eval.models import EarlyStopInfo, EarlyStopReason, LivePolarity, LiveSuccessCriterion
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

    # Armed pair the watcher holds: (criterion model, its checker). Lives in the
    # TYPE_CHECKING block (only annotations reference it, and those are lazy under
    # `from __future__ import annotations`), so the names are real references
    # rather than quoted strings static analyzers cannot resolve.
    _ArmedPair = tuple[LiveSuccessCriterion, BaseCriterion[Any]]


logger = logging.getLogger(__name__)


def _requested_polarities(
    stop_when: Literal["pass", "fail", "decided", "auto"], decidable: frozenset[LivePolarity]
) -> frozenset[LivePolarity]:
    """The polarities a ``stop_when`` value requests to arm, given what the instance can decide.

    The single source of truth for the ``stop_when`` -> polarity mapping — both
    the resolution-time validator and the runtime watcher resolve through here,
    so a value can never mean different things in the two places. ``pass``/
    ``fail`` request that single polarity; ``decided`` requests both; ``auto``
    requests exactly the instance's own decidable set (which is why it can
    return the empty set: an instance that can decide neither polarity is a
    dead arm, and the validator rejects it). ``assert_never`` makes widening the
    ``stop_when`` Literal without updating this mapping a type error instead of
    a silently inert arm.
    """
    if stop_when == "auto":
        return decidable
    if stop_when == "decided":
        return frozenset({"pass", "fail"})
    if stop_when == "pass" or stop_when == "fail":
        return frozenset({stop_when})
    # `return` is redundant for control flow (assert_never never returns) but
    # keeps every path explicit for analyzers that don't model `Never`.
    return assert_never(stop_when)


class EarlyStopConfigError(ValueError):
    """Raised when a task arms ``run_limits.stop_early`` in a way v1 cannot honor.

    Subclasses ``ValueError`` so the run path's resolve -> ``typer.BadParameter``
    conversion covers it transparently; the ``plan`` command catches this
    subclass specifically to flip its exit code (generic per-variant resolution
    failures intentionally stay soft).
    """


def validate_early_stop(task: TaskDefinition) -> None:
    """Validate an armed early-stop task at resolution time; no-op when unarmed.

    Called after the config layers have merged (``resolve_all_tasks`` post-CLI
    overrides, the ``plan`` per-variant loop, and defensively in
    ``Orchestrator._setup``). All checks below are skipped unless
    ``run_limits.stop_early`` is True, so default runs are entirely unaffected.

    Raise order (matters for which error a multiply-invalid task reports first):
      5. armed together with ``simulation.enabled`` -> error
      1. agent does not declare ``supports_cooperative_stop`` -> error
      2. armed but no criterion sets ``stop_when`` -> error
      then per armed criterion:
      3. criterion is not observable mid-run -> error
      4. requested ``stop_when`` polarity the criterion cannot decide -> error
         (``auto`` errors only on a dead arm: an instance that can decide neither)

    Raises:
        EarlyStopConfigError: on any unsupported armed configuration.
    """
    limits = task.run_limits
    if limits is None or not limits.stop_early:
        return

    # (5) Simulation/dialog mode has its own criteria-driven stop.
    if task.simulation is not None and task.simulation.enabled:
        raise EarlyStopConfigError(
            "run_limits.stop_early is not supported together with simulation.enabled "
            + "(early-stop v1 is single-shot only); use simulation.stop_on_criteria_pass "
            + "for dialog-mode criteria stopping."
        )

    # (1) The agent must honor the cooperative interrupt. Lazily import the
    # registry + plugin loader so this module stays free of runtime coder_eval
    # imports at load time.
    from coder_eval.agents.registry import AgentRegistry
    from coder_eval.plugins import ensure_plugins_loaded

    ensure_plugins_loaded()
    agent_type = str(task.agent.type) if task.agent is not None and task.agent.type is not None else None
    registration = AgentRegistry.get(agent_type) if agent_type is not None else None
    supports = bool(getattr(registration.agent_class, "supports_cooperative_stop", False)) if registration else False
    if not supports:
        raise EarlyStopConfigError(
            "run_limits.stop_early requires an agent that supports cooperative stopping "
            + f"(claude-code, codex, antigravity); agent type {agent_type!r} does not."
        )

    # (2) Arming requires at least one stop criterion.
    armed = [c for c in task.success_criteria if c.stop_when is not None]
    if not armed:
        raise EarlyStopConfigError(
            "run_limits.stop_early is armed but no success criterion sets stop_when; "
            + "arming requires at least one stop criterion (e.g. stop_when: auto)."
        )

    # (3)+(4) Per armed criterion: observable, then the requested polarity is
    # decidable.
    for c in armed:
        # `armed` filtered on `stop_when is not None`; re-bind + assert so pyright
        # narrows the Literal away from None for the set arithmetic below.
        polarity = c.stop_when
        assert polarity is not None
        # (3) Type-level observability: a criterion type is live-observable iff
        # its model is a ``LiveSuccessCriterion`` subclass (models/criteria.py)
        # — the single source of truth, replacing a separate checker-side flag.
        if not isinstance(c, LiveSuccessCriterion):
            raise EarlyStopConfigError(
                f"criterion type {c.type!r} is armed (stop_when={polarity!r}) but is not "
                + "observable mid-run; early-stop supports only live-observable criteria "
                + "(e.g. skill_triggered, command_executed)."
            )
        # (4) Instance-level decidability: some criteria (e.g. command_executed)
        # can decide fewer polarities than their type advertises depending on this
        # instance's config, so gate on the per-instance set — otherwise a dead
        # arm (a polarity this instance can never fire) would silently degrade to
        # a full run instead of erroring here.
        polarities = c.live_decidable_polarities()
        requested = _requested_polarities(polarity, polarities)
        # Dead arm: only `auto` can request the empty set (it requests exactly
        # the instance's decidable polarities) — an instance that can decide
        # neither has nothing to arm and would silently never fire.
        if not requested:
            raise EarlyStopConfigError(
                f"criterion {c.type!r} ({c.description!r}) is armed (stop_when='auto') but this "
                + "instance can decide no polarity mid-run; 'auto' requires at least one "
                + "live-decidable polarity (its decidability can depend on the criterion's "
                + "fields — e.g. command_executed can live-pass only with max_count unset + "
                + "min_count>0, and live-fail only with max_count set)."
            )
        missing = sorted(requested - polarities)
        if missing:
            supported = sorted(polarities) or "no polarities"
            raise EarlyStopConfigError(
                f"criterion {c.type!r} ({c.description!r}) cannot decide polarity {missing} mid-run "
                + f"(stop_when={polarity!r}) for this configuration; it supports {supported}. "
                + "Decidability can depend on the criterion's fields (e.g. command_executed "
                + "can live-pass only with max_count unset + min_count>0, and live-fail only "
                + "with max_count set)."
            )


class EarlyStopWatcher:
    """Observes the agent event stream and trips the cooperative interrupt.

    A ``StreamCallback`` composed into the agent's callback chain (alone when
    ``--stream`` is off, else beside the ``TaskScopedCallback``). It maintains
    its OWN ``EventCollector`` — independent of the one the agent builds its
    returned ``TurnRecord`` from — so each ``live_verdict`` sees a fresh,
    single-element partial-trajectory list. On every tool completion it computes
    all armed verdicts and applies the stop rule; once a stop fires (or the
    watcher disarms on a raising verdict) the decision is latched and further
    events are ignored.

    The orchestrator polls ``should_stop`` (passed to ``agent.communicate``) and,
    after the turn, reads ``info`` to populate ``EvaluationResult.early_stop``.

    Fail-open: a ``live_verdict`` that raises disarms the watcher, logs
    loudly, and degrades the run to a full run (``info`` stays ``None``). Because
    live verdicts are triggers — not truth — this can never produce a *false*
    early stop; it only ever errs toward running more.
    """

    def __init__(
        self,
        task_id: str,
        armed: list[_ArmedPair],
        *,
        max_turns: int | None,
        gate_threshold: float = 1.0,
    ) -> None:
        self._task_id = task_id
        self._armed = armed
        self._gate_threshold = gate_threshold
        self._armed_weight = sum(c.weight for c, _ in armed)
        # Per-instance resolved arming polarities, aligned with ``_armed``. Static
        # for the run, resolved through ``_requested_polarities`` (the single
        # stop_when -> polarity mapping). The stop rule consults this, not the raw
        # ``stop_when`` string, so a distractor armed ``auto`` (fail only) is not
        # required to live-pass for a pass-stop.
        self._armed_polarities: list[frozenset[str]] = [
            self._resolve_armed_polarities(criterion) for criterion, _checker in armed
        ]
        self._max_turns = max_turns
        self._collector = EventCollector()
        self._sdk_turn_index = 0
        self._tool_call_index = 0
        self._started_monotonic: float | None = None
        # Previous round's verdicts, for the "which criterion flipped to pass this
        # round" attribution on pass-stop. Reassigned ONLY at the end of a
        # non-firing evaluation, so it always holds the PREVIOUS round when a stop
        # fires. Starts all-"undecided".
        self._prev_verdicts: list[LiveVerdict] = ["undecided"] * len(armed)
        self._info: EarlyStopInfo | None = None
        self._disarmed = False

    @classmethod
    def for_task(cls, task: TaskDefinition) -> EarlyStopWatcher:
        """Build a watcher for an armed task (instantiates the armed criteria's checkers).

        The criteria registry is imported lazily here — it is not initialized at
        module import time. Checker classes take no ctor args.
        """
        from coder_eval.criteria import CriterionRegistry, init_criteria

        init_criteria(validate=False)
        # The `isinstance` check is defense-in-depth, not load-bearing: every
        # call site (`run`, `plan`) runs `validate_early_stop` first, which
        # already hard-rejects an armed non-observable criterion at resolution
        # time. Narrows `c` to `LiveSuccessCriterion` for pyright either way.
        armed: list[_ArmedPair] = [
            (c, CriterionRegistry.get_checker(c.type)())
            for c in task.success_criteria
            if c.stop_when is not None and isinstance(c, LiveSuccessCriterion)
        ]
        max_turns = task.run_limits.max_turns if task.run_limits is not None else None
        gate_threshold = task.run_limits.stop_early_gate_threshold if task.run_limits is not None else 1.0
        return cls(task.task_id, armed, max_turns=max_turns, gate_threshold=gate_threshold)

    # --- StreamCallback -------------------------------------------------- #

    def on_event(self, event: StreamEvent) -> None:
        """Forward the event to the internal collector; evaluate on each tool call.

        Short-circuits once the decision is latched (fired or disarmed). Counts
        ``TurnStartEvent`` for ``sdk_turn_index`` and each dispatched tool call
        for the 1-based ``tool_call_index``, and stamps the wall-clock origin at
        the FIRST ``AgentStartEvent`` only (a retry's second AgentStart does not
        reset it).

        The decision is evaluated on the tool *call* (``ToolStartEvent``): for an
        observable criterion the verdict is fully determined by the call's inputs,
        so latching here lets the agent's post-dispatch ``should_stop`` poll break
        the loop before a cut-short turn can strip the result. The call is not in
        the collector yet (it reduces commands from ``ToolEndEvent``), so it is
        passed to ``_evaluate`` as the in-flight command, reported at
        ``tool_call_index + 1`` (it has no ``ToolEndEvent`` to count yet). The
        matching ``ToolEndEvent`` still evaluates, which covers a verdict that only
        becomes decidable once the result is known and is a no-op once a call has
        already latched the stop. ``tool_call_index`` is incremented on the
        resolved ``ToolEndEvent`` so it stays a count of completed tool calls.

        UNRESOLVED tool ends are ignored entirely (not counted or evaluated).
        ``_ClaudeTurnState.finalize`` force-closes orphaned tools as UNRESOLVED
        *after* the message loop has ended and the terminal status is already
        chosen (COMPLETED / TIMEOUT / crash) — those are not live tool activity
        and must not trip the stop rule, or a run that ran to completion (or timed
        out / crashed) without a real, in-loop decision would latch a false early
        stop. A legitimate stop always fires on the in-loop call, so dropping
        unresolved ends can never suppress a real stop; it also keeps a crashed
        attempt's orphan tools out of the retry-persistent partial trajectory.
        """
        if self._info is not None or self._disarmed:
            return
        if isinstance(event, AgentStartEvent):
            if self._started_monotonic is None:
                self._started_monotonic = time.monotonic()
        elif isinstance(event, TurnStartEvent):
            self._sdk_turn_index += 1
        elif isinstance(event, ToolStartEvent):
            # Decide on the call, evaluating with it appended as the in-flight
            # command (it has no ToolEnd to count yet, so report it as +1).
            self._evaluate(in_flight=event.tool)
            return
        elif isinstance(event, ToolEndEvent):
            if event.status == ToolEndStatus.UNRESOLVED:
                return
            self._tool_call_index += 1
            self._collector.on_event(event)
            self._evaluate()
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

    # --- Stop rule -------------------------------------------------- #

    @staticmethod
    def _resolve_armed_polarities(criterion: LiveSuccessCriterion) -> frozenset[str]:
        """The polarities this armed instance may fire, via ``_requested_polarities``.

        Validation has already guaranteed the resolved set is non-empty and
        decidable for every armed criterion. ``None`` is never armed, but is
        mapped to the empty set defensively so the caller need not special-case
        it.
        """
        sw = criterion.stop_when
        if sw is None:
            return frozenset()
        return _requested_polarities(sw, criterion.live_decidable_polarities())

    def _ceiling(self, verdicts: list[LiveVerdict]) -> float:
        """Best-case weighted score over the WHOLE armed set, given current verdicts.

        Every already-live-failed criterion is pinned at 0 (a monotonic
        ``live_verdict`` guarantees it stays failed); every ``pass`` or still
        ``undecided`` criterion is credited its full weight (the optimistic
        assumption that it could still end up scoring 1.0). This is the same
        weighting ``EvaluationResult.armed_criteria_passed`` uses for the real,
        final gate, so ``ceiling < gate_threshold`` means the gate is
        mathematically guaranteed to fail no matter how the trajectory continues.
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

    def _evaluate(self, in_flight: CommandTelemetry | None = None) -> None:
        record = self._collector.build_turn_record()
        if in_flight is not None:
            # The in-flight call has no ToolEnd yet, so the collector (which
            # reduces commands from ToolEnd) has not captured it. Append it so its
            # engagement is visible to the verdict; re-sort by sequence to keep the
            # partial trajectory in emission order.
            record.commands = sorted([*record.commands, in_flight], key=lambda c: c.sequence_number)
        records = [record]
        # An in-flight call has not been counted by a ToolEnd yet, so report it as
        # the next (1-based) tool call.
        tool_call_index = self._tool_call_index + (1 if in_flight is not None else 0)
        verdicts: list[LiveVerdict] = []
        for criterion, checker in self._armed:
            try:
                verdicts.append(checker.live_verdict(criterion, records))
            except Exception:
                # Fail-open: a raising verdict disarms; the run degrades to full.
                self._disarmed = True
                logger.error(
                    "[%s] early-stop live_verdict raised for criterion %r; disarming watcher, "
                    + "run degrades to a full run",
                    self._task_id,
                    criterion.type,
                    exc_info=True,
                )
                return

        pass_armed = [i for i, pol in enumerate(self._armed_polarities) if "pass" in pol]

        # Fail-stop: at least one armed criterion (criteria order) that live-fails
        # AND whose resolved arming permits fail is a CANDIDATE — but the stop only
        # actually fires once the ceiling bound (best case: every still-undecided
        # or already-passed armed criterion ends up scoring 1.0, every live-failed
        # one scores 0) can no longer reach ``gate_threshold``, i.e. the armed gate
        # (``EvaluationResult.armed_criteria_passed``) is GUARANTEED to fail no
        # matter what happens on the rest of the trajectory. At the default
        # ``gate_threshold=1.0`` this is equivalent to firing on the first
        # candidate (any armed criterion's weight is > 0 by construction, so a
        # single fail already drops the ceiling below 1.0) — below 1.0 a
        # low-weight candidate's failure may not be enough to doom the gate, so the
        # run keeps going. This is DEFERRED while any pass-armed criterion is still
        # undecided. Cutting a positive row on a distractor misfire before its
        # expected signal could appear would freeze a would-be TP as an FN
        # (truncating the suite's recall); the misfire is latched by the
        # criterion's own monotone semantics, so the deferred fail still fires the
        # moment every pass-armed criterion decides, and a row with zero
        # pass-armed criteria (a negative row) defers nothing.
        if not any(verdicts[i] == "undecided" for i in pass_armed):
            candidate = next(
                (
                    criterion
                    for (criterion, _checker), verdict, armed_pol in zip(
                        self._armed, verdicts, self._armed_polarities, strict=True
                    )
                    if verdict == "fail" and "fail" in armed_pol
                ),
                None,
            )
            if candidate is not None and self._ceiling(verdicts) < self._gate_threshold:
                self._fire(EarlyStopReason.CRITERION_FAILED, candidate, tool_call_index=tool_call_index)
                return

        # Pass-stop: the PASS-ARMED subset's own floor bound (worst case: every
        # still-undecided pass-armed criterion ends up scoring 0, weighted against
        # only the pass-armed subset's total weight) already meets
        # ``gate_threshold`` — guaranteed regardless of what the rest of that
        # subset still decides. Fail-armed criteria (e.g. distractors armed
        # ``auto`` -> fail only) are excluded from both the numerator and the
        # denominator: they can never live-pass and only guard the fail side
        # above, so folding them in would veto every pass-stop (the mixed-arming
        # bug) and penalize this bound for a criterion it was never scoped to
        # cover. At the default ``gate_threshold=1.0`` this requires every
        # pass-armed criterion to actually be "pass" (any non-pass drops the floor
        # below 1.0) — identical to the pre-weighting ``all(...)`` rule. Guard the
        # vacuous case: with zero pass-armed criteria (a negative row whose
        # criteria are all distractors) there is nothing to pass-stop on, so the
        # run must continue to the cap rather than firing on turn 0 with an empty
        # numerator/denominator.
        floor = self._floor(verdicts, pass_armed)
        if floor is not None and floor >= self._gate_threshold:
            # Deciding criterion = the last pass-armed (criteria order) whose verdict
            # flipped vs the previous round; fall back to the last pass-armed.
            deciding = self._armed[pass_armed[-1]][0]
            for i in pass_armed:
                if verdicts[i] != self._prev_verdicts[i]:
                    deciding = self._armed[i][0]
            self._fire(EarlyStopReason.CRITERION_PASSED, deciding, tool_call_index=tool_call_index)
            return

        # Decision-step budget: an armed criterion with max_steps_to_decide set
        # that is STILL "undecided" once that many tool-call steps have elapsed
        # forces a hard fail — checked last, after the real fail-/pass-stop
        # checks above, so a criterion that decides on this very round (however
        # late) is never punished for a budget it technically exceeded. It never
        # reached a verdict at all, so there is nothing meaningful to weigh it
        # against — this is why DECISION_BUDGET_EXCEEDED bypasses the weighted
        # gate entirely at the orchestrator finalize step rather than folding
        # into the ceiling/floor bounds above.
        for (criterion, _checker), verdict in zip(self._armed, verdicts, strict=True):
            budget = criterion.max_steps_to_decide
            if budget is not None and verdict == "undecided" and tool_call_index >= budget:
                self._fire(EarlyStopReason.DECISION_BUDGET_EXCEEDED, criterion, tool_call_index=tool_call_index)
                return

        # No stop this round — record the verdicts so the next round can detect flips.
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
