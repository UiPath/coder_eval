"""The harness contract: the model, registration validation, the resolution check, and ``by_type``."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from coder_eval.agents.registry import AgentRegistry
from coder_eval.models import (
    AgentKind,
    BaseAgentConfig,
    ClaudeCodeAgentConfig,
    Enforcement,
    ExperimentDefaults,
    ExperimentDefinition,
    ExperimentVariant,
    FileExistsCriterion,
    HarnessContract,
    SandboxConfig,
    TaskDefinition,
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

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError, match="timing_basis"):
            HarnessContract(**{**stub_contract().model_dump(), "timing_basis": "wall"})


class _ContractAgent:
    contract = stub_contract()


class TestRegistryValidation:
    def test_missing_contract_rejected(self, restored_registry: None) -> None:
        class NoContractAgent:
            pass

        with pytest.raises(TypeError, match=rf"{KIND}.*NoContractAgent.*HarnessContract"):
            AgentRegistry.register(KIND, config_for_kind(KIND))(NoContractAgent)
        assert AgentRegistry.get(KIND) is None

    def test_dict_contract_rejected(self, restored_registry: None) -> None:
        class DictContractAgent:
            contract = stub_contract().model_dump()

        with pytest.raises(TypeError, match=rf"{KIND}.*DictContractAgent"):
            AgentRegistry.register(KIND, config_for_kind(KIND))(DictContractAgent)

    def test_config_not_a_base_agent_config_rejected(self, restored_registry: None) -> None:
        class ForeignConfig(BaseModel):
            model_config = ConfigDict(extra="forbid")
            type: Literal["contract-test-kind"]

        with pytest.raises(TypeError, match=rf"{KIND}.*ForeignConfig.*BaseAgentConfig"):
            AgentRegistry.register(KIND, ForeignConfig)(_ContractAgent)  # type: ignore[type-var]

    def test_config_without_extra_forbid_rejected(self, restored_registry: None) -> None:
        class LaxConfig(BaseAgentConfig):
            model_config = ConfigDict(extra="ignore")
            type: Literal["contract-test-kind"]  # type: ignore[assignment]

        with pytest.raises(TypeError, match=rf"{KIND}.*LaxConfig.*extra='forbid'"):
            AgentRegistry.register(KIND, LaxConfig)(_ContractAgent)

    def test_type_literal_not_naming_the_kind_rejected(self, restored_registry: None) -> None:
        with pytest.raises(TypeError, match=rf"{KIND}.*ClaudeCodeAgentConfig.*Literal"):
            AgentRegistry.register(KIND, ClaudeCodeAgentConfig)(_ContractAgent)

    def test_type_literal_covering_several_kinds_accepted(self, restored_registry: None) -> None:
        class TwoKindConfig(BaseAgentConfig):
            type: Literal["contract-test-kind", "other-kind"]  # type: ignore[assignment]

        AgentRegistry.register(KIND, TwoKindConfig)(_ContractAgent)
        AgentRegistry.register("other-kind", TwoKindConfig)(_ContractAgent)
        assert AgentRegistry.get("other-kind") is not None

    def test_valid_pair_registers_idempotently(self, restored_registry: None) -> None:
        config = config_for_kind(KIND)
        AgentRegistry.register(KIND, config)(_ContractAgent)
        AgentRegistry.register(KIND, config)(_ContractAgent)
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


def _task(kind: str, **agent_fields: Any) -> TaskDefinition:
    prompt = None if kind == AgentKind.NONE else "do it"
    return TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt=prompt,
        agent=parse_agent_config(type=kind, **agent_fields),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(description="c", path="out.txt")],
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

    def test_explicit_empty_allowlist_is_set(self) -> None:
        with pytest.raises(HarnessContractError, match=r"agent\.allowed_tools"):
            validate_harness_contract(_task(AgentKind.CODEX, allowed_tools=[]))

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
        AgentRegistry.register(KIND, config_for_kind(KIND))(_ContractAgent)
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
