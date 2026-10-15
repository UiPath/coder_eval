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

import os
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

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

    def test_command_output_recorded_whole_not_truncated(self):
        # Regression for the Codex `output[:100]` bug (CE043): result_summary must
        # carry the FULL command output so result_tokens reflects real tool-output
        # size instead of being pinned at a ~31-token, 100-char cap.
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="gpt-5-codex"))
        state, commands, _messages = self._state(agent)

        big_output = "X" * 4000  # far beyond the old 100-char clip
        cmd_root = SimpleNamespace(
            type="commandExecution",
            id="c2",
            command="cat big.txt",
            exit_code=0,
            aggregated_output=big_output,
            duration_ms=5,
        )
        state.on_item_started(_item_notification("item/started", cmd_root))
        state.on_item_completed(_item_notification("item/completed", cmd_root))

        cmd = commands[0]
        assert big_output in (cmd.result_summary or ""), "full output must be recorded, not truncated"
        # result_tokens (ceil(len/4)) must scale with the real output, not ~31.
        assert cmd.result_tokens >= len(big_output) // 4
        assert cmd.result_tokens > 100


# Execution bounds. The SDK delivers `started_at_ms` / `completed_at_ms` on the item
# notification and the agent discarded both, publishing the item's own `duration_ms`
# instead — 0.0 for 70 of 211 commands in one nightly, absent for 25 more. All three
# telemetry builders now derive their timing identically.
# Pure logic: the builders take plain SimpleNamespace roots, so these live here rather
# than behind test_codex_agent.py's importorskip — otherwise a clean `make test`
# (which syncs no codex extra) skips them entirely.

_EPOCH_MS = 1_800_000_000_000


def _command_item(item_id: str = "cmd_1", *, duration_ms: object = None) -> SimpleNamespace:
    return SimpleNamespace(
        type="commandExecution",
        id=item_id,
        command="echo hi",
        exit_code=0,
        aggregated_output="hi",
        duration_ms=duration_ms,
    )


def _generic_item(item_id: str = "mcp_1", *, duration_ms: object = None) -> SimpleNamespace:
    return SimpleNamespace(
        type="mcpToolCall",
        id=item_id,
        server="srv",
        tool="lookup",
        arguments={"q": "x"},
        status="completed",
        error=None,
        success=True,
        duration_ms=duration_ms,
    )


def _file_change_item(item_id: str = "fc_1", *, duration_ms: object = None) -> SimpleNamespace:
    """A fileChange root. The SDK reports no duration for one, so
    ``duration_ms`` is accepted (for signature parity with the other two
    factories) and deliberately ignored."""
    del duration_ms
    return SimpleNamespace(
        type="fileChange",
        id=item_id,
        changes=[SimpleNamespace(path="out.txt")],
        status="completed",
    )


# (root factory, root_type) for each of the three builders, so a fix in one
# cannot pass for the others.
_BUILDERS = [
    pytest.param(_command_item, "commandExecution", id="commandExecution"),
    pytest.param(_file_change_item, "fileChange", id="fileChange"),
    pytest.param(_generic_item, "mcpToolCall", id="generic"),
]

# The two whose SDK item carries a duration at all. NAMED, not sliced
# positionally: reordering _BUILDERS must not silently point the fallback
# tests at fileChange, where the value is discarded and they would pass
# while asserting nothing.
_BUILDERS_WITH_SDK_DURATION = [_BUILDERS[0], _BUILDERS[2]]


