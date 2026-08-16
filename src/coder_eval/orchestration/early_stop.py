"""Early-stop-on-criterion: resolution-time validation + runtime watcher.

Opt-in mechanism that ends a single-shot run early once its *armed* criteria
decide the outcome mid-run. Arming lives ENTIRELY on the criterion — there is
no run-level master switch. A ``stop_early:`` block on a live-observable
criterion (``LiveSuccessCriterion`` only, so arming an unobservable criterion
is unrepresentable) alone activates the run's watcher;
``run_limits.stop_early: false`` is the run-level KILL SWITCH that force-
disarms every block (the one-line experiment-variant override for an
authoritative, non-truncated run), and ``run_limits.stop_early: true`` — the
removed master arm — is rejected at resolution:

* ``stop_early: {}`` — the block's PRESENCE is the arming; it carries one
  implicit trigger: a native live FAIL may end the run (fail-stop).
* ``stop_early: {on_pass: stop}`` — a live PASS may also end the run
  (pass-stop); the default ``on_pass: continue`` just latches the verdict.
* ``stop_early: {decide_within: N}`` — still undecided after N tool-call steps
  latches an *effective* FAIL, fed through the same fail-stop rule (reported
  as ``decision_budget_exceeded``).

A trigger whose polarity the instance can never decide (see
``live_decidable_polarities``) is INERT BY DESIGN, not an error — one
dataset-fanned YAML line (same block on every row) serves both positive rows
(pass/timeout live, fail inert) and distractor rows (fail live, pass/timeout
inert) without per-row conditionals.

This module owns the whole feature:

* ``validate_early_stop`` — resolution-time guardrails. Rejects every
  configuration v1 cannot honor as a hard error, so an unsupported arming is
  never a silent no-op.
* ``EarlyStopWatcher`` — the runtime observer. A ``StreamCallback`` composed
  into the agent's event stream that maintains its own ``EventCollector``,
  evaluates the armed criteria's ``live_verdict`` on each tool *call* (and on
  its result), applies the stop rule, and exposes ``should_stop()`` (the
  cooperative interrupt the agent polls) plus ``info`` (the ``EarlyStopInfo``
  the orchestrator records). Fail-open: a raising ``live_verdict`` disarms the
  watcher and degrades to a full run — a verdict bug can never cause a *false*
  early stop.

Verdicts LATCH: once an armed criterion decides (pass or fail) on a resolved
round, its ``live_verdict`` is never polled again for the rest of the run —
the checkers' documented monotonicity makes re-polling pure waste. Latching
happens only on resolved rounds (``ToolEndEvent``); an in-flight round's fresh
verdict can fire a stop but is not persisted, so a dispatched call that never
resolves (e.g. a crashed attempt) cannot leave a stale verdict behind across
retries.

Evaluating on the tool *call* (``ToolStartEvent``) as well as on its result is
what makes the stop robust: where a criterion CAN decide from the call's inputs
alone, the watcher latches the instant the call is dispatched — before a
cut-short turn (e.g. a timeout) can strip the result and leave the call
unresolved. The agent polls ``should_stop`` immediately after dispatching each
message, so a stop on the call breaks the loop before the result message is ever
pulled.

Which criteria can decide there is **per-criterion, and sometimes per-field**;
the seam is not universal and must not be assumed. ``skill_triggered`` can never
decide at ToolStart, and ``command_executed`` can only while ``require_success``
is unset. The whole rule, with its reasons, is on
``EarlyStopWatcher._on_event_impl`` — the one declaration; do not restate it
here.

Live verdicts only *trigger* the stop; the authoritative scores always come
from the standard ``check_all_async`` on the frozen trajectory after the cut.

Weighting: both the stop rule and the post-hoc gate
(``EvaluationResult.armed_criteria_passed``) consult ``run_limits.
stop_early_gate_threshold`` (default ``1.0``) rather than treating every armed
criterion's pass/fail as equally decisive. A fail-stop fires once the armed
set's CEILING (best case: every still-``undecided``/``pass`` criterion scores
1.0, every effectively-failed one scores 0) can no longer reach the threshold —
i.e. the gate is mathematically guaranteed to fail regardless of how the
trajectory continues. A ``decide_within`` timeout participates as an
ordinary weighted fail: a low-weight criterion's timeout that cannot doom the
gate does not stop the run (it is absorbed, exactly like a low-weight native
fail). A pass-stop fires once the ``on_pass: stop`` subset's FLOOR (worst
case: every still-undecided one scores 0) already meets the threshold. At the
default threshold of 1.0 both bounds collapse exactly to "any single armed
criterion's effective fail stops the run" / "every on_pass=stop criterion has
live-passed". Lowering the threshold lets a low-weight armed criterion's
failure be absorbed without truncating the run.

Precision trade-off: a pass-stop cuts the run the instant the on_pass=stop
floor locks in, so a fail-armed criterion (e.g. a distractor) that would
only misfire on a LATER tool call is never observed — the frozen trajectory
then scores that row as a clean pass. This is an intentional
precision-for-budget trade of the opt-in "smoke" flavor; the authoritative
precision/recall must come from a non-early-stop (``stop_early: false``) run.

Recall, by contrast, is never truncated — BOTH stops defer on it. The
fail-stop is DEFERRED while any pass-capable armed criterion is still
undecided (and within its budget), so a distractor misfire on an early tool
call cannot cut a positive row before its expected signal has had the chance
to appear (which would freeze a would-be TP as an FN and deflate recall/F1).
Symmetrically, the pass-stop is DEFERRED while any pass-capable armed
criterion OUTSIDE the on_pass=stop subset is still undecided (members of the
subset are already accounted for by the floor bound itself) — otherwise an
on_pass=stop criterion passing early would freeze a sibling ``on_pass:
continue`` criterion (e.g. one armed via ``decide_within``) as an unearned
fail on the truncated trajectory. Neither deferral loses the trigger —
verdicts latch monotonically, so the held stop fires the moment every
pass-capable armed criterion decides (fail-stop is evaluated before pass-stop
each round), and if none ever decides the run simply continues to the cap. A
row with zero pass-capable armed criteria (e.g. a negative row stacking only
distractors) has nothing to defer for and fail-stops on the first misfire.
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

    # Armed pair the watcher holds: (criterion model, its checker). Lives in the
    # TYPE_CHECKING block (only annotations reference it, and those are lazy under
    # `from __future__ import annotations`), so the names are real references
    # rather than quoted strings static analyzers cannot resolve.
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

    Called after the config layers have merged (``resolve_all_tasks`` post-CLI
    overrides, the ``plan`` per-variant loop, and defensively in
    ``Orchestrator._setup``). ``run_limits.stop_early: true`` (the removed
    master arm) is always rejected; everything else is skipped unless the task
    is actually armed (``early_stop_active``), so default runs — and runs
    force-disarmed via the ``stop_early: false`` kill switch — are entirely
    unaffected.

    Raise order (matters for which error a multiply-invalid task reports first):
      1. ``run_limits.stop_early: true`` -> error (master arm removed)
      2. armed together with ``simulation.enabled`` -> error
      3. agent does not declare ``supports_cooperative_stop`` -> error
      4. degenerate ``stop_early_gate_threshold`` (``<= 0.0``) -> error

    There are deliberately NO per-instance polarity guards: a trigger whose
    polarity this instance cannot decide is inert by design (documented on each
    trigger field), which is what lets one dataset-fanned YAML line serve both
    positive and distractor rows. Arming an unobservable criterion type is
    structurally impossible — the ``stop_early`` block exists only on
    ``LiveSuccessCriterion``, so a ``file_exists`` criterion carrying one is a
    pydantic ``extra='forbid'`` error at load time, not a case this validator
    needs to catch. And an armed-but-empty set needs no guard either: with no
    blocks present there is simply no watcher, byte-for-byte default behavior.

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

    # (4) A threshold of exactly 0 trivially satisfies both the pass-stop
    # floor check and the final weighted gate regardless of whether any armed
    # criterion has actually decided — neutralizing the armed pass/fail gate
    # with one YAML line (coder-eval is used as a CI gate). Checked here
    # (not on RunLimits itself) because this is the whole-task, hard-stop
    # surface: an EarlyStopConfigError here flips the plan exit code and
    # aborts run, whereas a plain ValueError on the merged RunLimits model
    # would land in the CLI's generic "resolution failed" branch, which
    # prints red text but does not flip the exit code.
    if limits is not None and limits.stop_early_gate_threshold <= 0.0:
        raise EarlyStopConfigError(
            f"run_limits.stop_early_gate_threshold ({limits.stop_early_gate_threshold}) must be "
            + "> 0.0 on an armed task (a threshold of 0 trivially passes the armed gate "
            + "regardless of whether any armed criterion actually decided)."
        )


class EarlyStopWatcher:
    """Observes the agent event stream and trips the cooperative interrupt.

    A ``StreamCallback`` composed into the agent's callback chain (alone when
    ``--stream`` is off, else beside the ``TaskScopedCallback``). It maintains
    its OWN ``EventCollector`` — independent of the one the agent builds its
    returned ``TurnRecord`` from — so each ``live_verdict`` sees a fresh,
    single-element partial-trajectory list. On every tool call it evaluates the
    armed criteria still undecided and applies the stop rule; once a stop fires
    (or the watcher disarms on a raising verdict) the decision is latched and
    further events are ignored.

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
        gate_threshold: float = DEFAULT_STOP_EARLY_GATE_THRESHOLD,
    ) -> None:
        self._task_id = task_id
        self._armed = armed
        self._gate_threshold = gate_threshold
        self._armed_weight = sum(c.weight for c, _ in armed)
        # Per-instance decidable polarities, aligned with ``_armed``. Static for
        # the run. Each trigger below is resolved against this set — a trigger
        # whose polarity the instance cannot decide is inert by design.
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
        # The fail trigger is IMPLICIT in arming: an armed criterion's native
        # live-fail may always stop the run (ceiling-gated) — that is what the
        # block's presence means. Inert when the instance can't live-fail.
        self._fail_trigger: list[bool] = ["fail" in pol for pol in self._decidable]
        # A timeout only means anything for an instance actively waiting to
        # observe a pass — a fail-only instance's 'undecided' is its success
        # state (the forbidden event hasn't happened), so its budget is inert.
        self._budget: list[int | None] = [
            block.decide_within if "pass" in pol else None for block, pol in zip(blocks, self._decidable, strict=True)
        ]
        if not any(self._pass_trigger) and not any(self._fail_trigger) and all(b is None for b in self._budget):
            # Legal (a fanned row whose armed lines are all inert for this row's
            # role) but user-visible: on a non-fanned task this is dead config —
            # the row can never stop early and will simply run to the cap,
            # gating on the armed subset if the watcher somehow fires.
            logger.warning("[%s] all armed stop triggers are inert for this row; run cannot stop early", task_id)
        self._max_turns = max_turns
        self._collector = EventCollector()
        self._sdk_turn_index = 0
        self._tool_call_index = 0
        self._started_monotonic: float | None = None
        # Latched verdicts, aligned with ``_armed``. Once an entry leaves
        # "undecided" (on a RESOLVED round) its checker is never polled again —
        # the checkers' documented monotonicity makes re-polling pure waste.
        # ``_budget_expired`` marks a latched fail as timeout-driven (reported
        # as DECISION_BUDGET_EXCEEDED instead of CRITERION_FAILED).
        self._latched: list[LiveVerdict] = ["undecided"] * len(armed)
        self._budget_expired: list[bool] = [False] * len(armed)
        # Previous round's verdicts, for the "which criterion flipped to pass
        # this round" attribution on pass-stop. Reassigned ONLY at the end of a
        # non-firing evaluation, so it always holds the PREVIOUS round when a
        # stop fires. Starts all-"undecided".
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

    # --- StreamCallback -------------------------------------------------- #

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

        Counts
        ``TurnStartEvent`` for ``sdk_turn_index`` and each dispatched tool call
        for the 1-based ``tool_call_index``, and stamps the wall-clock origin at
        the FIRST ``AgentStartEvent`` only (a retry's second AgentStart does not
        reset it).

        The decision is evaluated on the tool *call* (``ToolStartEvent``), which
        lets the agent's post-dispatch ``should_stop`` poll break the loop before a
        cut-short turn can strip the result. Whether a given criterion can actually
        decide there is **per-criterion, not global**, and for
        ``command_executed`` it is per-CONFIGURATION too: its verdict is
        determined by the call's inputs only while ``require_success`` is unset.
        With ``require_success`` — the configuration CE034 mandates for an armed,
        live-*passable* ``command_executed`` — ``_matching_commands`` drops any
        command whose ``result_status != "success"``, and an in-flight call
        carries ``result_status=None``; so it is not counted, the criterion stays
        undecided at ToolStart, and the matching ``ToolEndEvent`` below is what
        decides. ``skill_triggered`` is never decidable at ToolStart: for the
        ``Skill`` tool the body is delivered AS the result, so an in-flight call
        has engaged nothing and that criterion deliberately stays undecided until
        its ``ToolEndEvent``. A new live criterion must state which seam its
        verdict is decidable at — and, if its answer depends on its own fields,
        under which settings. The call is
        not in the collector yet (it reduces commands from ``ToolEndEvent``), so it
        is passed to ``_evaluate_impl`` as the in-flight command, reported at
        ``tool_call_index + 1`` (it has no ``ToolEndEvent`` to count yet). The
        matching ``ToolEndEvent`` still evaluates, which covers a verdict that only
        becomes decidable once the result is known and is a no-op once a call has
        already latched the stop. ``tool_call_index`` is incremented on the
        resolved ``ToolEndEvent`` so it stays a count of completed tool calls.

        UNRESOLVED tool ends are RECORDED but never counted or evaluated on.
        ``_ClaudeTurnState.finalize`` force-closes orphaned tools as UNRESOLVED
        *after* the message loop has ended and the terminal status is already
        chosen (COMPLETED / TIMEOUT / crash) — those are not live tool activity
        and must not trip the stop rule, or a run that ran to completion (or timed
        out / crashed) without a real, in-loop decision would latch a false early
        stop. A legitimate stop always fires on the in-loop call, so skipping the
        evaluation can never suppress a real stop. They DO land in the collector:
        the agent's own ``EventCollector`` records force-closed commands into the
        ``TurnRecord`` that ``check_all_async`` later scores (including a crashed
        attempt's drained partial turn), so the watcher must reduce the same
        trajectory — otherwise its verdicts (and the fail-stop's ceiling bound)
        would be computed over a strictly smaller command set than the
        authoritative check, and a ``decide_within`` timeout could latch an
        effective fail on a criterion the frozen trajectory scores as a pass.
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
                # Trajectory parity with the agent's collector (see docstring):
                # record, but don't count a round or evaluate — a force-closed
                # orphan is not live tool activity and must not fire a stop.
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

    # --- Stop rule -------------------------------------------------- #

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
            # The in-flight call has no ToolEnd yet, so the collector (which
            # reduces commands from ToolEnd) has not captured it. Append it so its
            # engagement is visible to the verdict; re-sort by sequence to keep the
            # partial trajectory in emission order.
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
                # Re-raise to the wrapping try/except in on_event, which sets
                # _disarmed — but log the specific criterion here first, since
                # that context (which criterion's live_verdict raised) would
                # otherwise be lost once the exception is caught generically.
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

        # Recall deferral: while any pass-capable armed criterion is still
        # undecided (within its budget — an expired budget is already an
        # effective fail), a fail-stop is HELD. Cutting a positive row on a
        # distractor misfire before its expected signal could appear would
        # freeze a would-be TP as an FN (truncating the suite's recall); the
        # misfire latches via the criterion's own monotone semantics, so the
        # deferred fail still fires the moment every pass-capable criterion
        # decides, and a row with zero pass-capable criteria (a negative row)
        # defers nothing.
        pass_capable_undecided = any(
            v == "undecided" and "pass" in pol for v, pol in zip(verdicts, self._decidable, strict=True)
        )

        # Fail-stop: a criterion whose effective verdict is "fail" is a
        # CANDIDATE — the fail trigger is implicit in arming (native fail on a
        # fail-capable instance, or a decide_within timeout). The stop only fires once the ceiling bound (best case:
        # every still-undecided or already-passed armed criterion ends up
        # scoring 1.0, every failed one scores 0) can no longer reach
        # ``gate_threshold``, i.e. the armed gate (``EvaluationResult.
        # armed_criteria_passed``) is GUARANTEED to fail no matter what happens
        # on the rest of the trajectory. At the default ``gate_threshold=1.0``
        # this is equivalent to firing on the first candidate (any armed
        # criterion's weight is > 0 by construction, so a single fail already
        # drops the ceiling below 1.0) — below 1.0 a low-weight candidate's
        # failure (or timeout) may not be enough to doom the gate, so the run
        # keeps going: the failure is absorbed.
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

        # Pass-stop: the on_pass=stop subset's own floor bound (worst case:
        # every still-undecided member scores 0, weighted against only that
        # subset's total weight) already meets ``gate_threshold`` — guaranteed
        # regardless of what the rest of that subset still decides. Criteria
        # armed only on the fail side (distractors) are excluded from both the
        # numerator and the denominator: they can never live-pass and only
        # guard the fail side above, so folding them in would veto every
        # pass-stop and penalize this bound for a criterion it was never
        # scoped to cover. At the default ``gate_threshold=1.0`` this requires
        # every on_pass=stop criterion to actually be "pass" (any non-pass
        # drops the floor below 1.0). The vacuous case (no on_pass=stop
        # criteria at all) returns None — nothing to pass-stop on, the run
        # continues to the cap.
        #
        # Recall deferral, mirrored from the fail-stop: the pass-stop is HELD
        # while any pass-capable armed criterion OUTSIDE the on_pass=stop
        # subset is still undecided (members of the subset are already priced
        # into the floor). Cutting here would freeze a sibling
        # ``on_pass: continue`` criterion's expected signal out of the
        # trajectory — an unearned fail on the armed gate that a full run
        # would not have produced. Once every such criterion decides (pass or
        # fail), the still-satisfied floor fires the pass-stop on that round.
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
