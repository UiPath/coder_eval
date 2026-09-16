"""The harness contract: the model, registration validation, and every built-in's declaration."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from coder_eval.agents.registry import AgentRegistry
from coder_eval.models import (
    AgentKind,
    BaseAgentConfig,
    ClaudeCodeAgentConfig,
    Enforcement,
    HarnessContract,
    parse_agent_config,
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