class TestExecutionBoundsFromSdkStamps:
    """All three builders derive bounds and duration from the SDK stamps."""

    @staticmethod
    def _build(root, root_type, *, started_ms=None, completed_ms=None):
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="gpt-5.5"))
        telemetry, _ = agent._telemetry_for_item(
            root, root_type, getattr(root, "id", "x"), 0, started_ms=started_ms, completed_ms=completed_ms
        )
        assert telemetry is not None
        return telemetry

    @pytest.mark.parametrize(("factory", "root_type"), _BUILDERS)
    def test_both_stamps_give_bounds_and_an_exact_duration(self, factory, root_type):
        tel = self._build(factory(), root_type, started_ms=_EPOCH_MS, completed_ms=_EPOCH_MS + 250)
        assert tel.execution_started_at == datetime.fromtimestamp(_EPOCH_MS / 1000)
        assert tel.execution_completed_at == datetime.fromtimestamp((_EPOCH_MS + 250) / 1000)
        assert tel.duration_ms == 250.0

    @pytest.mark.parametrize(("factory", "root_type"), _BUILDERS)
    def test_timestamp_is_the_tools_own_start(self, factory, root_type):
        # datetime.now() at COMPLETION would place the call after its own
        # execution.
        tel = self._build(factory(), root_type, started_ms=_EPOCH_MS, completed_ms=_EPOCH_MS + 250)
        assert tel.timestamp == datetime.fromtimestamp(_EPOCH_MS / 1000)

    @pytest.mark.parametrize(("factory", "root_type"), _BUILDERS)
    def test_only_one_stamp_never_fabricates_an_interval(self, factory, root_type):
        # _ms_to_dt(None) is datetime.now(), so pairing a real stamp with a
        # missing one would invent an interval running to the present moment.
        tel = self._build(factory(), root_type, started_ms=_EPOCH_MS, completed_ms=None)
        assert tel.execution_started_at is None
        assert tel.execution_completed_at is None

    @pytest.mark.parametrize(("factory", "root_type"), _BUILDERS)
    def test_a_backwards_pair_clamps_but_keeps_both_bounds(self, factory, root_type):
        # Clock skew. The duration cannot be negative, but the anomaly stays
        # visible in the record.
        tel = self._build(factory(), root_type, started_ms=_EPOCH_MS + 500, completed_ms=_EPOCH_MS)
        assert tel.duration_ms == 0.0
        assert tel.execution_started_at is not None
        assert tel.execution_completed_at is not None

    @pytest.mark.parametrize(("factory", "root_type"), _BUILDERS_WITH_SDK_DURATION)
    def test_derived_duration_wins_over_a_conflicting_sdk_value(self, factory, root_type):
        tel = self._build(factory(duration_ms=0), root_type, started_ms=_EPOCH_MS, completed_ms=_EPOCH_MS + 250)
        assert tel.duration_ms == 250.0

    @pytest.mark.parametrize(("factory", "root_type"), _BUILDERS_WITH_SDK_DURATION)
    def test_no_stamps_falls_back_to_a_reported_sdk_duration(self, factory, root_type):
        tel = self._build(factory(duration_ms=12), root_type)
        assert tel.execution_started_at is None
        assert tel.duration_ms == 12.0

    @pytest.mark.parametrize(("factory", "root_type"), _BUILDERS_WITH_SDK_DURATION)
    def test_an_sdk_zero_is_unreported_not_instant(self, factory, root_type):
        # The case that motivated the change: 70 of 211 commands in one
        # nightly reported 0 for calls the message gaps show took seconds.
        assert self._build(factory(duration_ms=0), root_type).duration_ms is None

    @pytest.mark.parametrize(("factory", "root_type"), _BUILDERS_WITH_SDK_DURATION)
    def test_a_negative_sdk_duration_is_unreported_too(self, factory, root_type):
        assert self._build(factory(duration_ms=-5), root_type).duration_ms is None

    @pytest.mark.parametrize(("factory", "root_type"), _BUILDERS_WITH_SDK_DURATION)
    def test_an_absent_sdk_duration_stays_unknown(self, factory, root_type):
        assert self._build(factory(), root_type).duration_ms is None

    @pytest.mark.parametrize(("factory", "root_type"), _BUILDERS)
    def test_generation_completed_at_is_left_unset(self, factory, root_type):
        # Codex's stream does not say when the model finished emitting the
        # tool_use block; deriving it from the flush time would be a guess.
        tel = self._build(factory(), root_type, started_ms=_EPOCH_MS, completed_ms=_EPOCH_MS + 1)
        assert tel.generation_completed_at is None


def _skills_repo(root: Path, *names: str) -> Path:
    for name in names:
        (root / "skills" / name / "references").mkdir(parents=True)
        (root / "skills" / name / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
        (root / "skills" / name / "references" / "guide.md").write_text("ref\n", encoding="utf-8")
    return root


class TestSkillCopies:
    """Skills reach ``.agents/skills/`` as real files for each turn, and leave with it."""

    @staticmethod
    def _agent(tmp_path: Path, *names: str) -> CodexAgent:
        plugin = _skills_repo(tmp_path / "plugin", *names)
        agent = CodexAgent(
            parse_agent_config(type=AgentKind.CODEX, model="gpt-5.5", plugins=[{"type": "local", "path": str(plugin)}])
        )
        agent.working_directory = tmp_path / "work"
        agent.working_directory.mkdir()
        agent._collect_skills(None)
        return agent

    def test_a_walk_that_skips_symlinks_still_lists_the_skill(self, tmp_path):
        # os.walk without followlinks sees what `rg --files` and `find` see.
        agent = self._agent(tmp_path, "uipath-planner")
        agent._install_skills()
        skills_root = agent.working_directory / ".agents" / "skills"
        found = sorted(
            (Path(d) / f).relative_to(skills_root).as_posix() for d, _, files in os.walk(skills_root) for f in files
        )
        assert found == ["uipath-planner/SKILL.md", "uipath-planner/references/guide.md"]

    def test_remove_restores_the_workspace(self, tmp_path):
        agent = self._agent(tmp_path, "uipath-planner")
        agent._install_skills()
        agent._remove_skills()
        assert list(agent.working_directory.iterdir()) == []

    def test_workspace_skill_dirs_and_agent_files_survive(self, tmp_path):
        agent = self._agent(tmp_path, "uipath-planner", "uipath-rpa")
        own = agent.working_directory / ".agents" / "skills" / "uipath-rpa"
        own.mkdir(parents=True)
        (own / "SKILL.md").write_text("task-provided\n", encoding="utf-8")
        agent._install_skills()
        (agent.working_directory / ".agents" / "notes.md").write_text("agent output\n", encoding="utf-8")
        agent._remove_skills()
        assert (own / "SKILL.md").read_text(encoding="utf-8") == "task-provided\n"
        assert (agent.working_directory / ".agents" / "notes.md").is_file()
        assert not (agent.working_directory / ".agents" / "skills" / "uipath-planner").exists()

    @pytest.mark.parametrize("turn_raises", [False, True])
    async def test_skills_exist_only_during_each_turn(self, tmp_path, monkeypatch, turn_raises):
        agent = self._agent(tmp_path, "uipath-planner")
        agent.codex_client = object()
        skill_md = agent.working_directory / ".agents" / "skills" / "uipath-planner" / "SKILL.md"
        seen: list[bool] = []

        async def fake_turn(user_input, **_):
            seen.append(skill_md.is_file())
            if turn_raises:
                raise RuntimeError("turn failed")
            return "record"

        monkeypatch.setattr(agent, "_communicate_turn", fake_turn)
        for _ in range(2):
            if turn_raises:
                with pytest.raises(RuntimeError):
                    await agent.communicate("go")
            else:
                assert await agent.communicate("go") == "record"
            assert not (agent.working_directory / ".agents").exists()
        assert seen == [True, True]
