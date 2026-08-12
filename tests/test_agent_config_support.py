"""Tests for per-agent ``BaseAgentConfig`` support declarations and their guard.

A base-config field must mean the same thing on every harness. Where a backend
cannot implement one it declares the divergence on its agent class instead of
dropping the field at runtime; ``validate_config_support`` turns the strictest
declaration (UNHONORED) into a resolution-time error.
"""

from typing import ClassVar

import pytest

from coder_eval.agent import Agent, ConfigFieldSupport, ConfigSupport
from coder_eval.agents.antigravity_agent import AntigravityAgent
from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.agents.codex_agent import CodexAgent
from coder_eval.agents.registry import AgentRegistry
from coder_eval.models import CodexAgentConfig, FileExistsCriterion, TaskDefinition, parse_agent_config
from coder_eval.orchestration.config_support import AgentConfigSupportError, validate_config_support
from coder_eval.plugins import ensure_plugins_loaded


def _task(**agent_kwargs) -> TaskDefinition:
    return TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(**agent_kwargs),
        success_criteria=[FileExistsCriterion(path="x", description="f")],
    )


# --- the declarations themselves ---------------------------------------------------


def test_claude_code_declares_no_divergence():
    """Claude Code is the reference implementation — it honors every shared field."""
    assert ClaudeCodeAgent.config_support == {}


def test_codex_declares_permission_mode_and_disallowed_tools_approximated():
    """Both are forwarded and warned about at start(), so neither is a hard rejection."""
    support = CodexAgent.config_support
    assert support["permission_mode"].support is ConfigSupport.APPROXIMATED
    assert support["disallowed_tools"].support is ConfigSupport.APPROXIMATED
    assert all(note.reason for note in support.values())


def test_antigravity_declares_permission_mode_approximated():
    support = AntigravityAgent.config_support
    assert support["permission_mode"].support is ConfigSupport.APPROXIMATED
    assert "allowed_tools" not in support  # honored since the CapabilitiesConfig wiring


def test_every_declared_field_exists_on_that_agents_config():
    """A declaration naming a field the config lacks is dead text that can never fire."""
    ensure_plugins_loaded()
    for registration in AgentRegistry.registrations():
        fields = registration.config_class.model_fields
        for field in registration.agent_class.config_support:
            assert field in fields, f"{registration.agent_class.__name__} declares unknown field {field!r}"


# --- the resolution guard -----------------------------------------------------------


def test_approximated_field_does_not_raise():
    """bypassPermissions on Codex is approximated, not rejected — the nightly sets it."""
    validate_config_support(_task(type="codex", permission_mode="bypassPermissions"))


def test_no_declarations_is_a_noop():
    validate_config_support(_task(type="claude-code", permission_mode="bypassPermissions"))


def test_task_without_agent_type_is_a_noop():
    validate_config_support(
        TaskDefinition(
            task_id="t",
            description="d",
            initial_prompt="p",
            success_criteria=[FileExistsCriterion(path="x", description="f")],
        )
    )


class _StrictAgent(Agent[CodexAgentConfig]):
    """A synthetic agent that genuinely drops a field, to drive the UNHONORED path.

    No built-in declares UNHONORED today (the whole point of this PR is that the
    known drops were implemented instead), so the reject path needs a stand-in to
    stay covered as rot-protection for the next agent that adds one.
    """

    config_support: ClassVar[dict[str, ConfigFieldSupport]] = {
        "model": ConfigFieldSupport(ConfigSupport.UNHONORED, "pinned to a fixed model by the vendor"),
    }

    async def start(self, working_directory, *, env_path_prepend=None, plugin_tools_dir=None) -> None: ...

    async def communicate(  # type: ignore[empty-body]
        self, user_input, *, stream_callback=None, timeout=None, max_turns=None, should_stop=None
    ): ...

    async def stop(self) -> None: ...


@pytest.fixture
def strict_codex():
    """Bind ``_StrictAgent`` to the ``codex`` kind for one test, then restore.

    Rebinding the existing kind (rather than adding a new one) lets the tests build
    tasks through the normal ``type: codex`` path — ``_StrictAgent`` deliberately
    reuses ``CodexAgentConfig``, so nothing about resolution changes except which
    ``config_support`` map the guard reads.
    """
    ensure_plugins_loaded()
    saved = dict(AgentRegistry._registry)
    AgentRegistry._registry["codex"] = type(saved["codex"])(agent_class=_StrictAgent, config_class=CodexAgentConfig)
    yield
    AgentRegistry._registry.clear()
    AgentRegistry._registry.update(saved)


def test_unhonored_field_set_to_non_default_raises(strict_codex):
    with pytest.raises(AgentConfigSupportError, match="does not implement"):
        validate_config_support(_task(type="codex", model="gpt-5.5"))


def test_unhonored_field_left_at_default_does_not_raise(strict_codex):
    """A field the five-layer merge never touched carries no author intent."""
    validate_config_support(_task(type="codex"))


def test_error_names_the_field_and_the_reason(strict_codex):
    with pytest.raises(AgentConfigSupportError) as exc:
        validate_config_support(_task(type="codex", model="gpt-5.5"))

    assert "agent.model" in str(exc.value)
    assert "pinned to a fixed model by the vendor" in str(exc.value)
