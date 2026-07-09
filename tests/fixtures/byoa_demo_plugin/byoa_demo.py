"""A minimal out-of-tree coder-eval agent plugin (test fixture).

This package lives entirely outside ``coder_eval`` and adds a new agent kind,
``byoa-demo``, purely through the public plugin SPI:

  * ``DemoAgentConfig`` — a ``ClaudeCodeAgentConfig`` subclass that re-types the
    discriminator to the plugin's own kind string.
  * ``DemoAgent`` — subclasses the real ``ClaudeCodeAgent``, so it drives the live
    Anthropic API exactly like the built-in agent (this is what the live test
    exercises). A real third-party agent would instead implement the ``Agent`` ABC
    from scratch; subclassing here keeps the fixture small while still proving the
    seam end to end.
  * ``register`` — the entry-point hook coder-eval calls at startup.

It is wired via ``[project.entry-points."coder_eval.plugins"]`` in pyproject.toml,
so installing this package is the only step needed for ``agent.type: byoa-demo``
to work in any task YAML — no edits to coder_eval.
"""

from typing import Literal

from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.agents.registry import AgentRegistry
from coder_eval.models import ClaudeCodeAgentConfig


DEMO_KIND = "byoa-demo"


class DemoAgentConfig(ClaudeCodeAgentConfig):
    """Config for the demo plugin agent — a fresh kind, not an AgentKind member."""

    type: Literal["byoa-demo"] = "byoa-demo"  # type: ignore[assignment]
    demo_label: str = "byoa"


class DemoAgent(ClaudeCodeAgent):
    """Plugin agent: inherits the real Claude Code behavior under a new kind."""


def register(registry: type[AgentRegistry]) -> None:
    """Entry-point hook: bind the demo kind to its (agent, config) pair.

    ``registry`` is the ``AgentRegistry`` class (not an instance).
    """
    registry.register(DEMO_KIND, DemoAgentConfig)(DemoAgent)
