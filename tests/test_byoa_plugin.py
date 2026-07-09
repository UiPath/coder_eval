"""Offline end-to-end test for the BYOA plugin SPI.

Uses the out-of-tree fixture plugin in ``tests/fixtures/byoa_demo_plugin`` to
prove a third-party agent flows through discovery -> task validation -> config
merge -> ``create_agent`` with zero edits to ``coder_eval``. Does NOT hit any
API (it never calls ``agent.start()``); the real-API path is covered by
``tests/test_byoa_plugin_live.py``.
"""

import importlib.metadata
import sys
from pathlib import Path

import pytest

import coder_eval.plugins as plugins
from coder_eval.agents.registry import AgentRegistry, create_agent
from coder_eval.models import TaskDefinition, parse_agent_config
from coder_eval.orchestration.config_merge import MergeError, validate_paths


_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "byoa_demo_plugin"
CRIT = [{"type": "file_exists", "path": "out.txt", "description": "out exists"}]


class _FakeEntryPoint:
    name = "byoa_demo"
    value = "byoa_demo:register"

    def load(self):
        import byoa_demo

        return byoa_demo.register


@pytest.fixture
def discovered_demo_plugin(monkeypatch):
    """Put the fixture package on sys.path and discover it via the entry-point path."""
    monkeypatch.syspath_prepend(str(_FIXTURE_DIR))
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda *, group: [_FakeEntryPoint()])
    saved_loaded = plugins._loaded
    saved_registry = dict(AgentRegistry._registry)
    plugins._loaded = False
    plugins.load_plugins(force=True)
    try:
        yield
    finally:
        plugins._loaded = saved_loaded
        AgentRegistry._registry.clear()
        AgentRegistry._registry.update(saved_registry)
        sys.modules.pop("byoa_demo", None)


def test_plugin_kind_is_discovered(discovered_demo_plugin):
    assert "byoa-demo" in AgentRegistry.list_kinds()


def test_task_yaml_accepts_plugin_kind_and_custom_field(discovered_demo_plugin):
    task = TaskDefinition(
        task_id="t1",
        description="d",
        initial_prompt="hi",
        agent={"type": "byoa-demo", "demo_label": "custom", "model": "claude-sonnet-4-6"},
        success_criteria=CRIT,
    )
    import byoa_demo

    assert type(task.agent) is byoa_demo.DemoAgentConfig
    assert task.agent.demo_label == "custom"


def test_cli_override_validates_against_plugin_config(discovered_demo_plugin):
    # -D agent.demo_label is a plugin-only field; the registry-driven merge knows it.
    validate_paths(["agent.demo_label", "agent.model"])  # no raise
    with pytest.raises(MergeError) as exc:
        validate_paths(["agent.demo_labl"])  # typo
    assert exc.value.suggestion == "demo_label"


def test_create_agent_instantiates_plugin_agent(discovered_demo_plugin):
    import byoa_demo

    cfg = parse_agent_config(type="byoa-demo", demo_label="x")
    agent = create_agent("byoa-demo", cfg)
    assert isinstance(agent, byoa_demo.DemoAgent)


def test_create_agent_wrong_config_type_raises(discovered_demo_plugin):
    claude_cfg = parse_agent_config(type="claude-code")
    with pytest.raises(TypeError, match=r"--type.*mismatch"):
        create_agent("byoa-demo", claude_cfg)
