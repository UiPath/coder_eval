"""Agent implementations and registry."""

# Import agents to trigger their @register decorators
from coder_eval.agents.antigravity_agent import AntigravityAgent
from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.agents.codex_agent import CodexAgent
from coder_eval.agents.noop_agent import NoOpAgent
from coder_eval.agents.opencode_agent import OpenCodeAgent
from coder_eval.agents.registry import AgentRegistry, create_agent
from coder_eval.models import AgentKind


def register_builtins(registry: type[AgentRegistry]) -> None:
    """Register the built-in agents (Claude/Codex/Antigravity/OpenCode/NoOp) onto ``registry``.

    This is the target of coder-eval's own ``coder_eval.plugins`` entry point, so
    the built-in agents travel the identical discovery path as any third-party
    plugin (see :mod:`coder_eval.plugins`). Importing the agent modules above
    fires their ``@AgentRegistry.register`` decorators; this hook only has to
    ensure those modules are imported, which the package import already did.
    """
    # Reference the imported classes so the registration side effect is explicit
    # and a future refactor that drops the top-level imports fails loudly here.
    _ = (ClaudeCodeAgent, CodexAgent, AntigravityAgent, OpenCodeAgent, NoOpAgent)
    # Rot-protection: the decorators fire on import, but assert the built-ins are
    # actually registered so a future lazy-import refactor (which would leave the
    # import-cached modules' decorators un-run) fails loudly instead of silently
    # registering nothing.
    for kind in (
        AgentKind.CLAUDE_CODE,
        AgentKind.CODEX,
        AgentKind.ANTIGRAVITY,
        AgentKind.OPENCODE,
        AgentKind.NONE,
    ):
        if registry.get(kind) is None:
            raise RuntimeError(f"register_builtins: built-in agent kind {kind!r} did not register")


__all__ = [
    "AgentRegistry",
    "AntigravityAgent",
    "ClaudeCodeAgent",
    "CodexAgent",
    "NoOpAgent",
    "OpenCodeAgent",
    "create_agent",
    "register_builtins",
]
