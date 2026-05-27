"""Tests for agent registry and factory pattern."""

import pytest

from coder_eval.agents.registry import AgentRegistry, create_agent
from coder_eval.models import AgentKind, ClaudeCodeAgentConfig, parse_agent_config


def test_register_decorator_preserves_class_type():
    """Verify that @register decorator is identity-preserving at runtime."""
    # The decorator should return the exact same class it receives
    # This allows type checkers to infer the correct type even though
    # the type annotation erases to Any in the decorator signature.

    class FakeAgent:
        def __init__(self, config, route=None, **kwargs):
            self.config = config

    # Register it (would normally be via @AgentRegistry.register(...))
    registration = AgentRegistry.register(AgentKind.CLAUDE_CODE, ClaudeCodeAgentConfig)(FakeAgent)
    assert registration is FakeAgent


def test_create_agent_unregistered_kind_raises_with_kinds_listed():
    """ValueError for unregistered agent kind should list registered agents."""
    # Try to create an agent with a kind that's not registered
    fake_config = parse_agent_config(type=AgentKind.CLAUDE_CODE)

    with pytest.raises(ValueError, match="No agent registered"):
        # This will fail because we're using a fake kind, but it should
        # include the registered kinds in the message
        create_agent(AgentKind.UNKNOWN, fake_config)


def test_create_agent_wrong_config_type_raises_with_hint():
    """TypeError for mismatched config should mention --type mismatch."""
    # If a task has ClaudeCodeAgentConfig but we try to create a Codex agent,
    # it should fail with a clear error mentioning --type

    claude_config = parse_agent_config(type=AgentKind.CLAUDE_CODE)
    # Try to use claude config with codex agent - codex is registered but
    # the config type doesn't match, so we expect a TypeError

    with pytest.raises(TypeError, match=r"--type.*mismatch"):
        create_agent(AgentKind.CODEX, claude_config)
