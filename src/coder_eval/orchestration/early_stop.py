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
from the standard ``check_all`` on the frozen trajectory after the cut.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from coder_eval.models import EarlyStopInfo, EarlyStopReason
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
    from coder_eval.models import BaseSuccessCriterion, CommandTelemetry, TaskDefinition

    # Armed pair the watcher holds: (criterion model, its checker). Lives in the
    # TYPE_CHECKING block (only annotations reference it, and those are lazy under
    # `from __future__ import annotations`), so the names are real references
    # rather than quoted strings static analyzers cannot resolve.
    _ArmedPair = tuple[BaseSuccessCriterion, BaseCriterion[Any]]


logger = logging.getLogger(__name__)


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
            + f"(currently Claude Code); agent type {agent_type!r} does not."
        )

    # (2) Arming requires at least one stop criterion.
    armed = [c for c in task.success_criteria if c.stop_when is not None]
    if not armed:
        raise EarlyStopConfigError(
            "run_limits.stop_early is armed but no success criterion sets stop_when; "
            + "arming requires at least one stop criterion (e.g. stop_when: decided)."
        )

    # (3)+(4) Per armed criterion: observable, then the requested polarity is
    # decidable. The criteria registry is not initialized at resolution time.
    from coder_eval.criteria import CriterionRegistry, init_criteria

    init_criteria(validate=False)
    for c in armed:
        # `armed` filtered on `stop_when is not None`; re-bind + assert so pyright
        # narrows the Literal away from None for the set arithmetic below.
        polarity = c.stop_when
        assert polarity is not None
        checker_cls = CriterionRegistry.get_checker(c.type)
        # (3) Class-level observability: an empty ``live_stop_polarities`` means
        # the criterion TYPE can never decide mid-run, regardless of config.
        if not checker_cls.live_stop_polarities:
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
        polarities = checker_cls.live_decidable_polarities(c)
        needed = {"pass", "fail"} if polarity == "decided" else {polarity}
        missing = sorted(needed - polarities)
        if missing:
            supported = sorted(polarities) or "no polarities"
            raise EarlyStopConfigError(
                f"criterion type {c.type!r} cannot decide polarity {missing} mid-run "
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
    ) -> None:
        self._task_id = task_id
        self._armed = armed
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
        armed: list[_ArmedPair] = [
            (c, CriterionRegistry.get_checker(c.type)()) for c in task.success_criteria if c.stop_when is not None
        ]
        max_turns = task.run_limits.max_turns if task.run_limits is not None else None
        return cls(task.task_id, armed, max_turns=max_turns)

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

    def _evaluate(self, in_flight: CommandTelemetry | None = None) -> None:
        record = self._collector.build_turn_record()
        if in_flight is not None:
            # The in-flight call has no ToolEnd yet, so the collector (which
            # reduces commands from ToolEnd) has not captured it. Append it and
            # re-sort by sequence so the verdict sees it in first-engagement order.
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

        # Fail-stop: first armed criterion (criteria order) that live-fails AND
        # whose stop_when permits fail decides the run.
        for (criterion, _checker), verdict in zip(self._armed, verdicts, strict=True):
            if verdict == "fail" and criterion.stop_when in ("fail", "decided"):
                self._fire(EarlyStopReason.CRITERION_FAILED, criterion, tool_call_index=tool_call_index)
                return

        # Pass-stop: EVERY armed criterion live-passes AND each permits pass.
        all_pass = all(v == "pass" for v in verdicts)
        all_permit_pass = all(c.stop_when in ("pass", "decided") for c, _ in self._armed)
        if all_pass and all_permit_pass:
            # Deciding criterion = the last armed (criteria order) whose verdict
            # flipped vs the previous round; fall back to the last armed.
            deciding = self._armed[-1][0]
            for (criterion, _checker), verdict, prev in zip(self._armed, verdicts, self._prev_verdicts, strict=True):
                if verdict != prev:
                    deciding = criterion
            self._fire(EarlyStopReason.CRITERION_PASSED, deciding, tool_call_index=tool_call_index)
            return

        # No stop this round — record the verdicts so the next round can detect flips.
        self._prev_verdicts = verdicts

    def _fire(self, reason: EarlyStopReason, criterion: BaseSuccessCriterion, *, tool_call_index: int) -> None:
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
