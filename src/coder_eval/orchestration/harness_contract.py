"""Resolution-time check that a task sets no agent field its harness cannot honor."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from coder_eval.models import Enforcement


if TYPE_CHECKING:
    from coder_eval.agents.registry import AgentRegistration
    from coder_eval.models import TaskDefinition


class TaskResolutionError(ValueError):
    """A hard configuration error found at resolution: never demoted to a skipped task."""


class HarnessContractError(TaskResolutionError):
    """The task's agent config sets a field its harness declares unsupported, or names no registered harness."""


# BaseAgentConfig field -> the HarnessContract row that gates it.
_GATED: dict[str, str] = {
    "system_prompt": "system_prompt",
    "plugins": "plugin_skills",
    "permission_mode": "permission_mode",
    "allowed_tools": "allowed_tools",
    "disallowed_tools": "disallowed_tools",
}


def registration_for(task: TaskDefinition, *, requirement: str, hint: str = "") -> AgentRegistration[Any]:
    """The registry entry for the task's resolved agent kind.

    Args:
        task: A resolved task.
        requirement: What needs the registration, opening each error message.
        hint: A sentence appended to each error message.

    Raises:
        HarnessContractError: the task has no agent type, or its kind is not registered.
    """
    from coder_eval.agents.registry import AgentRegistry
    from coder_eval.plugins import ensure_plugins_loaded

    ensure_plugins_loaded()
    suffix = f" {hint}" if hint else ""
    if task.agent is None or task.agent.type is None:
        raise HarnessContractError(
            f"{requirement} requires an agent block with a registered type; this task resolves without one.{suffix}"
        )
    kind = str(task.agent.type)
    registration = AgentRegistry.get(kind)
    if registration is None:
        raise HarnessContractError(
            f"{requirement} requires a registered agent type; {kind!r} is not registered "
            + f"(is the providing plugin installed and loaded?).{suffix}"
        )
    return registration


def validate_harness_contract(task: TaskDefinition) -> None:
    """Reject a gated agent field that is set on a harness whose contract marks it unsupported.

    A field is set when a config layer wrote it and its value is not None. A task
    without an agent type returns silently; the layer-5 type guard reports that.

    Raises:
        HarnessContractError: on the first unsupported field that is set, or an unregistered kind.
    """
    if task.agent is None or task.agent.type is None:
        return
    from coder_eval.agents.registry import AgentRegistry

    contract = registration_for(task, requirement="The harness contract check").agent_class.contract
    kind = str(task.agent.type)
    for field, row in _GATED.items():
        is_set = field in task.agent.model_fields_set and getattr(task.agent, field) is not None
        if is_set and getattr(contract, row) is Enforcement.UNSUPPORTED:
            honoring = [
                k
                for k in AgentRegistry.list_kinds()
                if (reg := AgentRegistry.get(k)) is not None
                and getattr(reg.agent_class.contract, row) is Enforcement.ENFORCED
            ]
            raise HarnessContractError(
                f"agent.{field} is set but the {kind!r} harness does not support it "
                + "(see docs/agents/HARNESS_PARITY.md). Remove the field, or move it under "
                + f"by_type.<kind> in the experiment for a harness that honors it ({', '.join(honoring) or 'none'})."
            )
