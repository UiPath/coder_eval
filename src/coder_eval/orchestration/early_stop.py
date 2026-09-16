"""Early-stop-on-criterion: resolution-time validation.

Opt-in. Arming lives ENTIRELY on the criterion — there is no run-level master
switch. A ``stop_early:`` block on a ``LiveSuccessCriterion`` (so arming an
unobservable criterion is unrepresentable) alone arms the run's ``TurnMonitor``;
``run_limits.stop_early: false`` is the run-level KILL SWITCH, and
``stop_early: true`` — the removed master arm — is rejected at resolution.

* ``stop_early: {}`` — the block's PRESENCE is the arming, carrying one implicit
  trigger: a native live FAIL may end the run.
* ``stop_early: {on_pass: stop}`` — a live PASS may also end it.
* ``stop_early: {decide_within: N}`` — still undecided after N tool-call steps
  latches an effective FAIL, fed through the same rule.

A trigger whose polarity an instance can never decide is INERT BY DESIGN.

This module owns the resolution-time guardrails (``validate_early_stop``) and the
arming predicate; ``orchestration.turn_monitor.TurnMonitor`` evaluates the armed
criteria at run time.

Rationale: .claude/notes/orchestration.md § Early stop on criterion
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from coder_eval.orchestration.harness_contract import TaskResolutionError, registration_for


if TYPE_CHECKING:
    from coder_eval.models import TaskDefinition


logger = logging.getLogger(__name__)


class EarlyStopConfigError(TaskResolutionError):
    """Raised when a task arms early-stop in a way v1 cannot honor.

    A ``TaskResolutionError`` (so a ``ValueError``): the run path aborts on it and
    the ``plan`` command flips its exit code, while generic per-variant resolution
    failures stay soft.
    """


def early_stop_active(task: TaskDefinition) -> bool:
    """True iff this run arms the monitor's criteria: >= 1 armed criterion, kill switch not thrown.

    The single arming predicate the orchestrator consults. Deliberately ignores
    ``run_limits.stop_early is True`` (the removed master arm) — that value is
    rejected by ``validate_early_stop``, which every path runs before monitor
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
      3. agent's contract does not declare ``cooperative_stop``
      4. degenerate ``stop_early_gate_threshold`` (``<= 0.0``)

    There are deliberately NO per-instance polarity guards, and no armed-but-empty
    guard.

    Rationale: .claude/notes/orchestration.md § Inert triggers are by design, and the watcher fails open

    Raises:
        EarlyStopConfigError: on any unsupported armed configuration.
        HarnessContractError: an armed task has no agent type, or an unregistered one.
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

    # (3) The agent must honor the cooperative interrupt.
    from coder_eval.agents.registry import AgentRegistry

    registration = registration_for(
        task,
        requirement="criterion-level stop_early arming",
        hint="Disarm with run_limits.stop_early: false to bypass this check.",
    )
    assert task.agent is not None
    agent_type = str(task.agent.type)
    if not registration.agent_class.contract.cooperative_stop:
        supporting = ", ".join(
            kind
            for kind in AgentRegistry.list_kinds()
            if (reg := AgentRegistry.get(kind)) is not None and reg.agent_class.contract.cooperative_stop
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
