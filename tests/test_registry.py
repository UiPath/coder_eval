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


def test_all_criterion_checkers_accept_context_kwarg():
    """Every registered criterion checker's ``_check_impl`` must accept the
    uniform ``context`` keyword (a CheckContext bundle). This prevents a new
    checker from regressing the uniform signature that replaced the old
    route/reference_dir/proxy kwarg train + inspect-based signature filter.
    """
    import inspect

    from coder_eval.criteria import CriterionRegistry, init_criteria

    init_criteria(validate=False)
    checkers = {t: CriterionRegistry.get_checker(t) for t in CriterionRegistry.list_types()}
    assert checkers, "registry discovered no checkers"
    for criterion_type, checker_cls in checkers.items():
        params = inspect.signature(checker_cls._check_impl).parameters
        assert "context" in params, (
            f"{checker_cls.__name__} (_check_impl for '{criterion_type}') missing 'context' kwarg"
        )
        # No checker should still declare the removed route/reference_dir/proxy kwargs.
        for removed in ("route", "reference_dir", "proxy"):
            assert removed not in params, (
                f"{checker_cls.__name__} still declares removed '{removed}' kwarg; use context.{removed} instead"
            )
