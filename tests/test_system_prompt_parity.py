"""``agent.system_prompt`` must mean the same thing on every harness: APPEND.

The field is extra text layered on top of whatever the harness's own default agent
prompt is. Full replacement is expressible on all three SDKs but is the wrong
semantics for a task-level guardrail — substituting a one-liner for Codex's base
instructions or Antigravity's core mandates would gut the harness rather than
constrain it. See docs/agents/HARNESS_PARITY.md.
"""

from types import ModuleType, SimpleNamespace

import pytest

from coder_eval.agents.antigravity_agent import AntigravityAgent
from coder_eval.agents.claude_code_agent import _append_system_prompt
from coder_eval.agents.codex_agent import CodexAgent
from coder_eval.models import AgentKind, parse_agent_config
from coder_eval.streaming.collector import EventCollector
from coder_eval.streaming.events import ToolEndEvent, ToolEndStatus


PROMPT = "You are a coding agent. Do not access files in sibling runs/* directories."


# --- claude-code ---------------------------------------------------------------------


def test_claude_appends_rather_than_replacing():
    """The preset+append form selects --append-system-prompt, not --system-prompt."""
    assert _append_system_prompt(PROMPT) == {"type": "preset", "preset": "claude_code", "append": PROMPT}


def test_claude_unset_prompt_keeps_the_default_prompt():
    """No append key → the SDK emits no prompt flag at all, so the CLI default stands.

    Passing None straight through would make the SDK emit ``--system-prompt ""``,
    leaving an unconfigured run with NO system prompt while Codex and Antigravity
    keep their full vendor prompts — the divergence this parity work removes.
    """
    preset = _append_system_prompt(None)

    assert preset == {"type": "preset", "preset": "claude_code"}
    assert "append" not in preset


def test_claude_empty_prompt_is_treated_as_unset():
    assert "append" not in _append_system_prompt("")


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        (PROMPT, {"type": "preset", "preset": "claude_code", "append": PROMPT}),
        (None, {"type": "preset", "preset": "claude_code"}),
    ],
)
def test_claude_options_carry_the_preset_form(prompt, expected):
    """End-to-end through the real options builder, not just the helper."""
    agent = _claude_agent(system_prompt=prompt)

    options, _transport, _model = agent._build_claude_query(
        user_input="go", timeout=None, max_turns=None, stderr_callback=lambda _line: None
    )

    assert options.system_prompt == expected


def _claude_agent(**cfg):
    from pathlib import Path

    from coder_eval.agents.claude_code_agent import ClaudeCodeAgent

    agent = ClaudeCodeAgent(parse_agent_config(type=AgentKind.CLAUDE_CODE, **cfg))
    agent.working_directory = Path(".")
    return agent


# --- codex ---------------------------------------------------------------------------


def test_codex_uses_developer_instructions():
    """Codex's additive knob — NOT base_instructions, which replaces its whole prompt."""
    agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, system_prompt=PROMPT))

    options = agent._build_thread_options()

    assert options["developer_instructions"] == PROMPT
    assert "base_instructions" not in options


def test_codex_omits_the_knob_when_unset():
    """The field was dropped entirely before; absent must still mean "SDK default"."""
    agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))

    assert "developer_instructions" not in agent._build_thread_options()


# --- antigravity ---------------------------------------------------------------------


async def test_antigravity_passes_a_plain_string(monkeypatch, tmp_path):
    """A plain str becomes a TemplatedSystemInstructions SECTION on top of the defaults.

    types.CustomSystemInstructions would replace them wholesale — the SDK's own
    docstring flags it as advanced usage that drops the core safety mandates.
    """
    configs: list = []

    class _FakeSdkAgent:
        def __init__(self, cfg):
            configs.append(cfg)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    ag = ModuleType("google.antigravity")
    ag.Agent = _FakeSdkAgent
    ag.LocalAgentConfig = lambda **kwargs: SimpleNamespace(models=[], **kwargs)
    ag.types = SimpleNamespace(
        ThinkingLevel=lambda level: level,
        GeminiAPIEndpoint=type("GeminiAPIEndpoint", (), {}),
        GeminiModelOptions=lambda **kw: SimpleNamespace(**kw),
        CapabilitiesConfig=lambda **kw: SimpleNamespace(**kw),
    )
    hooks = ModuleType("google.antigravity.hooks")
    hooks.policy = SimpleNamespace(allow_all=lambda: SimpleNamespace(kind="allow_all"))
    import sys

    google_pkg = sys.modules.get("google") or ModuleType("google")
    monkeypatch.setitem(sys.modules, "google", google_pkg)
    monkeypatch.setitem(sys.modules, "google.antigravity", ag)
    monkeypatch.setitem(sys.modules, "google.antigravity.hooks", hooks)

    agent = AntigravityAgent(parse_agent_config(type=AgentKind.ANTIGRAVITY, system_prompt=PROMPT))
    await agent.start(str(tmp_path))

    assert configs[0].system_instructions == PROMPT


# --- the shared visible-turn definition ----------------------------------------------


def _tool_end(collector: EventCollector, tool_id: str) -> None:
    from datetime import datetime

    from coder_eval.models import CommandTelemetry

    collector.on_event(
        ToolEndEvent(
            task_id="t",
            turn_id="turn-1",
            tool=CommandTelemetry(tool_name="Bash", tool_id=tool_id, timestamp=datetime.now(), sequence_number=0),
            status=ToolEndStatus.OK,
        )
    )


def test_collector_visible_turn_count_counts_resolved_tool_calls():
    """The single definition Codex and Antigravity both cap against."""
    collector = EventCollector()
    assert collector.visible_turn_count == 0

    _tool_end(collector, "a")
    _tool_end(collector, "b")

    assert collector.visible_turn_count == 2


def test_collector_visible_turn_count_does_not_double_count_a_tool_id():
    """Keyed on tool_id, so a re-emitted end event cannot inflate the count past the cap."""
    collector = EventCollector()

    _tool_end(collector, "a")
    _tool_end(collector, "a")

    assert collector.visible_turn_count == 1


def test_collector_visible_turn_count_matches_the_built_record():
    """It is the live view of exactly the list ``TurnRecord.commands`` ends up holding."""
    collector = EventCollector()
    for tool_id in ("a", "b", "c"):
        _tool_end(collector, tool_id)

    assert collector.visible_turn_count == len(collector.build_turn_record().commands)


@pytest.mark.parametrize("agent_cls", [CodexAgent, AntigravityAgent])
def test_both_capped_agents_declare_cooperative_stop(agent_cls):
    """The turn cap reuses the cooperative-stop boundary, so both must support it."""
    assert agent_cls.supports_cooperative_stop is True
