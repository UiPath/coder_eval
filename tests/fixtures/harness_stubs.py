"""Registration-valid building blocks for tests that register a throwaway agent kind."""

from __future__ import annotations

from typing import Literal

from pydantic import create_model

from coder_eval.models import BaseAgentConfig, Enforcement, HarnessContract, UsageGranularity


def stub_contract(*, cooperative_stop: bool = True) -> HarnessContract:
    """A contract that honors the system prompt and skills only, so it needs no ``tool_names``."""
    return HarnessContract(
        system_prompt=Enforcement.ENFORCED,
        system_prompt_semantics="append",
        plugin_skills=Enforcement.ENFORCED,
        permission_mode=Enforcement.UNSUPPORTED,
        allowed_tools=Enforcement.UNSUPPORTED,
        disallowed_tools=Enforcement.UNSUPPORTED,
        cooperative_stop=cooperative_stop,
        usage_granularity=UsageGranularity.TURN,
    )


def config_for_kind[C: BaseAgentConfig](kind: str, base: type[C] = BaseAgentConfig) -> type[C]:
    """A ``base`` subclass whose ``type`` Literal names ``kind``, as registration requires."""
    return create_model(f"StubConfig_{kind.replace('-', '_')}", __base__=base, type=(Literal[kind], ...))  # type: ignore[valid-type]
