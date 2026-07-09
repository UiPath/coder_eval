"""Live integration test for the bring-your-own-agent (BYOA) plugin SPI.

Proves the *whole* BYOA scenario against a real model: a third-party package
(``tests/fixtures/byoa_demo_plugin``) registers a ``byoa-demo`` agent kind purely
through the ``coder_eval.plugins`` entry point, and that plugin-registered agent
then drives a real Anthropic turn through coder-eval's factory + agent lifecycle —
with zero edits to ``coder_eval``.

Skipped by default; runs only with ``pytest -m live``. Requirements:
  - ``ANTHROPIC_API_KEY`` in the environment.
  - The fixture plugin importable. In CI it is ``pip install``-ed so genuine
    entry-point discovery runs; locally the test falls back to putting the
    fixture on ``sys.path`` and importing it, then registering through the same
    ``register(registry)`` hook entry-point discovery would call.
"""

import os
import sys
from pathlib import Path

import pytest

import coder_eval.plugins as plugins
from coder_eval.agents.registry import AgentRegistry, create_agent
from coder_eval.models import parse_agent_config


_live = pytest.mark.live


def _have_api_key() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


pytestmark = [_live, pytest.mark.skipif(not _have_api_key(), reason="Live BYOA test needs ANTHROPIC_API_KEY")]

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "byoa_demo_plugin"


@pytest.fixture
def demo_plugin_registered():
    """Ensure the ``byoa-demo`` kind is registered the way a real install would.

    First tries genuine entry-point discovery (CI installs the fixture package);
    falls back to importing the fixture off ``sys.path`` and invoking its
    ``register`` hook so the test is runnable locally without an install.
    """
    saved_registry = dict(AgentRegistry._registry)
    saved_loaded = plugins._loaded
    plugins.load_plugins()
    if AgentRegistry.get("byoa-demo") is None:
        sys.path.insert(0, str(_FIXTURE_DIR))
        import byoa_demo

        byoa_demo.register(AgentRegistry)
    try:
        yield
    finally:
        AgentRegistry._registry.clear()
        AgentRegistry._registry.update(saved_registry)
        plugins._loaded = saved_loaded
        sys.modules.pop("byoa_demo", None)
        if str(_FIXTURE_DIR) in sys.path:
            sys.path.remove(str(_FIXTURE_DIR))


@_live
async def test_byoa_plugin_agent_runs_real_turn(demo_plugin_registered, tmp_path):
    """A plugin-registered agent (byoa-demo) completes a real Anthropic turn."""
    assert "byoa-demo" in AgentRegistry.list_kinds()

    config = parse_agent_config(
        type="byoa-demo",
        permission_mode="bypassPermissions",
        model=os.getenv("ANTHROPIC_MODEL"),  # None -> SDK default
        demo_label="live",
    )
    agent = create_agent("byoa-demo", config)
    # The factory dispatched the plugin kind to the plugin's agent class.
    assert type(agent).__name__ == "DemoAgent"

    await agent.start(str(tmp_path))
    try:
        record = await agent.communicate(
            "Reply with exactly the word PONG and nothing else.",
            timeout=120,
        )
    finally:
        await agent.stop()

    assert record.crashed is False
    assert record.agent_output.strip()
    assert "pong" in record.agent_output.lower()
