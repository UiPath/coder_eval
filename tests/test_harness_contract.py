"""The harness contract: the model, registration validation, the resolution check, and ``by_type``."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from coder_eval.agents.pi_agent import PiAgent
from coder_eval.agents.registry import SPI_VERSION, AgentRegistry
from coder_eval.models import (
    CANONICAL_TOOL_NAMES,
    READ_ONLY_DENIED_TOOLS,
    AgentKind,
    BaseAgentConfig,
    ClaudeCodeAgentConfig,
    Enforcement,
    ExperimentDefaults,
    ExperimentDefinition,
    ExperimentVariant,
    FileExistsCriterion,
    HarnessContract,
    PermissionMode,
    RunLimits,
    SandboxConfig,
    TaskDefinition,
    ToolNameMap,
    UsageGranularity,
    parse_agent_config,
)
from coder_eval.orchestration.config import BatchRunConfig
from coder_eval.orchestration.config_merge import MergeError
from coder_eval.orchestration.experiment import (
    DEFAULT_EXPERIMENT_PATH,
    _apply_cli_overrides,
    load_experiment,
    resolve_task_for_variant,
)
from coder_eval.orchestration.harness_contract import (
    MODEL_TURN_LIMITS,
    HarnessContractError,
    TaskResolutionError,
    validate_harness_contract,
)
from coder_eval.plugins import ensure_plugins_loaded
from tests.fixtures.harness_stubs import config_for_kind, stub_contract


KIND = "contract-test-kind"


@pytest.fixture
def restored_registry() -> Iterator[None]:
    saved = dict(AgentRegistry._registry)
    try:
        yield
    finally:
        AgentRegistry._registry.clear()
        AgentRegistry._registry.update(saved)


class TestModel:
    def test_enforced_prompt_requires_semantics(self) -> None:
        with pytest.raises(ValidationError, match="system_prompt_semantics"):
            HarnessContract(**{**stub_contract().model_dump(), "system_prompt_semantics": None})

    def test_unsupported_prompt_rejects_semantics(self) -> None:
        with pytest.raises(ValidationError, match="system_prompt_semantics"):
            HarnessContract(**{**stub_contract().model_dump(), "system_prompt": Enforcement.UNSUPPORTED})

    def test_unsupported_prompt_without_semantics_is_valid(self) -> None:
        contract = HarnessContract(
            **{**stub_contract().model_dump(), "system_prompt": "unsupported", "system_prompt_semantics": None}
        )
        assert contract.system_prompt is Enforcement.UNSUPPORTED

    def test_contract_is_frozen(self) -> None:
        contract = stub_contract()
        with pytest.raises(ValidationError):
            contract.cooperative_stop = False  # type: ignore[misc]

    def test_usage_granularity_is_required(self) -> None:
        fields = stub_contract().model_dump()
        del fields["usage_granularity"]
        with pytest.raises(ValidationError, match="usage_granularity"):
            HarnessContract(**fields)

    def test_timing_basis_is_required(self) -> None:
        fields = stub_contract().model_dump()
        del fields["timing_basis"]
        with pytest.raises(ValidationError, match="timing_basis"):
            HarnessContract(**fields)

    def test_unknown_timing_basis_rejected(self) -> None:
        with pytest.raises(ValidationError, match="timing_basis"):
            HarnessContract(**{**stub_contract().model_dump(), "timing_basis": "wall"})

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError, match="clock_basis"):
            HarnessContract(**{**stub_contract().model_dump(), "clock_basis": "wall"})


class TestPermissionModes:
    def _contract(self, **fields: Any) -> HarnessContract:
        return HarnessContract(**{**stub_contract().model_dump(), **fields})

    def test_enforced_permission_mode_requires_a_value_set(self) -> None:
        with pytest.raises(ValidationError, match="permission_modes"):
            self._contract(permission_mode="enforced")

    def test_unsupported_permission_mode_rejects_a_value_set(self) -> None:
        with pytest.raises(ValidationError, match="permission_modes"):
            self._contract(permission_modes={PermissionMode.PLAN})

    def test_empty_value_set_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="permission_modes"):
            self._contract(permission_mode="enforced", permission_modes=set())

    def test_value_set_dumps_sorted(self) -> None:
        contract = self._contract(
            permission_mode="enforced", permission_modes={PermissionMode.PLAN, PermissionMode.BYPASS_PERMISSIONS}
        )
        assert contract.model_dump(mode="json")["permission_modes"] == ["bypassPermissions", "plan"]

    def test_only_claude_code_honors_default_or_accept_edits(self) -> None:
        ensure_plugins_loaded()
        for kind in (k for k in AgentKind if k is not AgentKind.UNKNOWN):
            registration = AgentRegistry.get(kind)
            assert registration is not None
            modes = registration.agent_class.contract.permission_modes or frozenset()
            if kind is not AgentKind.CLAUDE_CODE:
                assert not modes & {PermissionMode.DEFAULT, PermissionMode.ACCEPT_EDITS}, kind


def _identity_names() -> dict[str, tuple[str, ...]]:
    return {name: (name,) for name in CANONICAL_TOOL_NAMES}


class TestToolNameMap:
    def test_missing_canonical_name_is_rejected(self) -> None:
        names = _identity_names()
        del names["Bash"]
        with pytest.raises(ValidationError, match="missing=\\['Bash'\\]"):
            ToolNameMap(names=names)

    def test_extra_name_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="extra=\\['LS'\\]"):
            ToolNameMap(names={**_identity_names(), "LS": ("ls",)})

    def test_from_inverse_rejects_a_name_both_mapped_and_absent(self) -> None:
        forward = {name.lower(): name for name in CANONICAL_TOOL_NAMES}
        with pytest.raises(ValueError, match="in both: \\['Bash'\\]"):
            ToolNameMap.from_inverse(forward, no_equivalent=frozenset({"Bash"}))

    def test_from_inverse_rejects_a_gap(self) -> None:
        forward = {name.lower(): name for name in CANONICAL_TOOL_NAMES - {"Skill"}}
        with pytest.raises(ValueError, match="in neither: \\['Skill'\\]"):
            ToolNameMap.from_inverse(forward, no_equivalent=frozenset())

    def test_from_inverse_drops_telemetry_only_names_and_groups_natives(self) -> None:
        forward = {name.lower(): name for name in CANONICAL_TOOL_NAMES - {"Edit"}}
        forward |= {"patch": "Edit", "edit": "Edit", "ls": "LS"}
        tool_names = ToolNameMap.from_inverse(forward, no_equivalent=frozenset())
        assert tool_names.names["Edit"] == ("edit", "patch")
        assert "LS" not in tool_names.names

    def test_no_equivalent_maps_to_an_empty_tuple(self) -> None:
        forward = {name.lower(): name for name in CANONICAL_TOOL_NAMES - {"Skill"}}
        assert ToolNameMap.from_inverse(forward, no_equivalent=frozenset({"Skill"})).names["Skill"] == ()

    def test_an_alias_shares_its_targets_natives(self) -> None:
        forward = {name.lower(): name for name in CANONICAL_TOOL_NAMES - {"Task"}}
        assert ToolNameMap.from_inverse(forward, no_equivalent=frozenset()).names["Task"] == ("agent",)

    def test_claude_code_honors_every_mode_and_names_tools_natively(self) -> None:
        from coder_eval.agents.claude_code_agent import ClaudeCodeAgent

        assert ClaudeCodeAgent.contract.permission_modes == frozenset(PermissionMode)
        assert ClaudeCodeAgent.tool_names is not None and ClaudeCodeAgent.tool_names.mcp_names is True
        assert all(natives == (name,) for name, natives in ClaudeCodeAgent.tool_names.names.items())

    def test_read_only_denied_tools_are_canonical(self) -> None:
        assert set(READ_ONLY_DENIED_TOOLS) <= CANONICAL_TOOL_NAMES


class _ContractAgent:
    contract = stub_contract()


class TestRegistryValidation:
    def test_missing_contract_rejected(self, restored_registry: None) -> None:
        class NoContractAgent:
            pass

        with pytest.raises(TypeError, match=rf"{KIND}.*NoContractAgent.*HarnessContract"):
            AgentRegistry.register(KIND, config_for_kind(KIND), spi_version=SPI_VERSION)(NoContractAgent)
        assert AgentRegistry.get(KIND) is None

    def test_dict_contract_rejected(self, restored_registry: None) -> None:
        class DictContractAgent:
            contract = stub_contract().model_dump()

        with pytest.raises(TypeError, match=rf"{KIND}.*DictContractAgent"):
            AgentRegistry.register(KIND, config_for_kind(KIND), spi_version=SPI_VERSION)(DictContractAgent)

    def test_config_not_a_base_agent_config_rejected(self, restored_registry: None) -> None:
        class ForeignConfig(BaseModel):
            model_config = ConfigDict(extra="forbid")
            type: Literal["contract-test-kind"]

        with pytest.raises(TypeError, match=rf"{KIND}.*ForeignConfig.*BaseAgentConfig"):
            AgentRegistry.register(KIND, ForeignConfig, spi_version=SPI_VERSION)(_ContractAgent)  # type: ignore[type-var]

    def test_config_without_extra_forbid_rejected(self, restored_registry: None) -> None:
        class LaxConfig(BaseAgentConfig):
            model_config = ConfigDict(extra="ignore")
            type: Literal["contract-test-kind"]  # type: ignore[assignment]

        with pytest.raises(TypeError, match=rf"{KIND}.*LaxConfig.*extra='forbid'"):
            AgentRegistry.register(KIND, LaxConfig, spi_version=SPI_VERSION)(_ContractAgent)

    def test_type_literal_not_naming_the_kind_rejected(self, restored_registry: None) -> None:
        with pytest.raises(TypeError, match=rf"{KIND}.*ClaudeCodeAgentConfig.*Literal"):
            AgentRegistry.register(KIND, ClaudeCodeAgentConfig, spi_version=SPI_VERSION)(_ContractAgent)

    def test_type_literal_covering_several_kinds_accepted(self, restored_registry: None) -> None:
        class TwoKindConfig(BaseAgentConfig):
            type: Literal["contract-test-kind", "other-kind"]  # type: ignore[assignment]

        AgentRegistry.register(KIND, TwoKindConfig, spi_version=SPI_VERSION)(_ContractAgent)
        AgentRegistry.register("other-kind", TwoKindConfig, spi_version=SPI_VERSION)(_ContractAgent)
        assert AgentRegistry.get("other-kind") is not None

    def test_enforced_tool_lists_require_tool_names(self, restored_registry: None) -> None:
        class NoMapAgent:
            contract = HarnessContract(**{**stub_contract().model_dump(), "allowed_tools": "enforced"})

        with pytest.raises(TypeError, match=rf"{KIND}.*NoMapAgent.*tool_names"):
            AgentRegistry.register(KIND, config_for_kind(KIND), spi_version=SPI_VERSION)(NoMapAgent)

    def test_unsupported_tool_lists_reject_tool_names(self, restored_registry: None) -> None:
        class StrayMapAgent:
            contract = stub_contract()
            tool_names = ToolNameMap(names=_identity_names())

        with pytest.raises(TypeError, match=rf"{KIND}.*StrayMapAgent.*tool_names"):
            AgentRegistry.register(KIND, config_for_kind(KIND), spi_version=SPI_VERSION)(StrayMapAgent)

    def test_valid_pair_registers_idempotently(self, restored_registry: None) -> None:
        config = config_for_kind(KIND)
        AgentRegistry.register(KIND, config, spi_version=SPI_VERSION)(_ContractAgent)
        AgentRegistry.register(KIND, config, spi_version=SPI_VERSION)(_ContractAgent)
        registration = AgentRegistry.get(KIND)
        assert registration is not None and registration.agent_class is _ContractAgent


@pytest.mark.parametrize("kind", [k for k in AgentKind if k is not AgentKind.UNKNOWN])
def test_every_builtin_declares_a_contract(kind: AgentKind) -> None:
    ensure_plugins_loaded()
    registration = AgentRegistry.get(kind)
    assert registration is not None
    assert isinstance(registration.agent_class.contract, HarnessContract)


@pytest.mark.parametrize("kind", [k for k in AgentKind if k is not AgentKind.UNKNOWN])
def test_every_builtin_accepts_cost_log_tags(kind: AgentKind) -> None:
    ensure_plugins_loaded()
    registration = AgentRegistry.get(kind)
    assert registration is not None
    tags = {"x-ce-run-id": "r"}
    agent = registration.agent_class(parse_agent_config(type=kind), cost_log_tags=tags)
    assert agent.cost_log_tags == tags


def _task(kind: str, *, run_limits: RunLimits | None = None, **agent_fields: Any) -> TaskDefinition:
    prompt = None if kind == AgentKind.NONE else "do it"
    return TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt=prompt,
        agent=parse_agent_config(type=kind, **agent_fields),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(description="c", path="out.txt")],
        run_limits=run_limits,
    )


_GATED_VALUES: dict[str, Any] = {
    "system_prompt": "be terse",
    "plugins": [{"type": "local", "path": "/plugins/p"}],
    "permission_mode": "plan",
    "allowed_tools": ["Bash"],
    "disallowed_tools": ["Bash"],
}


class TestValidateHarnessContract:
    @pytest.mark.parametrize("field", list(_GATED_VALUES))
    def test_unsupported_field_is_rejected(self, field: str) -> None:
        with pytest.raises(HarnessContractError) as exc:
            validate_harness_contract(_task(AgentKind.NONE, **{field: _GATED_VALUES[field]}))
        message = str(exc.value)
        assert f"agent.{field}" in message
        assert "'none'" in message
        assert "docs/agents/HARNESS_PARITY.md" in message
        assert "claude-code" in message.split("honors it", 1)[1]

    @pytest.mark.parametrize("field", list(_GATED_VALUES))
    def test_enforced_field_is_accepted(self, field: str) -> None:
        validate_harness_contract(_task(AgentKind.CLAUDE_CODE, **{field: _GATED_VALUES[field]}))

    def test_the_error_is_a_task_resolution_error(self) -> None:
        with pytest.raises(TaskResolutionError):
            validate_harness_contract(_task(AgentKind.CODEX, permission_mode="acceptEdits"))

    def test_ungated_field_passes_on_a_harness_that_supports_nothing(self) -> None:
        validate_harness_contract(_task(AgentKind.NONE, model="m", ignore_patterns=["*.log"]))

    def test_unset_default_permission_mode_passes(self) -> None:
        task = _task(AgentKind.CODEX)
        assert "permission_mode" not in task.agent.model_fields_set  # type: ignore[union-attr]
        validate_harness_contract(task)

    @pytest.mark.parametrize("field", ["allowed_tools", "disallowed_tools"])
    def test_an_empty_tool_list_restricts_nothing_so_it_is_not_set(self, field: str) -> None:
        validate_harness_contract(_task(AgentKind.CODEX, **{field: []}))

    def test_null_plugins_from_default_is_not_set(self) -> None:
        default = ExperimentDefinition(
            experiment_id="default",
            defaults=ExperimentDefaults(agent={"type": "claude-code", "plugins": None}),
            variants=[ExperimentVariant(variant_id="default")],
        )
        resolved, _, _ = resolve_task_for_variant(default, _task(AgentKind.CODEX), default, default.variants[0])
        assert resolved.agent is not None and "plugins" in resolved.agent.model_fields_set
        validate_harness_contract(resolved)

    def test_task_without_agent_type_is_left_to_the_type_guard(self) -> None:
        validate_harness_contract(_task(AgentKind.CODEX).model_copy(update={"agent": None}))

    def test_unregistered_kind_is_rejected(self, restored_registry: None) -> None:
        AgentRegistry.register(KIND, config_for_kind(KIND), spi_version=SPI_VERSION)(_ContractAgent)
        task = TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="do it",
            agent={"type": KIND},
            success_criteria=[FileExistsCriterion(description="c", path="out.txt")],
        )
        AgentRegistry._registry.pop(KIND)
        with pytest.raises(HarnessContractError, match="not registered"):
            validate_harness_contract(task)


class TestModelTurnLimits:
    @pytest.mark.parametrize("cooperative_stop", [True, False])
    @pytest.mark.parametrize("granularity", list(UsageGranularity))
    def test_counts_model_turns_follows_usage_granularity_and_cooperative_stop(
        self, cooperative_stop: bool, granularity: UsageGranularity
    ) -> None:
        contract = stub_contract(cooperative_stop=cooperative_stop).model_copy(
            update={"usage_granularity": granularity}
        )
        assert contract.counts_model_turns is (cooperative_stop and granularity is not UsageGranularity.TURN)

    @pytest.mark.parametrize(
        ("kind", "accepted"),
        [
            (AgentKind.CLAUDE_CODE, True),
            (AgentKind.OPENCODE, True),
            (AgentKind.PI, True),
            (AgentKind.CODEX, False),
            (AgentKind.ANTIGRAVITY, False),
            (AgentKind.NONE, False),
        ],
    )
    @pytest.mark.parametrize("field", MODEL_TURN_LIMITS)
    def test_model_turn_limit_gate(self, kind: AgentKind, accepted: bool, field: str) -> None:
        task = _task(kind, run_limits=RunLimits.model_validate({field: 3}))
        if accepted:
            validate_harness_contract(task)
            return
        with pytest.raises(HarnessContractError) as exc:
            validate_harness_contract(task)
        message = str(exc.value)
        assert f"run_limits.{field}" in message
        assert f"{kind.value!r}" in message
        assert "docs/agents/HARNESS_PARITY.md" in message
        assert "claude-code" in message.split("counts model turns", 1)[1]

    def test_unset_model_turn_limits_pass_everywhere(self) -> None:
        validate_harness_contract(_task(AgentKind.CODEX, run_limits=RunLimits()))


class TestMaxUsdPriceable:
    @pytest.mark.parametrize(
        ("kind", "reports_cost"),
        [
            (AgentKind.CLAUDE_CODE, True),
            (AgentKind.NONE, True),
            (AgentKind.CODEX, False),
            (AgentKind.PI, False),
            (AgentKind.OPENCODE, False),
            (AgentKind.ANTIGRAVITY, False),
        ],
    )
    def test_reports_cost_per_builtin(self, kind: AgentKind, reports_cost: bool) -> None:
        registration = AgentRegistry.get(kind.value)
        assert registration is not None
        assert registration.agent_class.contract.reports_cost is reports_cost

    @pytest.mark.parametrize("model", [None, "provider/not-on-the-card"])
    @pytest.mark.parametrize("kind", [AgentKind.CODEX, AgentKind.PI, AgentKind.OPENCODE, AgentKind.ANTIGRAVITY])
    def test_an_unpriced_model_is_rejected_where_the_harness_reports_no_cost(
        self, kind: AgentKind, model: str | None
    ) -> None:
        task = _task(kind, run_limits=RunLimits(max_usd=1.0), model=model)
        with pytest.raises(HarnessContractError, match=r"run_limits\.max_usd is set") as exc:
            validate_harness_contract(task)
        assert "claude-code" in str(exc.value)

    def test_a_priced_model_is_accepted_where_the_harness_reports_no_cost(self) -> None:
        validate_harness_contract(_task(AgentKind.CODEX, run_limits=RunLimits(max_usd=1.0), model="gpt-5-codex"))

    @pytest.mark.parametrize("model", [None, "provider/not-on-the-card"])
    def test_a_harness_that_reports_cost_needs_no_rate(self, model: str | None) -> None:
        validate_harness_contract(_task(AgentKind.CLAUDE_CODE, run_limits=RunLimits(max_usd=1.0), model=model))

    def test_no_max_usd_needs_no_rate(self) -> None:
        validate_harness_contract(_task(AgentKind.CODEX, run_limits=RunLimits(max_output_tokens=10)))


_NON_CLAUDE_ENFORCING = [AgentKind.PI, AgentKind.OPENCODE, AgentKind.ANTIGRAVITY]


class TestValueChecks:
    @pytest.mark.parametrize("kind", _NON_CLAUDE_ENFORCING)
    @pytest.mark.parametrize("mode", ["acceptEdits", "default"])
    def test_claude_only_modes_are_rejected(self, kind: AgentKind, mode: str) -> None:
        with pytest.raises(HarnessContractError) as exc:
            validate_harness_contract(_task(kind, permission_mode=mode))
        message = str(exc.value)
        assert f"agent.permission_mode={mode!r}" in message
        assert "['bypassPermissions', 'plan']" in message
        assert "claude-code" in message.split("honors it", 1)[1]

    @pytest.mark.parametrize("kind", _NON_CLAUDE_ENFORCING)
    @pytest.mark.parametrize("mode", ["plan", "bypassPermissions"])
    def test_declared_modes_are_accepted(self, kind: AgentKind, mode: str) -> None:
        validate_harness_contract(_task(kind, permission_mode=mode))

    @pytest.mark.parametrize("mode", list(PermissionMode))
    def test_claude_code_accepts_every_mode(self, mode: PermissionMode) -> None:
        validate_harness_contract(_task(AgentKind.CLAUDE_CODE, permission_mode=mode))

    @pytest.mark.parametrize("field", ["allowed_tools", "disallowed_tools"])
    def test_unknown_tool_name_is_rejected_with_a_suggestion(self, field: str) -> None:
        with pytest.raises(HarnessContractError) as exc:
            validate_harness_contract(_task(AgentKind.PI, **{field: ["Read", "Bassh"]}))
        message = str(exc.value)
        assert f"agent.{field} names unknown tool(s) ['Bassh']" in message
        assert "did you mean 'Bash'?" in message

    @pytest.mark.parametrize("name", ["mcp__github__create_issue", "mcp__my_server__do_it", "mcp__n8n-mcp"])
    def test_mcp_name_is_accepted_only_where_the_harness_addresses_it(self, name: str) -> None:
        validate_harness_contract(_task(AgentKind.CLAUDE_CODE, allowed_tools=[name]))
        with pytest.raises(HarnessContractError, match=re.escape(name)):
            validate_harness_contract(_task(AgentKind.PI, allowed_tools=[name]))

    def test_malformed_mcp_name_is_rejected(self) -> None:
        with pytest.raises(HarnessContractError, match="mcp____x"):
            validate_harness_contract(_task(AgentKind.CLAUDE_CODE, allowed_tools=["mcp____x"]))

    @pytest.mark.parametrize("rule", ["Bash(git status:*)", "Read(./src/**)"])
    def test_permission_rule_syntax_is_accepted_only_on_claude_code(self, rule: str) -> None:
        validate_harness_contract(_task(AgentKind.CLAUDE_CODE, allowed_tools=[rule]))
        with pytest.raises(HarnessContractError, match="unknown tool"):
            validate_harness_contract(_task(AgentKind.PI, allowed_tools=[rule]))

    @pytest.mark.parametrize("kind", [AgentKind.OPENCODE, AgentKind.ANTIGRAVITY, AgentKind.CLAUDE_CODE])
    def test_unknown_name_is_rejected_on_every_enforcing_harness(self, kind: AgentKind) -> None:
        with pytest.raises(HarnessContractError, match=r"unknown tool\(s\) \['LS'\]"):
            validate_harness_contract(_task(kind, disallowed_tools=["LS"]))

    def test_a_name_the_harness_lacks_is_accepted(self) -> None:
        assert PiAgent.tool_names is not None and PiAgent.tool_names.names["Skill"] == ()
        validate_harness_contract(_task(AgentKind.PI, allowed_tools=["Skill"]))

    def test_an_unset_default_mode_is_not_checked(self) -> None:
        validate_harness_contract(_task(AgentKind.PI))

    def test_a_task_level_claude_mode_is_rejected_under_a_cli_kind(self) -> None:
        resolved, _ = _resolve(_default_experiment(), _bare_task(permission_mode="acceptEdits"), agent_type="pi")
        with pytest.raises(HarnessContractError, match="permission_mode='acceptEdits'"):
            validate_harness_contract(resolved)


def _default_experiment(**by_type: dict[str, Any]) -> ExperimentDefinition:
    agent: dict[str, Any] = {"type": "claude-code", "plugins": None}
    if by_type:
        agent["by_type"] = by_type
    return ExperimentDefinition(
        experiment_id="default",
        defaults=ExperimentDefaults(agent=agent),
        variants=[ExperimentVariant(variant_id="default")],
    )


def _resolve(
    default: ExperimentDefinition,
    task: TaskDefinition,
    experiment: ExperimentDefinition | None = None,
    **config: Any,
) -> tuple[TaskDefinition, dict[str, Any]]:
    experiment = experiment or ExperimentDefinition(experiment_id="exp", variants=[ExperimentVariant(variant_id="v")])
    batch = BatchRunConfig(run_dir=Path("."), **config)
    resolved, lineage, _ = resolve_task_for_variant(default, task, experiment, experiment.variants[0], batch)
    _apply_cli_overrides(resolved, batch, lineage)
    return resolved, lineage


def _bare_task(**agent: Any) -> TaskDefinition:
    return TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="do it",
        agent=agent or None,
        success_criteria=[FileExistsCriterion(description="c", path="out.txt")],
    )


class TestByType:
    def test_default_by_type_reaches_the_matching_kind_with_lineage(self) -> None:
        default = _default_experiment(**{"claude-code": {"model": "claude-sonnet-4-6"}})
        resolved, lineage = _resolve(default, _bare_task())
        assert resolved.agent is not None and resolved.agent.model == "claude-sonnet-4-6"
        assert lineage["agent.model"].source == "default"
        assert lineage["agent.model"].source_detail == "by_type.claude-code"

    def test_cli_type_does_not_inherit_another_kinds_entry(self) -> None:
        default = _default_experiment(**{"claude-code": {"model": "claude-sonnet-4-6", "permission_mode": "plan"}})
        resolved, _ = _resolve(default, _bare_task(), agent_type="pi")
        assert resolved.agent is not None and str(resolved.agent.type) == "pi"
        assert resolved.agent.model is None
        assert "permission_mode" not in resolved.agent.model_fields_set

    def test_cli_kind_selects_the_entry_over_the_task_type(self) -> None:
        default = _default_experiment(pi={"model": "m-pi"}, codex={"model": "m-codex"})
        resolved, _ = _resolve(default, _bare_task(type="pi"), agent_type="codex")
        assert resolved.agent is not None and resolved.agent.model == "m-codex"

    def test_explicit_dash_d_type_beats_dash_dash_type(self) -> None:
        default = _default_experiment(pi={"model": "m-pi"}, codex={"model": "m-codex"})
        resolved, _ = _resolve(default, _bare_task(), agent_type="codex", overrides={"agent.type": "pi"})
        assert resolved.agent is not None and resolved.agent.model == "m-pi"

    def test_cli_kind_entry_may_carry_fields_only_that_kind_declares(self) -> None:
        default = _default_experiment(pi={"thinking_level": "high"})
        resolved, lineage = _resolve(default, _bare_task(), agent_type="pi")
        assert resolved.agent is not None and resolved.agent.thinking_level == "high"  # type: ignore[attr-defined]
        assert lineage["agent.type"].source_detail == "--type"

    def test_cli_kind_drops_nothing_from_a_task_of_another_kind(self) -> None:
        default = _default_experiment(**{"claude-code": {"sdk_options": {"effort": "high"}}})
        resolved, _ = _resolve(default, _bare_task(type="pi"), agent_type="claude-code")
        assert resolved.agent is not None and resolved.agent.sdk_options == {"effort": "high"}  # type: ignore[attr-defined]

    def test_task_value_beats_by_type(self) -> None:
        default = _default_experiment(**{"claude-code": {"model": "claude-sonnet-4-6"}})
        resolved, lineage = _resolve(default, _bare_task(type="claude-code", model="task-model"))
        assert resolved.agent is not None and resolved.agent.model == "task-model"
        assert lineage["agent.model"].source == "task"

    def test_experiment_by_type_beats_default_by_type(self) -> None:
        default = _default_experiment(**{"claude-code": {"model": "default-model"}})
        experiment = ExperimentDefinition(
            experiment_id="exp",
            defaults=ExperimentDefaults(agent={"by_type": {"claude-code": {"model": "exp-model"}}}),
            variants=[ExperimentVariant(variant_id="v")],
        )
        resolved, lineage = _resolve(default, _bare_task(), experiment)
        assert resolved.agent is not None and resolved.agent.model == "exp-model"
        assert lineage["agent.model"].source == "experiment-defaults"
        assert lineage["agent.model"].source_detail == "by_type.claude-code"

    def test_unregistered_kind_is_tolerated(self) -> None:
        default = _default_experiment(**{"not-installed": {"model": "x"}, "claude-code": {"model": "m"}})
        resolved, _ = _resolve(default, _bare_task())
        assert resolved.agent is not None and resolved.agent.model == "m"

    @pytest.mark.parametrize("by_type", ["pi", {"pi": "not-a-mapping"}])
    def test_non_mapping_is_rejected(self, by_type: Any) -> None:
        default = _default_experiment()
        assert default.defaults is not None and default.defaults.agent is not None
        default.defaults.agent["by_type"] = by_type
        with pytest.raises(ValueError, match=r"default agent\.by_type must map"):
            _resolve(default, _bare_task())

    def test_entry_setting_type_is_rejected(self) -> None:
        default = _default_experiment(pi={"type": "codex"})
        with pytest.raises(ValueError, match=r"by_type\.pi must not set 'type'"):
            _resolve(default, _bare_task())

    def test_by_type_on_a_task_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="by_type"):
            _bare_task(type="codex", by_type={"codex": {"model": "m"}})

    def test_by_type_on_a_variant_is_rejected(self) -> None:
        default = _default_experiment()
        experiment = ExperimentDefinition(
            experiment_id="exp",
            variants=[ExperimentVariant(variant_id="v", agent={"by_type": {"codex": {"model": "m"}}})],
        )
        with pytest.raises(MergeError, match="by_type"):
            _resolve(default, _bare_task(), experiment)

    def test_the_shipped_default_experiment_resolves_every_builtin_kind(self) -> None:
        default = load_experiment(DEFAULT_EXPERIMENT_PATH)
        for kind in (k for k in AgentKind if k not in (AgentKind.UNKNOWN, AgentKind.NONE)):
            resolved, _ = _resolve(default, _bare_task(), agent_type=str(kind))
            validate_harness_contract(resolved)


_PARITY_FIXTURE_DIRS = ("tasks/run_limits", "tasks/skills")


def _contract_of(kind: AgentKind) -> HarnessContract:
    registration = AgentRegistry.get(kind)
    assert registration is not None
    return registration.agent_class.contract


def _cooperative_kinds() -> list[AgentKind]:
    ensure_plugins_loaded()
    kinds = [kind for kind in AgentKind if kind is not AgentKind.UNKNOWN]
    return [
        kind
        for kind in kinds
        if (reg := AgentRegistry.get(kind)) is not None and reg.agent_class.contract.cooperative_stop
    ]


@pytest.mark.parametrize(
    "fixture",
    sorted(str(p) for d in _PARITY_FIXTURE_DIRS for p in (Path(__file__).parent.parent / d).glob("*.yaml")),
    ids=lambda p: Path(p).name,
)
def test_multi_harness_fixtures_run_on_every_cooperative_harness(fixture: str) -> None:
    """A fixture documented to run with `--type <kind>` must not carry a field one harness rejects.

    A fixture that sets a model-turn limit runs on the harnesses that count model turns, and the rest reject it.
    """
    from coder_eval.orchestration.task_loader import load_task

    task, _source = load_task(Path(fixture))
    authored = task.agent.model_dump(exclude_unset=True, exclude={"type"}) if task.agent is not None else {}
    counts_turns_only = task.run_limits is not None and any(
        getattr(task.run_limits, field) is not None for field in MODEL_TURN_LIMITS
    )
    for kind in _cooperative_kinds():
        retyped = task.model_copy(update={"agent": parse_agent_config(type=kind, **authored)})
        if counts_turns_only and not _contract_of(kind).counts_model_turns:
            with pytest.raises(HarnessContractError, match=r"run_limits\."):
                validate_harness_contract(retyped)
        else:
            validate_harness_contract(retyped)
