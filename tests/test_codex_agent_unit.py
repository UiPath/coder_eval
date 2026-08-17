"""SDK-independent unit tests for CodexAgent.

These tests exercise pure-logic seams of ``codex_agent.py`` — the static
Claude→Codex tool-name map and the per-turn ``_CodexTurnState`` list-mutation
contract — that need NO Codex SDK. ``codex_agent`` imports ``openai_codex``
only lazily (inside ``start`` / ``_build_thread_options`` / the turn-completed
handler), so the module imports cleanly without the extra and these tests run
in the base Quality Gate.

The SDK-dependent tests (anything constructing real SDK notification/Turn
types or driving ``communicate``) stay in ``test_codex_agent.py`` behind that
module's ``importorskip("openai_codex")``.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from coder_eval.agents.codex_agent import _CLAUDE_TO_CODEX_TOOL_MAP, CodexAgent
from coder_eval.models import AgentKind, parse_agent_config


def _item_notification(method: str, root: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(method=method, payload=SimpleNamespace(item=SimpleNamespace(root=root)))


class TestToolNameMapping:
    """The static Claude→Codex tool-name map (pure dict, no SDK)."""

    def test_bash_maps_to_shell(self):
        assert _CLAUDE_TO_CODEX_TOOL_MAP["Bash"] == "shell"

    def test_write_maps_to_apply_patch(self):
        assert _CLAUDE_TO_CODEX_TOOL_MAP["Write"] == "apply_patch"

    def test_edit_maps_to_apply_patch(self):
        assert _CLAUDE_TO_CODEX_TOOL_MAP["Edit"] == "apply_patch"

    def test_read_maps_to_shell(self):
        """Read maps to shell in Codex (no dedicated read tool)."""
        assert _CLAUDE_TO_CODEX_TOOL_MAP["Read"] == "shell"

    def test_grep_maps_to_shell(self):
        assert _CLAUDE_TO_CODEX_TOOL_MAP["Grep"] == "shell"

    def test_glob_maps_to_shell(self):
        assert _CLAUDE_TO_CODEX_TOOL_MAP["Glob"] == "shell"

    def test_webfetch_maps_to_web_search(self):
        assert _CLAUDE_TO_CODEX_TOOL_MAP["WebFetch"] == "web_search"

    def test_websearch_maps_to_web_search(self):
        assert _CLAUDE_TO_CODEX_TOOL_MAP["WebSearch"] == "web_search"

    def test_all_tools_mapped(self):
        expected_tools = {"Bash", "Write", "Edit", "Read", "Grep", "Glob", "WebFetch", "WebSearch"}
        assert expected_tools.issubset(set(_CLAUDE_TO_CODEX_TOOL_MAP.keys()))


class TestCodexTurnState:
    """Unit tests for the per-turn state object extracted from _run_turn_with_streaming."""

    @staticmethod
    def _state(agent):
        from coder_eval.agents.codex_agent import _CodexTurnState
        from coder_eval.streaming.callbacks import CompositeStreamCallback
        from coder_eval.streaming.collector import EventCollector

        commands: list = []
        messages: list = []
        collector = EventCollector()
        state = _CodexTurnState(
            agent,
            emit=CompositeStreamCallback([collector]),
            task_id="codex",
            turn_id="codex-1",
            collector=collector,
            commands=commands,
            messages=messages,
            user_input="go",
            iteration=1,
            turn_start_time=time.monotonic(),
        )
        return state, commands, messages

    def test_holds_commands_and_messages_by_identity(self):
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="gpt-5-codex"))
        state, commands, messages = self._state(agent)
        # The state must hold the caller's SAME list objects (no copy) so a
        # mid-turn crash keeps the partial transcript.
        assert state.commands is commands
        assert state.messages is messages

    def test_command_dispatch_mutates_lists_in_place(self):
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="gpt-5-codex"))
        state, commands, _messages = self._state(agent)

        cmd_root = SimpleNamespace(
            type="commandExecution", id="c1", command="echo hi", exit_code=0, aggregated_output="hi\n", duration_ms=5
        )
        state.on_item_started(_item_notification("item/started", cmd_root))
        state.on_item_completed(_item_notification("item/completed", cmd_root))

        # Telemetry recorded into the SAME commands list, by identity.
        assert commands is state.commands
        assert len(commands) == 1
        assert commands[0].tool_name == "Bash"
        assert commands[0].result_status == "success"
        # A tool_use block was recorded into the open buffer (cut at the next
        # tokenUsage flush, not here), joinable to the command by tool_id.
        assert any(b.block_type == "tool_use" and b.tool_use_id == "c1" for b in state.open_blocks)

    def test_web_open_page_start_is_webfetch_with_url(self):
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="gpt-5-codex"))
        state, _commands, _messages = self._state(agent)
        action = SimpleNamespace(root=SimpleNamespace(type="openPage", url="https://example.com/docs"))
        web_root = SimpleNamespace(type="webSearch", id="ws-open", query="", action=action)

        state.on_item_started(_item_notification("item/started", web_root))

        telemetry = state.open_tools["ws-open"]
        assert telemetry.tool_name == "WebFetch"
        assert telemetry.parameters == {"url": "https://example.com/docs"}

    def test_web_open_page_completion_upgrades_provisional_websearch(self):
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="gpt-5-codex"))
        state, commands, _messages = self._state(agent)
        started = SimpleNamespace(type="webSearch", id="ws-late-action", query="", action=None)
        completed = SimpleNamespace(
            type="webSearch",
            id="ws-late-action",
            query="",
            action=SimpleNamespace(
                root=SimpleNamespace(type="openPage", url="https://example.com/late"),
            ),
        )

        state.on_item_started(_item_notification("item/started", started))
        provisional = state.open_tools["ws-late-action"]
        assert provisional.tool_name == "WebSearch"

        state.on_item_completed(_item_notification("item/completed", completed))

        assert provisional.tool_name == "WebFetch"
        assert provisional.parameters == {"url": "https://example.com/late"}
        assert commands[0].tool_name == "WebFetch"
        assert commands[0].parameters == {"url": "https://example.com/late"}

    def test_explicit_search_action_remains_websearch_and_preserves_queries(self):
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="gpt-5-codex"))
        state, _commands, _messages = self._state(agent)
        action = SimpleNamespace(
            root=SimpleNamespace(type="search", query="guardrails", queries=["guardrails", "uipath guardrails"])
        )
        web_root = SimpleNamespace(type="webSearch", id="ws-search", query="guardrails", action=action)

        state.on_item_started(_item_notification("item/started", web_root))

        telemetry = state.open_tools["ws-search"]
        assert telemetry.tool_name == "WebSearch"
        assert telemetry.parameters == {
            "query": "guardrails",
            "queries": ["guardrails", "uipath guardrails"],
        }
