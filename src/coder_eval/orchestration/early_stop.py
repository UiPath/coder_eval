"""Early-stop-on-criterion: resolution-time validation.

Opt-in mechanism (``run_limits.stop_early``) that ends a single-shot run as soon
as its *armed* criteria (those with ``stop_when`` set) are decided mid-run — on
pass or on a definitive fail. This module owns the resolution-time guardrails:
``validate_early_stop`` rejects every configuration v1 cannot honor as a hard
error, so an unsupported arming is never a silent no-op. The runtime watcher that
actually observes the event stream and trips the interrupt lands in a later
commit; until then arming is inert at runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from coder_eval.models import TaskDefinition


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
        polarities = checker_cls.live_stop_polarities
        if not polarities:
            raise EarlyStopConfigError(
                f"criterion type {c.type!r} is armed (stop_when={polarity!r}) but is not "
                + "observable mid-run; early-stop supports only live-observable criteria "
                + "(e.g. skill_triggered, command_executed)."
            )
        needed = {"pass", "fail"} if polarity == "decided" else {polarity}
        missing = sorted(needed - polarities)
        if missing:
            raise EarlyStopConfigError(
                f"criterion type {c.type!r} cannot decide polarity {missing} mid-run "
                + f"(stop_when={polarity!r}); it supports {sorted(polarities)}."
            )
