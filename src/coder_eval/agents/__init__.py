"""Agent implementations and registry."""

# Import agents to trigger their @register decorators
from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.agents.codex_agent import CodexAgent
from coder_eval.agents.registry import AgentRegistry, create_agent


__all__ = [
    "AgentRegistry",
    "ClaudeCodeAgent",
    "CodexAgent",
    "create_agent",
]
