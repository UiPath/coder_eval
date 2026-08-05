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

    def test_all_tools_mapped(self):
        expected_tools = {"Bash", "Write", "Edit", "Read", "Grep", "Glob"}
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


class TestSubagentRecoveryCounter:
    """The recovery gap must be counted, not just swallowed.

    A sub-agent whose rollout is missing or whose recovery raises contributes no
    tool calls to the transcript. Left silent, a scan over that transcript reports
    "nothing suspicious" for commands it never saw.
    """

    @staticmethod
    def _agent():
        from coder_eval.agents.codex_agent import CodexAgent
        from coder_eval.models import parse_agent_config

        return CodexAgent(parse_agent_config(type="codex"))

    @staticmethod
    def _noop_emit():
        class _Emit:
            def on_event(self, event) -> None:
                pass

        return _Emit()

    async def test_missing_rollout_counts_as_unrecovered(self, monkeypatch):
        agent = self._agent()

        async def _no_rollout(_home, _thread_id):
            return None

        monkeypatch.setattr(agent, "_await_rollout_file", _no_rollout)
        messages: list = []
        commands: list = []
        count = await agent._recover_subagent_tool_calls(
            [("thread-a", "tool-1", "gpt"), ("thread-b", "tool-2", "gpt")],
            {},
            messages,
            commands,
            self._noop_emit(),
            "task",
            "turn",
        )
        assert count == 2
        assert commands == []

    async def test_recovery_exception_counts_as_unrecovered(self, monkeypatch):
        agent = self._agent()

        async def _boom(_home, _thread_id):
            raise OSError("rollout unreadable")

        monkeypatch.setattr(agent, "_await_rollout_file", _boom)
        count = await agent._recover_subagent_tool_calls(
            [("thread-a", "tool-1", None)],
            {},
            [],
            [],
            self._noop_emit(),
            "task",
            "turn",
        )
        assert count == 1

    async def test_full_recovery_counts_zero(self, monkeypatch):
        agent = self._agent()

        async def _rollout(_home, _thread_id):
            return "rollout.jsonl"

        monkeypatch.setattr(agent, "_await_rollout_file", _rollout)
        monkeypatch.setattr(agent, "_parse_rollout_generations", lambda _p: [])
        count = await agent._recover_subagent_tool_calls(
            [("thread-a", "tool-1", None)],
            {},
            [],
            [],
            self._noop_emit(),
            "task",
            "turn",
        )
        assert count == 0


def test_turn_record_defaults_unrecovered_subagent_threads_to_zero():
    """Claude bubbles its sub-agent calls natively, so the default must be 0."""
    from coder_eval.models import TurnRecord

    assert TurnRecord(iteration=1, user_input="p", agent_output="a").unrecovered_subagent_threads == 0


class TestFileChangeKinds:
    """apply_patch change kinds ride along in Write telemetry parameters.

    The integrity scan needs them: a Codex `update` of a file required its
    current content (a read), while an `add` is a creation like Claude's Write.
    """

    def _agent(self) -> CodexAgent:
        return CodexAgent(parse_agent_config(type=AgentKind.CODEX))

    def test_kinds_are_recorded_alongside_paths(self):
        changes = [
            SimpleNamespace(path="a.py", kind="update"),
            SimpleNamespace(path="b.py", kind="add"),
        ]
        telemetry = self._agent()._extract_file_change_telemetry("fc_1", changes, "success", 0)
        assert telemetry is not None
        assert telemetry.tool_name == "Write"
        assert telemetry.parameters == {"paths": ["a.py", "b.py"], "kinds": ["update", "add"]}

    def test_kinds_are_omitted_when_the_sdk_provides_none(self):
        changes = [SimpleNamespace(path="a.py")]
        telemetry = self._agent()._extract_file_change_telemetry("fc_1", changes, "success", 0)
        assert telemetry is not None
        assert telemetry.parameters == {"paths": ["a.py"]}

    def test_stream_parameters_carry_the_first_change_kind(self):
        root = SimpleNamespace(
            type="fileChange", id="f1", changes=[SimpleNamespace(path="a.py", kind="update")], status="success"
        )
        assert self._agent()._tool_parameters(root, "fileChange") == {"path": "a.py", "kind": "update"}
