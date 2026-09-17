"""Resolution-time check that a task sets no agent field its harness cannot honor."""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from coder_eval.models import (
    CANONICAL_TOOL_NAMES,
    BaseAgentConfig,
    Enforcement,
    HarnessContract,
    PermissionMode,
    ToolNameMap,
)


if TYPE_CHECKING:
    from coder_eval.agents.registry import AgentRegistration
    from coder_eval.models import TaskDefinition


class TaskResolutionError(ValueError):
    """A hard configuration error found at resolution: never demoted to a skipped task."""


class HarnessContractError(TaskResolutionError):
    """The task's agent config sets a field its harness declares unsupported, or names no registered harness."""


# `mcp__<server>` (every tool of a server) or `mcp__<server>__<tool>`.
_MCP_NAME = re.compile(r"mcp__[^_]\S*")
# A Claude Code permission rule: `Bash(git status:*)`, `Read(./src/**)`.
_RULE_SPECIFIER = re.compile(r"(?P<tool>[A-Za-z]+)\(.*\)")

# BaseAgentConfig field -> the HarnessContract row that gates it.
_GATED: dict[str, str] = {
    "system_prompt": "system_prompt",
    "plugins": "plugin_skills",
    "permission_mode": "permission_mode",
    "allowed_tools": "allowed_tools",
    "disallowed_tools": "disallowed_tools",
}

MODEL_TURN_LIMITS: tuple[str, ...] = ("max_turns", "expected_turns")


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
    """Reject agent config the task's harness cannot honor with its documented meaning.

    Four checks, in order: a gated field set on a harness whose contract marks it
    unsupported; a ``permission_mode`` value outside the contract's
    ``permission_modes``; a tool-list name outside ``CANONICAL_TOOL_NAMES`` (or an
    ``mcp__`` name the harness cannot address); a non-None model-turn run limit
    (``MODEL_TURN_LIMITS``) on a harness whose contract does not count model turns.
    An agent field is set when a config layer wrote it with a value other than None
    or an empty tool list (which restricts nothing). A task without an agent type
    returns silently; the layer-5 type guard reports that.

    Raises:
        HarnessContractError: on the first violation, or an unregistered kind.
    """
    if task.agent is None or task.agent.type is None:
        return
    registration = registration_for(task, requirement="The harness contract check")
    contract = registration.agent_class.contract
    kind = str(task.agent.type)
    set_fields = [field for field in _GATED if _is_set(task.agent, field)]
    for field in set_fields:
        row = _GATED[field]
        if getattr(contract, row) is Enforcement.UNSUPPORTED:
            honoring = _honoring_kinds(lambda c, row=row: getattr(c, row) is Enforcement.ENFORCED)
            raise HarnessContractError(
                f"agent.{field} is set but the {kind!r} harness does not support it "
                + "(see docs/agents/HARNESS_PARITY.md). Remove the field, or move it under by_type.<kind> "
                + f"in the experiment for a harness that honors it ({honoring})."
            )
    if "permission_mode" in set_fields:
        _check_permission_value(task.agent.permission_mode, contract.permission_modes or frozenset(), kind)
    tool_names = registration.agent_class.tool_names
    for field in ("allowed_tools", "disallowed_tools"):
        if field in set_fields and tool_names is not None:
            _check_tool_names(field, getattr(task.agent, field), tool_names, kind)
    _check_model_turn_limits(task, contract, kind)


def _check_model_turn_limits(task: TaskDefinition, contract: HarnessContract, kind: str) -> None:
    limits = task.run_limits
    if limits is None or contract.counts_model_turns:
        return
    for field in MODEL_TURN_LIMITS:
        if getattr(limits, field) is not None:
            raise HarnessContractError(
                f"run_limits.{field} is set but the {kind!r} harness reports no per-response turn boundary "
                + f"(usage_granularity={contract.usage_granularity.value}; see docs/agents/HARNESS_PARITY.md). "
                + "Remove the field, or set it only in a variant for a harness that counts model turns "
                + f"({_honoring_kinds(lambda c: c.counts_model_turns)})."
            )


def _is_set(agent: BaseAgentConfig, field: str) -> bool:
    """A layer wrote the field with a value that means something: not None, and not an empty tool list."""
    value = getattr(agent, field)
    return field in agent.model_fields_set and value is not None and value != []


def _honoring_kinds(honors: Callable[[HarnessContract], bool]) -> str:
    from coder_eval.agents.registry import AgentRegistry

    kinds = [
        k for k in AgentRegistry.list_kinds() if (reg := AgentRegistry.get(k)) and honors(reg.agent_class.contract)
    ]
    return ", ".join(kinds) or "none"


def _check_permission_value(value: PermissionMode, honored: frozenset[PermissionMode], kind: str) -> None:
    if value not in honored:
        raise HarnessContractError(
            f"agent.permission_mode={str(value)!r} has no documented meaning on the {kind!r} harness, which "
            + f"honors {sorted(str(m) for m in honored)} (see docs/agents/HARNESS_PARITY.md). Use one of those, "
            + "or move the value under by_type.<kind> for a harness that honors it "
            + f"({_honoring_kinds(lambda c: value in (c.permission_modes or frozenset()))})."
        )


def _check_tool_names(field: str, names: list[str], tool_names: ToolNameMap, kind: str) -> None:
    def accepted(name: str) -> bool:
        if tool_names.mcp_names and _MCP_NAME.fullmatch(name):
            return True
        rule = _RULE_SPECIFIER.fullmatch(name)
        # A permission rule reaches only a harness that speaks canonical names natively.
        base = rule["tool"] if rule and tool_names.names.get(rule["tool"]) == (rule["tool"],) else name
        return base in CANONICAL_TOOL_NAMES

    unknown = sorted(name for name in set(names) if not accepted(name))
    if unknown:
        hints = {name: difflib.get_close_matches(name, CANONICAL_TOOL_NAMES, n=1) for name in unknown}
        did_you_mean = "; ".join(f"{name!r} -> did you mean {hint[0]!r}?" for name, hint in hints.items() if hint)
        raise HarnessContractError(
            f"agent.{field} names unknown tool(s) {unknown} for the {kind!r} harness. Accepted names: "
            + f"{sorted(CANONICAL_TOOL_NAMES)} (see docs/agents/HARNESS_PARITY.md)."
            + (f" {did_you_mean}" if did_you_mean else "")
        )
