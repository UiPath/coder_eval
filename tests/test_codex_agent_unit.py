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

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from coder_eval.agents.codex_agent import (
    _CLAUDE_TO_CODEX_TOOL_MAP,
    CodexAgent,
    _js_unescaped,
    _record_command_text,
)
from coder_eval.models import AgentKind, parse_agent_config
from tests._fixtures.codex_rollouts import (
    command_record,
    exec_call,
    exec_output,
    file_change_record,
    token_count_event,
    wait_call,
    wait_output,
    write_child_rollout,
)


_BOUNDARY = token_count_event(inp=10, cached=0, out=5, tot_in=10, tot_cached=0, tot_out=5)


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


class TestSubAgentRolloutRecovery:
    """A sub-agent's tool rows are rebuilt from its rollout file: pure parsing, no SDK.

    A code-mode ``exec`` script's rows are the ``item_completed`` records Codex writes
    while it runs, each with its own result.
    """

    @staticmethod
    def _rows(path: Path) -> list[tuple[str, str, dict]]:
        gens = CodexAgent._parse_rollout_generations(path)
        return [(c["tool_name"], c["status"], c["parameters"]) for g in gens for c in g["tools"]]

    def test_a_scripts_rows_are_its_records_each_with_its_own_result(self, tmp_path):
        script = (
            "await tools.apply_patch(p);"
            ' await Promise.all([tools.exec_command({cmd:"pytest -q"}), tools.exec_command({cmd:"ls nope"})]);'
        )
        path = write_child_rollout(
            tmp_path,
            "t1",
            [
                exec_call("c1", script),
                file_change_record("/w/a.txt", item_id="exec-1"),
                command_record("pytest -q", exit_code=0, item_id="exec-2"),
                command_record("ls nope", exit_code=1, item_id="exec-3"),
                exec_output("c1", '[{"exit_code":0},{"exit_code":1}]'),
            ],
        )

        gens = CodexAgent._parse_rollout_generations(path)

        # `Script completed` says only that the script ended; each record keeps its own result.
        assert [(c["call_id"], c["tool_name"], c["status"], c["parameters"]) for g in gens for c in g["tools"]] == [
            ("exec-1", "Write", "success", {"paths": ["/w/a.txt"]}),
            ("exec-2", "Bash", "success", {"command": "pytest -q"}),
            ("exec-3", "Bash", "error", {"command": "ls nope"}),
        ]

    def test_a_script_that_ran_nothing_recorded_is_one_exec_row(self, tmp_path):
        script = "text(ALL_TOOLS.filter(x => /agent/.test(x.name)));"
        path = write_child_rollout(tmp_path, "t1", [exec_call("c1", script), exec_output("c1", "[]")])

        assert self._rows(path) == [("exec", "success", {"input": script})]

    @pytest.mark.parametrize(
        ("output", "status"),
        [
            (exec_output("c1", "TypeError: x is undefined", head="Script failed"), "error"),
            ({"type": "custom_tool_call_output", "call_id": "c1", "output": "code-mode host is disabled"}, "error"),
            (None, "unknown"),
            (exec_output("c1", head="Script running with cell ID 3"), "unknown"),
            (exec_output("c1", head="Script terminated"), "unknown"),
            ({"type": "custom_tool_call_output", "call_id": "c1", "output": "aborted by user after 2.5s"}, "unknown"),
        ],
        ids=["script-failed", "host-disabled", "no-output", "still-running", "terminated", "aborted"],
    )
    def test_an_exec_row_status_comes_from_the_script_output(self, tmp_path, output, status):
        lines = [exec_call("c1", "await tools.view_image({path:'a.png'});")]
        if output is not None:
            lines.append(output)

        assert self._rows(write_child_rollout(tmp_path, "t1", lines))[0][1] == status

    @pytest.mark.parametrize(
        ("script", "recorded"),
        [
            (
                'const c = "rm -rf build"; await tools.exec_command({cmd: "ls"}); await tools.exec_command({cmd: c});',
                ["ls", "rm -rf build"],
            ),
            (
                'await tools.shell_command({justification:"re-run the command: tests", command:"pytest -q"});',
                ["pytest -q"],
            ),
            ('for (const f of ["a", "b"]) await tools.exec_command({cmd: `cat ${f}`});', ["cat a", "cat b"]),
        ],
        ids=["variable", "key-inside-a-string", "loop"],
    )
    def test_commands_come_from_the_records_not_the_script_text(self, tmp_path, script, recorded):
        lines = [exec_call("c1", script)]
        lines += [command_record(cmd, exit_code=0, item_id=f"exec-{i}") for i, cmd in enumerate(recorded)]
        lines.append(exec_output("c1"))

        assert [p["command"] for _, _, p in self._rows(write_child_rollout(tmp_path, "t1", lines))] == recorded

    def test_a_record_of_a_direct_call_is_not_a_row(self, tmp_path):
        path = write_child_rollout(
            tmp_path,
            "t1",
            [
                exec_call("c1", "text(1);"),
                {"type": "function_call", "name": "exec_command", "call_id": "c2", "arguments": '{"cmd":"ls"}'},
                command_record("ls", exit_code=0, item_id="c2"),
                {"type": "function_call_output", "call_id": "c2", "output": "Exit code: 0\nOutput:\na\n"},
                exec_output("c1", "1"),
            ],
        )

        assert self._rows(path) == [
            ("exec", "success", {"input": "text(1);"}),
            ("Bash", "success", {"cmd": "ls", "command": "ls"}),
        ]

    def test_a_late_record_matching_no_script_source_goes_to_the_newest_script(self, tmp_path):
        # The command is built from a variable, so no script's source holds its text,
        # and it ends after its script: it is still that script's row, not dropped.
        script = "for (const f of files) await tools.exec_command({cmd:`pytest ${f}`});"
        path = write_child_rollout(
            tmp_path,
            "t1",
            [
                exec_call("c1", script),
                exec_output("c1", '{"session_id":4}'),
                command_record("pytest test_a.py", exit_code=0, item_id="exec-1"),
            ],
        )

        assert self._rows(path) == [("Bash", "success", {"command": "pytest test_a.py"})]

    def test_a_record_in_a_rollout_with_no_script_is_not_a_row(self, tmp_path):
        path = write_child_rollout(tmp_path, "t1", [command_record("ls", exit_code=0, item_id="exec-1")])

        assert self._rows(path) == []

    @pytest.mark.parametrize(
        ("source_cmd", "command"),
        [
            ("sh -c 'sleep 45; exit 5'", "sh -c 'sleep 45; exit 5'"),
            ('pytest C:\\\\w\\\\tests -k \\"slow\\"', 'pytest C:\\w\\tests -k "slow"'),
            (
                "set +e\\nprintf '%s\\\\n' '== uip =='\\nuip --version",
                "set +e\nprintf '%s\\n' '== uip =='\nuip --version",
            ),
        ],
        ids=["as-written", "escaped-in-the-source", "multi-line"],
    )
    def test_a_long_command_polled_by_a_later_script_replaces_its_own_script(self, tmp_path, source_cmd, command):
        # The shape of a real CLI 0.156.1 run: the command outlives its script, a second
        # script polls it with write_stdin, and its record arrives while that one runs.
        start = (
            f'const r = await tools.exec_command({{cmd:"{source_cmd}",yield_time_ms:30000}}); text(JSON.stringify(r));'
        )
        poll = (
            'const r = await tools.write_stdin({session_id:27738,chars:"",yield_time_ms:30000});'
            " text(JSON.stringify(r));"
        )
        path = write_child_rollout(
            tmp_path,
            "t1",
            [
                exec_call("c1", start),
                exec_output("c1", '{"session_id":27738}'),
                _BOUNDARY,
                exec_call("c2", poll),
                command_record(command, exit_code=5, item_id="exec-1"),
                exec_output("c2", '{"exit_code":5}'),
                _BOUNDARY,
            ],
        )

        gens = CodexAgent._parse_rollout_generations(path)

        # The command's row sits in the generation that started it, so no generation is left empty.
        assert [[(c["tool_name"], c["status"], c["parameters"]) for c in g["tools"]] for g in gens] == [
            [("Bash", "error", {"command": command})],
            [("exec", "success", {"input": poll})],
        ]

    def test_a_record_that_arrives_during_a_wait_goes_to_the_yielded_script(self, tmp_path):
        path = write_child_rollout(
            tmp_path,
            "t1",
            [
                exec_call("c1", 'await tools.exec_command({cmd:"make build", yield_time_ms:120000});'),
                exec_output("c1", head="Script running with cell ID 3"),
                _BOUNDARY,
                wait_call("w1", "3"),
                wait_output("w1", head="Script running with cell ID 3"),
                _BOUNDARY,
                wait_call("w2", "3"),
                command_record("make build", exit_code=2, item_id="exec-1"),
                wait_output("w2", head="Script failed"),
                _BOUNDARY,
            ],
        )

        gens = CodexAgent._parse_rollout_generations(path)

        assert [[(c["tool_name"], c["status"]) for c in g["tools"]] for g in gens] == [
            [("Bash", "error")],
            [("wait", "unknown")],
            [("wait", "error")],
        ]

    def test_a_yielded_script_without_records_stays_unknown_and_its_wait_is_a_row(self, tmp_path):
        lines = [
            exec_call("c1", "await tools.view_image({path:'a.png'});"),
            exec_output("c1", head="Script running with cell ID 9"),
            wait_call("w1", "9"),
            wait_output("w1", head="Script completed"),
        ]

        assert [row[:2] for row in self._rows(write_child_rollout(tmp_path, "t1", lines))] == [
            ("exec", "unknown"),
            ("wait", "success"),
        ]

    @pytest.mark.parametrize(
        ("output", "status"),
        [
            ("Exit code: 0\nWall time: 1 seconds\nOutput:\nok\n", "success"),
            ("Exit code: 1\nWall time: 1 seconds\nOutput:\nboom\n", "error"),
            ("Chunk ID: ab12\nWall time: 0.2 seconds\nProcess exited with code 2\nOutput:\nboom\n", "error"),
            ("Chunk ID: ab12\nWall time: 0.2 seconds\nProcess exited with code 0\nOutput:\nExit code: 1\n", "success"),
            ("5050\n", "success"),
            (
                "Chunk ID: ab12\nWall time: 10.0 seconds\nProcess running with session ID 7\nOutput:\nstill going\n",
                "unknown",
            ),
            ("Wall time: 3.2 seconds\naborted by user", "unknown"),
            ("aborted by user after 3.2s", "unknown"),
            ("Exit code: 0\nWall time: 1 seconds\nOutput:\naborted by user\n", "success"),
            ("write_stdin failed: Unknown process id 7", "error"),
        ],
        ids=[
            "v1-ok",
            "v1-failed",
            "unified-exec-failed",
            "exit-line-in-the-body",
            "no-header",
            "unified-exec-still-running",
            "shell-aborted",
            "call-aborted",
            "abort-text-in-the-body",
            "call-failed",
        ],
    )
    def test_a_direct_shell_call_status_comes_from_its_output_header(self, output, status):
        assert CodexAgent._subagent_output({"output": output}) == (output, status)

    @pytest.mark.parametrize(
        ("argv", "text"),
        [
            (["/bin/zsh", "-lc", "pytest -q"], "pytest -q"),
            (["bash", "-c", "ls -la"], "ls -la"),
            (["C:\\Program Files\\PowerShell\\7\\pwsh.exe", "-NoProfile", "-Command", "git status"], "git status"),
            (["cmd.exe", "/d", "/c", "dir"], "dir"),
            (["python3", "-c", "print(1)"], "python3 -c 'print(1)'"),
            ("echo hi", "echo hi"),
        ],
        ids=["zsh-login", "bash", "powershell", "cmd", "not-a-shell", "string"],
    )
    def test_record_command_text_drops_only_the_shell_wrapper(self, argv, text):
        assert _record_command_text(argv) == text

    @pytest.mark.parametrize(
        ("source", "decoded"),
        [
            ("a\\nb\\tc", "a\nb\tc"),
            ("printf '%s\\\\n' \\\"x\\\"", "printf '%s\\n' \"x\""),
            ("caf\\u00e9 \\u{1F600} \\x41", "caf\u00e9 \U0001f600 A"),
            ("one \\\ntwo", "one two"),
            ("\\u{110000}", "\\u{110000}"),
        ],
        ids=["newline-tab", "escaped-backslash-and-quote", "unicode-and-hex", "line-continuation", "bad-code-point"],
    )
    def test_js_unescaped_decodes_string_escapes(self, source, decoded):
        assert _js_unescaped(source) == decoded

    def test_a_line_that_is_not_a_json_object_is_skipped(self, tmp_path):
        path = write_child_rollout(tmp_path, "t1", [exec_call("c1", "text(1);"), exec_output("c1", "1")])
        path.write_text('"a string"\n[1, 2]\n' + path.read_text(encoding="utf-8"), encoding="utf-8")

        assert self._rows(path) == [("exec", "success", {"input": "text(1);"})]

    def test_forked_parent_history_is_not_credited_to_the_child(self, tmp_path):
        # fork_turns copies the parent's history ahead of the child's own; the
        # parent's shell call in that prefix must not be replayed as the child's.
        child, parent = "019e0000-1111-7000-8000-000000000008", "019e0000-0000-7000-8000-000000000000"
        path = write_child_rollout(
            tmp_path,
            child,
            [
                {"type": "session_meta", "payload": {"id": child, "forked_from_id": parent}},
                {"type": "session_meta", "payload": {"id": parent}},
                {
                    "type": "function_call",
                    "name": "shell_command",
                    "call_id": "p_1",
                    "arguments": '{"command":"git status"}',
                },
                {"type": "function_call_output", "call_id": "p_1", "output": "Exit code: 0\nOutput:\n"},
                {"type": "event_msg", "payload": {"type": "thread_settings_applied", "thread_id": child}},
                {"type": "turn_context", "payload": {"turn_id": "t_child"}},
                {"type": "function_call", "name": "shell_command", "call_id": "c_1", "arguments": '{"command":"ls"}'},
                {"type": "function_call_output", "call_id": "c_1", "output": "Exit code: 0\nOutput:\nREADME.md\n"},
            ],
        )

        assert [p["command"] for _, _, p in self._rows(path)] == ["ls"]

    def test_rollout_without_fork_marker_is_parsed_whole(self, tmp_path):
        child = "019e0000-2222-7000-8000-000000000009"
        path = write_child_rollout(
            tmp_path,
            child,
            [
                {"type": "session_meta", "payload": {"id": child}},
                {"type": "function_call", "name": "shell_command", "call_id": "c_1", "arguments": '{"command":"ls"}'},
                {"type": "function_call_output", "call_id": "c_1", "output": "Exit code: 0\nOutput:\n"},
                {"type": "event_msg", "payload": {"type": "thread_settings_applied", "thread_id": child}},
            ],
        )

        assert [p["command"] for _, _, p in self._rows(path)] == ["ls"]

    def test_a_fork_without_its_own_settings_marker_is_parsed_whole_and_logged(self, tmp_path, caplog):
        child, parent = "019e0000-3333-7000-8000-00000000000a", "019e0000-0000-7000-8000-000000000000"
        path = write_child_rollout(
            tmp_path,
            child,
            [
                {"type": "session_meta", "payload": {"id": child, "forked_from_id": parent}},
                {"type": "function_call", "name": "shell_command", "call_id": "c_1", "arguments": '{"command":"ls"}'},
                {"type": "function_call_output", "call_id": "c_1", "output": "Exit code: 0\nOutput:\n"},
            ],
        )

        with caplog.at_level(logging.WARNING, logger="coder_eval.agents.codex_agent"):
            assert [p["command"] for _, _, p in self._rows(path)] == ["ls"]
        assert child in caplog.text
