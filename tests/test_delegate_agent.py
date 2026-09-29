"""Unit tests for :class:`coder_eval.agents.delegate_agent.DelegateAgent`.

The Node host is never spawned for real: ``asyncio.create_subprocess_exec`` is
patched with a fake process whose stdout/stderr are pre-scripted byte lines and
whose stdin captures every write for assertion, mirroring this repo's existing
CLI-agent test convention (see ``tests/test_opencode_agent.py``).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from coder_eval.agents import delegate_agent as agent_module
from coder_eval.agents.delegate_agent import DelegateAgent, _resolve_host_bundle
from coder_eval.agents.registry import AgentRegistry, create_agent
from coder_eval.errors import AgentConfigError, AgentCrashError, TurnTimeoutError
from coder_eval.errors.categories import ErrorCategory
from coder_eval.errors.categorization import categorize_error
from coder_eval.models import AgentKind, DelegateAgentConfig
from coder_eval.streaming.events import AgentEndEvent, AgentEndStatus, AgentStartEvent


def _line(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj) + "\n").encode("utf-8")


def _ev(**event: Any) -> bytes:
    """One ``event`` frame wrapping an SDK event, as ``delegate-stdio`` writes it."""
    return _line({"type": "event", "event": event})


def _result(**fields: Any) -> bytes:
    return _line({"type": "result", **fields})


def _tool_call(tool_id: str, name: str = "shell", **args: Any) -> bytes:
    return _ev(type="tool_call", toolId=tool_id, toolName=name, toolArgs=args, toolStatus="pending")


def _tool_result(tool_id: str, content: str = "ok", *, status: str = "completed") -> bytes:
    return _ev(
        type="tool_result",
        toolId=tool_id,
        toolName="shell",
        toolResult={"responseType": "success", "content": content},
        toolStatus=status,
    )


class _FakeStreamReader:
    def __init__(self, lines: list[bytes], *, hang_after: bool = False) -> None:
        self._lines = list(lines)
        self._hang_after = hang_after

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        if self._hang_after:
            # Never resolves -- for exercising a timeout that elapses WHILE
            # blocked reading, not just the loop's own top-of-iteration
            # pre-check.
            await asyncio.Event().wait()
        return b""


class _FakeStdin:
    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(json.loads(data.decode("utf-8").strip()))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(
        self, stdout_lines: list[bytes], stderr_lines: list[bytes] | None = None, *, hang_after: bool = False
    ) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStreamReader(stdout_lines, hang_after=hang_after)
        self.stderr = _FakeStreamReader(stderr_lines or [])
        self.returncode: int | None = None
        self.pid = 4242
        self._killed = False
        self.spawn_args: tuple[Any, ...] = ()
        self.spawn_kwargs: dict[str, Any] = {}

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0.01)
        return self.returncode

    def kill(self) -> None:
        self._killed = True
        self.returncode = -9

    def terminate(self) -> None:
        self.kill()


@pytest.fixture
def patch_exec(monkeypatch: pytest.MonkeyPatch):
    """Patch ``create_subprocess_exec`` to return a fake process fed ``stdout_lines``.

    Also stubs ``os.killpg`` so the agent's process-group sweep can never signal
    a real group whose id happens to collide with the fake pid.
    """

    def _install(
        stdout_lines: list[bytes], stderr_lines: list[bytes] | None = None, *, hang_after: bool = False
    ) -> _FakeProcess:
        proc = _FakeProcess(stdout_lines, stderr_lines, hang_after=hang_after)

        async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
            proc.spawn_args = args
            proc.spawn_kwargs = kwargs
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/node")
        monkeypatch.setattr(agent_module, "_resolve_host_bundle", lambda: Path("/opt/delegate_stdio.mjs"))
        # raising=False: os.killpg does not exist on Windows, where the sweep is a
        # no-op -- the stub must still install so the fixture works on every platform.
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: None, raising=False)
        return proc

    return _install


def _config(**overrides: Any) -> DelegateAgentConfig:
    return DelegateAgentConfig(type=AgentKind.DELEGATE, **overrides)


async def _started_agent(
    patch_exec, stdout_lines: list[bytes], tmp_path, **config_kwargs
) -> tuple[DelegateAgent, _FakeProcess]:
    proc = patch_exec([_line({"type": "init_ok"}), *stdout_lines])
    agent = DelegateAgent(_config(**config_kwargs), task_id="t1")
    await agent.start(str(tmp_path))
    return agent, proc


class TestResolveHostBundle:
    def test_explicit_path_env_var(self, tmp_path, monkeypatch):
        entry = tmp_path / "delegate_stdio.mjs"
        entry.write_text("#!/usr/bin/env node")
        monkeypatch.setenv("DELEGATE_STDIO_PATH", str(entry))
        assert _resolve_host_bundle() == entry.resolve()

    def test_explicit_path_missing_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DELEGATE_STDIO_PATH", str(tmp_path / "nope.mjs"))
        with pytest.raises(AgentConfigError, match="does not point to a file"):
            _resolve_host_bundle()

    def test_node_modules_override(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DELEGATE_STDIO_PATH", raising=False)
        monkeypatch.setenv("DELEGATE_STDIO_NODE_MODULES", str(tmp_path))
        entry = tmp_path / "node_modules" / "@uipath" / "delegate-stdio" / "dist" / "delegate_stdio.mjs"
        entry.parent.mkdir(parents=True)
        entry.write_text("#!/usr/bin/env node")
        assert _resolve_host_bundle() == entry.resolve()

    def test_node_modules_override_missing_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DELEGATE_STDIO_PATH", raising=False)
        monkeypatch.setenv("DELEGATE_STDIO_NODE_MODULES", str(tmp_path))
        with pytest.raises(AgentConfigError, match="not found"):
            _resolve_host_bundle()

    def test_no_config_and_not_found_raises_with_search_list(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DELEGATE_STDIO_PATH", raising=False)
        monkeypatch.delenv("DELEGATE_STDIO_NODE_MODULES", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path))
        # A real npm install under the developer's actual home directory must
        # not make this test flaky -- pin every search root to tmp_path.
        monkeypatch.setattr(agent_module, "_candidate_install_roots", lambda: [tmp_path])
        with pytest.raises(AgentConfigError, match="Searched the cwd"):
            _resolve_host_bundle()


class TestStart:
    async def test_missing_node_is_actionable(self, monkeypatch, tmp_path):
        monkeypatch.setattr("shutil.which", lambda _name: None)
        agent = DelegateAgent(_config())
        with pytest.raises(AgentConfigError, match=r"Node\.js was not found"):
            await agent.start(str(tmp_path))

    async def test_init_ok_completes_start(self, patch_exec, tmp_path):
        agent, proc = await _started_agent(patch_exec, [], tmp_path)
        assert agent.working_directory == str(tmp_path)
        sent = proc.stdin.written[0]
        assert sent["cmd"] == "init"
        assert sent["options"]["workingDirectory"] == str(tmp_path)
        assert proc.spawn_args == ("node", str(Path("/opt/delegate_stdio.mjs")))

    async def test_init_error_raises_agent_config_error(self, patch_exec, tmp_path):
        patch_exec([_line({"type": "error", "message": "backendUrl is required", "stack": "Error: ..."})])
        agent = DelegateAgent(_config())
        with pytest.raises(AgentConfigError, match="backendUrl is required"):
            await agent.start(str(tmp_path))
        assert agent._process is None

    async def test_eof_during_init_raises_crash(self, patch_exec, tmp_path):
        patch_exec([])  # EOF immediately
        agent = DelegateAgent(_config())
        with pytest.raises(AgentCrashError):
            await agent.start(str(tmp_path))

    async def test_effort_and_project_id_forwarded(self, patch_exec, tmp_path):
        _agent, proc = await _started_agent(patch_exec, [], tmp_path, effort="high", project_id="proj-1")
        options = proc.stdin.written[0]["options"]
        assert options["effort"] == "high"
        assert options["projectId"] == "proj-1"

    async def test_enable_computer_use_default_false(self, patch_exec, tmp_path):
        _agent, proc = await _started_agent(patch_exec, [], tmp_path)
        assert proc.stdin.written[0]["options"]["enableComputerUse"] is False

    async def test_shell_path_prepend_forwarded(self, patch_exec, tmp_path):
        proc = patch_exec([_line({"type": "init_ok"})])
        agent = DelegateAgent(_config())
        await agent.start(str(tmp_path), env_path_prepend=["/mocks/bin"])
        assert proc.stdin.written[0]["options"]["shellPathPrepend"] == ["/mocks/bin"]

    async def test_unsupported_fields_warn(self, patch_exec, tmp_path, caplog):
        import logging

        caplog.set_level(logging.WARNING)
        await _started_agent(patch_exec, [], tmp_path, system_prompt="be nice")
        assert any("has no Delegate SDK equivalent" in r.message for r in caplog.records)

    async def test_delegate_env_becomes_the_env_option(self, patch_exec, tmp_path, monkeypatch):
        monkeypatch.setenv("DELEGATE_ENV", "alpha")
        _agent, proc = await _started_agent(patch_exec, [], tmp_path)
        assert proc.stdin.written[0]["options"]["env"] == "alpha"

    async def test_skills_enabled_only_with_a_plugin(self, patch_exec, tmp_path):
        _agent, proc = await _started_agent(patch_exec, [], tmp_path)
        assert proc.stdin.written[0]["options"]["enableSkills"] is False

        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        _agent, proc = await _started_agent(
            patch_exec, [], tmp_path, plugins=[{"type": "local", "path": str(plugin_dir)}]
        )
        options = proc.stdin.written[0]["options"]
        assert options["enableSkills"] is True
        assert options["bundledSkillsPath"] == str(plugin_dir / "skills")

    async def test_auth_is_exported_under_the_hosts_own_env_names(self, patch_exec, tmp_path, monkeypatch):
        """The host reads auth from its environment, so nothing auth-shaped goes into init options."""
        monkeypatch.setenv("DELEGATE_AUTH_TOKEN", "tok-1")
        monkeypatch.setenv("AUTH_TOKEN", "stray-npm-token")
        monkeypatch.setenv("TENANT_ID", "tenant-guid")
        monkeypatch.setenv("ORG_ID", "org-guid")
        monkeypatch.setenv("ORG_SLUG", "my-org")
        monkeypatch.setenv("DELEGATE_TENANT_SLUG", "my-tenant")
        _agent, proc = await _started_agent(patch_exec, [], tmp_path)
        env = proc.spawn_kwargs["env"]
        assert env["AUTH_TOKEN"] == "tok-1"
        assert env["TENANT_ID"] == "tenant-guid"
        assert env["ORG_ID"] == "org-guid"
        assert env["ORG_LOGICAL_NAME"] == "my-org"
        assert env["TENANT_NAME"] == "my-tenant"
        assert "auth" not in proc.stdin.written[0]["options"]

    @pytest.mark.parametrize(
        ("token_file_env", "stripped"),
        [
            ({}, False),
            ({"DELEGATE_AUTH_TOKEN_FILE": "/run/token"}, True),
            ({"AUTH_TOKEN_FILE": "/run/token"}, True),
            ({"DELEGATE_AUTH_TOKEN_FILE": f" {os.pathsep} "}, False),
            ({"DELEGATE_AUTH_TOKEN_FILE": "", "AUTH_TOKEN_FILE": "/run/token"}, False),
        ],
        ids=["no-token-file", "token-file", "legacy-token-file", "blank-entries", "empty-shadows-legacy"],
    )
    async def test_gateway_creds_leave_the_host_env_only_when_a_token_file_wins(
        self, patch_exec, tmp_path, monkeypatch, token_file_env, stripped
    ):
        """The host refreshes from LLMGW_* only without a token file, so only then must they stay."""
        for name in ("DELEGATE_AUTH_TOKEN_FILE", "AUTH_TOKEN_FILE"):
            monkeypatch.delenv(name, raising=False)
        for name, value in token_file_env.items():
            monkeypatch.setenv(name, value)
        for name in ("LLMGW_CLIENT_ID", "LLMGW_CLIENT_SECRET", "LLMGW_URL"):
            monkeypatch.setenv(name, "value")
        _agent, proc = await _started_agent(patch_exec, [], tmp_path)
        env = proc.spawn_kwargs["env"]
        for name in ("LLMGW_CLIENT_ID", "LLMGW_CLIENT_SECRET", "LLMGW_URL"):
            assert (name in env) is not stripped


class TestCommunicate:
    async def test_happy_path_text_and_tool(self, patch_exec, tmp_path):
        events = [
            _ev(type="session_start", sessionId="sess-1"),
            _ev(type="thinking", content="let me think"),
            _tool_call("tool-1", "ExecutePowershellCommand", command="ls"),
            _tool_result("tool-1", "file.txt"),
            _ev(type="thinking", content=""),
            _ev(type="message", content="here is my answer", isStepStart=True),
            _ev(type="done", sessionId="sess-1"),
            _result(response="here is my answer", sessionId="sess-2", model="virtuoso-1-5"),
        ]
        agent, proc = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("do something")

        assert record.agent_output == "here is my answer"
        assert record.model_used == "virtuoso-1-5"
        assert not record.crashed
        assert len(record.commands) == 1
        command = record.commands[0]
        assert command.tool_name == "ExecutePowershellCommand"
        assert command.parameters == {"command": "ls"}
        assert command.result_status == "success"
        assert command.result_summary == "file.txt"
        sent = proc.stdin.written[1]
        assert sent == {"cmd": "send", "prompt": "do something", "sessionId": None}
        # The result's session id is remembered for the NEXT turn.
        assert agent._session_id == "sess-2"

    async def test_streamed_message_deltas_form_one_text_block(self, patch_exec, tmp_path):
        events = [
            _ev(type="message", content="P", isStepStart=True),
            _ev(type="message", content="ONG", isStepStart=False),
            _result(response="PONG"),
        ]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi")
        (message,) = record.messages
        text_blocks = [b for b in message.content_blocks if b.block_type == "text"]
        assert [b.text for b in text_blocks] == ["PONG"]
        assert record.assistant_turn_count == 1

    async def test_turn_usages_length_is_the_call_count(self, patch_exec, tmp_path):
        usage = {"input_tokens": 1, "output_tokens": 1}
        events = [
            _tool_call("a"),
            _tool_result("a"),
            _ev(type="message", content="done", isStepStart=True),
            _result(response="done", usage=usage, turnUsages=[usage, usage, usage]),
        ]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi")
        assert record.num_turns == 3

    async def test_send_error_raises_crash(self, patch_exec, tmp_path):
        events = [
            _ev(type="error", error="There was a problem with your request."),
            _ev(type="done", sessionId="s"),
            _line({"type": "error", "message": "Delegate backend error: HTTP 422", "stack": "Error: ..."}),
        ]
        agent, proc = await _started_agent(patch_exec, events, tmp_path)
        with pytest.raises(AgentCrashError, match="HTTP 422"):
            await agent.communicate("hi")
        assert agent.pending_turn is not None
        assert agent.pending_turn.crashed is True
        assert proc._killed
        assert agent._process is None

    @pytest.mark.parametrize(
        ("host_message", "category"),
        [
            (
                "Delegate backend error: HTTP 403: <!DOCTYPE html><title>Continue with UiPath Platform</title>",
                ErrorCategory.AGENT_INVALID_OUTPUT,
            ),
            ("Delegate backend error: SSE connect timeout after 30s", ErrorCategory.AGENT_API_ERROR),
        ],
        ids=["waf-block", "sse-connect-timeout"],
    )
    async def test_known_host_errors_are_recategorized(self, patch_exec, tmp_path, host_message, category):
        # The stderr tail says "timeout"; a rewritten reason must not carry it.
        proc = patch_exec(
            [_line({"type": "init_ok"}), _line({"type": "error", "message": host_message})],
            [b"[agenticApi] SSE connect timeout, retrying\n"],
        )
        agent = DelegateAgent(_config(), task_id="t1")
        await agent.start(str(tmp_path))
        await asyncio.sleep(0)
        assert agent._stderr_lines
        with pytest.raises(AgentCrashError) as excinfo:
            await agent.communicate("hi")
        assert categorize_error(excinfo.value, {"component": "agent"}) is category
        assert "<" not in str(excinfo.value)
        assert proc._killed
        assert agent._process is None

    async def test_session_conflict_drops_the_session_id(self, patch_exec, tmp_path):
        events = [
            _line(
                {
                    "type": "error",
                    "message": "HTTP 409: A reply is already being generated for this conversation.",
                }
            )
        ]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        agent._session_id = "wedged"
        with pytest.raises(AgentCrashError, match="already being generated"):
            await agent.communicate("hi")
        await agent.discard_pending_turn()

        proc = patch_exec([_line({"type": "init_ok"}), _result(response="recovered")])
        record = await agent.communicate("hi again")
        assert record.agent_output == "recovered"
        assert proc.stdin.written[1]["sessionId"] is None

    async def test_eof_mid_turn_raises_crash(self, patch_exec, tmp_path):
        agent, _ = await _started_agent(patch_exec, [], tmp_path)
        with pytest.raises(AgentCrashError, match="closed its output stream"):
            await agent.communicate("hi")

    async def test_non_json_stdout_line_is_skipped_not_fatal(self, patch_exec, tmp_path):
        events = [
            b"[backendUrl] Module loaded - VITE_USE_CLOUD_URL: undefined\n",
            _result(response="done"),
        ]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi")
        assert record.agent_output == "done"

    @pytest.mark.parametrize("status", ["failed", "interrupted"])
    async def test_tool_error_marks_error_status(self, patch_exec, tmp_path, status):
        events = [
            _tool_call("t1"),
            _tool_result("t1", "command not found", status=status),
            _result(response="done"),
        ]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi")
        assert record.commands[0].result_status == "error"
        assert record.commands[0].error_message == "command not found"

    async def test_orphaned_tool_call_is_force_closed(self, patch_exec, tmp_path):
        events = [_tool_call("t1"), _result(response="done")]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi")
        assert record.commands[0].result_status == "unknown"

    async def test_cooperative_stop_ends_cleanly(self, patch_exec, tmp_path):
        events = [
            _ev(type="message", content="partial", isStepStart=True),
            _ev(type="message", content="more", isStepStart=False),
        ]
        agent, proc = await _started_agent(patch_exec, events, tmp_path)
        calls = {"n": 0}

        def should_stop() -> bool:
            calls["n"] += 1
            return calls["n"] >= 1

        record = await agent.communicate("hi", should_stop=should_stop)
        assert not record.crashed
        assert proc._killed  # host abandoned, not resumed
        assert agent._process is None  # dropped so the next turn respawns

    async def test_cooperative_stop_then_next_turn_respawns_and_completes(self, patch_exec, tmp_path):
        events = [_ev(type="message", content="partial", isStepStart=True)]
        agent, _proc = await _started_agent(patch_exec, events, tmp_path)

        await agent.communicate("hi", should_stop=lambda: True)

        patch_exec([_line({"type": "init_ok"}), _result(response="resumed cleanly")])
        record = await agent.communicate("hi again")
        assert record.agent_output == "resumed cleanly"
        assert record.crashed is False

    async def test_should_stop_not_polled_means_full_completion(self, patch_exec, tmp_path):
        events = [_result(response="done")]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi")
        assert record.agent_output == "done"

    async def test_wall_clock_timeout_raises_and_kills(self, patch_exec, tmp_path):
        agent, _proc = await _started_agent(patch_exec, [], tmp_path)

        # timeout=0.0: the loop's own top-of-iteration pre-check fires before
        # any read is attempted, so this pins the deterministic, "already past
        # the deadline" arm exactly (not a race against a real read).
        with pytest.raises(TurnTimeoutError):
            await agent.communicate("hi", timeout=0.0)
        assert agent._process is None  # dropped so the next call respawns

    async def test_timeout_elapsing_mid_read_still_raises_turn_timeout_error(self, patch_exec, tmp_path):
        """The deadline can also elapse WHILE blocked in `_read_next_message`
        (inside `asyncio.wait_for`), not just at the loop's own pre-check --
        this must still surface as `TurnTimeoutError`, not a generic
        `AgentCrashError`, and must still drop the host handle so the next
        turn respawns cleanly instead of reusing a host with a "send" left
        in flight (a real bug found and fixed in review: it used to fall
        through to the generic crash path and leave the process alive)."""
        patch_exec([_line({"type": "init_ok"})], hang_after=True)
        agent = DelegateAgent(_config(), task_id="t1")
        await agent.start(str(tmp_path))

        with pytest.raises(TurnTimeoutError):
            await agent.communicate("hi", timeout=0.1)
        assert agent._process is None

        # A fresh host is spawned for the NEXT turn -- no cross-wiring with
        # the abandoned "send" from the timed-out one.
        patch_exec([_line({"type": "init_ok"}), _result(response="clean turn")])
        record = await agent.communicate("hi again")
        assert record.agent_output == "clean turn"

    @staticmethod
    def _round_trip(n: int) -> list[bytes]:
        """One backend round-trip: an empty reply, then its tool call and result."""
        return [_ev(type="message", content="", isStepStart=True), _tool_call(f"t{n}"), _tool_result(f"t{n}")]

    async def test_max_turns_exhausted(self, patch_exec, tmp_path):
        events = [*self._round_trip(0), *self._round_trip(1), *self._round_trip(2)]
        agent, _proc = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi", max_turns=1)
        assert record.max_turns_exhausted is True
        assert [c.tool_id for c in record.commands if c.result_status == "success"] == ["t0"]
        assert record.num_turns == 2

    async def test_max_turns_counts_round_trips_not_text_chunks(self, patch_exec, tmp_path):
        """The SDK streams a reply as several message events; they are one round-trip."""
        events = [
            *self._round_trip(0),
            _ev(type="thinking", content="wrap up"),
            _ev(type="message", content="all ", isStepStart=True),
            _ev(type="message", content="done", isStepStart=False),
            _result(response="all done"),
        ]
        agent, _proc = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi", max_turns=2)
        assert record.max_turns_exhausted is False
        assert record.num_turns == 2

    async def test_max_turns_stops_a_tool_only_reply_before_its_tool_runs(self, patch_exec, tmp_path):
        """A tool-only reply streams no text event, only its tool call."""
        events = [
            _ev(type="message", content="on it", isStepStart=True),
            *[frame for n in range(3) for frame in (_tool_call(f"t{n}"), _tool_result(f"t{n}"))],
        ]
        agent, _proc = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi", max_turns=2)
        assert record.max_turns_exhausted is True
        assert [c.tool_id for c in record.commands if c.result_status == "success"] == ["t0", "t1"]
        assert record.num_turns == 3

    async def test_max_turns_counts_a_batched_reply_once(self, patch_exec, tmp_path):
        events = [
            _ev(type="message", content="", isStepStart=True),
            _tool_call("a"),
            _tool_call("b"),
            _tool_result("a"),
            _tool_result("b"),
            _ev(type="message", content="done", isStepStart=True),
            _result(response="done"),
        ]
        agent, _proc = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi", max_turns=2)
        assert record.max_turns_exhausted is False
        assert record.num_turns == 2

    async def test_max_turns_still_counts_after_a_tool_never_returns(self, patch_exec, tmp_path):
        """Tool b never returns, so the next call opens on its first new tool call."""
        events = [
            _tool_call("a"),
            _tool_call("b"),
            _tool_result("a"),
            *[frame for n in range(3) for frame in (_tool_call(f"t{n}"), _tool_result(f"t{n}"))],
        ]
        agent, _proc = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi", max_turns=2)
        assert record.max_turns_exhausted is True
        assert [c.tool_id for c in record.commands if c.result_status == "success"] == ["a", "t0"]
        assert record.num_turns == 3

    async def test_communicate_before_start_raises(self):
        agent = DelegateAgent(_config())
        with pytest.raises(RuntimeError, match="start"):
            await agent.communicate("hi")

    async def test_respawns_after_crash_on_next_turn(self, patch_exec, tmp_path):
        agent, _ = await _started_agent(patch_exec, [], tmp_path)
        with pytest.raises(AgentCrashError):
            await agent.communicate("hi")
        await agent.discard_pending_turn()

        # A fresh host is spawned for the retry.
        patch_exec([_line({"type": "init_ok"}), _result(response="recovered")])
        record = await agent.communicate("hi again")
        assert record.agent_output == "recovered"

    async def test_emits_exactly_one_start_and_end_event(self, patch_exec, tmp_path):
        events = [_result(response="done")]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        seen: list[Any] = []

        class _Recorder:
            def on_event(self, event: Any) -> None:
                seen.append(event)

        record = await agent.communicate("hi", stream_callback=_Recorder())
        assert sum(isinstance(e, AgentStartEvent) for e in seen) == 1
        assert sum(isinstance(e, AgentEndEvent) for e in seen) == 1
        end = next(e for e in seen if isinstance(e, AgentEndEvent))
        assert end.status == AgentEndStatus.COMPLETED
        assert record.crashed is False

    @pytest.mark.parametrize(
        ("usage_payload", "expected_uncached_input", "expected_output", "expected_cache_read", "expected_cache_write"),
        [
            ({"input_tokens": 10, "output_tokens": 5}, 10, 5, 0, 0),
            ({"input_tokens": 6, "output_tokens": 5, "cache_read_input_tokens": 4}, 6, 5, 4, 0),
            (
                {
                    "input_tokens": 6,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 4,
                    "cache_creation_input_tokens": 3,
                },
                6,
                5,
                4,
                3,
            ),
        ],
    )
    async def test_usage_bucket_spellings_populate_token_usage(
        self,
        patch_exec,
        tmp_path,
        usage_payload,
        expected_uncached_input,
        expected_output,
        expected_cache_read,
        expected_cache_write,
    ):
        events = [_result(response="done", usage=usage_payload)]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi")
        assert record.token_usage is not None
        assert record.token_usage.uncached_input_tokens == expected_uncached_input
        assert record.token_usage.output_tokens == expected_output
        assert record.token_usage.cache_read_input_tokens == expected_cache_read
        assert record.token_usage.cache_creation_input_tokens == expected_cache_write

    async def test_usage_all_zero_is_none_and_warns(self, patch_exec, tmp_path, caplog):
        events = [_result(response="done", usage={"weird_bucket": 3})]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        with caplog.at_level("WARNING"):
            record = await agent.communicate("hi")
        assert record.token_usage is None
        assert any("usage payload matched none" in r.message for r in caplog.records)

    async def test_cost_wired_into_record_for_configured_model(self, patch_exec, tmp_path):
        events = [_result(response="done", usage={"input_tokens": 1_000_000, "output_tokens": 1_000_000})]
        agent, _ = await _started_agent(patch_exec, events, tmp_path, model="virtuoso-1-5")
        record = await agent.communicate("hi")
        assert record.model_used == "virtuoso-1-5"
        assert record.token_usage is not None
        assert record.token_usage.total_cost_usd == pytest.approx(0.95 + 4.0)

    async def test_send_error_delivers_end_events_to_stream_callback(self, patch_exec, tmp_path):
        events = [_line({"type": "error", "message": "network blip"})]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        seen: list[Any] = []

        class _Recorder:
            def on_event(self, event: Any) -> None:
                seen.append(event)

        with pytest.raises(AgentCrashError):
            await agent.communicate("hi", stream_callback=_Recorder())
        assert sum(isinstance(e, AgentStartEvent) for e in seen) == 1
        assert sum(isinstance(e, AgentEndEvent) for e in seen) == 1
        end = next(e for e in seen if isinstance(e, AgentEndEvent))
        assert end.crashed is True

    async def test_timeout_delivers_end_events_to_stream_callback(self, patch_exec, tmp_path):
        patch_exec([_line({"type": "init_ok"})], hang_after=True)
        agent = DelegateAgent(_config(), task_id="t1")
        await agent.start(str(tmp_path))
        seen: list[Any] = []

        class _Recorder:
            def on_event(self, event: Any) -> None:
                seen.append(event)

        with pytest.raises(TurnTimeoutError):
            await agent.communicate("hi", timeout=0.05, stream_callback=_Recorder())
        assert sum(isinstance(e, AgentStartEvent) for e in seen) == 1
        assert sum(isinstance(e, AgentEndEvent) for e in seen) == 1


class TestStop:
    async def test_stop_sends_destroy_and_marks_finished(self, patch_exec, tmp_path):
        agent, proc = await _started_agent(patch_exec, [], tmp_path)
        await agent.stop()
        assert proc.stdin.written[-1] == {"cmd": "destroy"}
        assert agent.pending_turn is None


class TestKill:
    def test_kill_sync_signals_running_process(self, patch_exec, tmp_path, monkeypatch):
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
        agent = DelegateAgent(_config())
        proc = _FakeProcess([])
        agent._process = proc  # type: ignore[assignment]
        agent.kill_sync()
        assert killed and killed[0][0] == proc.pid
        # Dropped so the NEXT communicate() respawns rather than write into a
        # pipe whose far end this sync path cannot wait to confirm is dead.
        assert agent._process is None

    def test_kill_sync_noop_without_process(self):
        agent = DelegateAgent(_config())
        agent.kill_sync()  # must not raise

    @pytest.mark.skipif(os.name != "posix", reason="process-group teardown is POSIX-only by design")
    def test_kill_sync_sweeps_process_group(self, monkeypatch):
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)
        killpg_calls: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))
        agent = DelegateAgent(_config())
        proc = _FakeProcess([])
        agent._process = proc  # type: ignore[assignment]
        agent._pgid = proc.pid
        agent.kill_sync()
        assert killpg_calls == [(proc.pid, agent_module._SIGKILL)]
        assert agent._pgid is None

    async def test_force_kill_host_sweeps_process_group(self, patch_exec, tmp_path, monkeypatch):
        agent, proc = await _started_agent(patch_exec, [], tmp_path)
        killpg_calls: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))
        await agent._force_kill_host()
        assert proc._killed
        if os.name == "posix":
            assert killpg_calls == [(proc.pid, agent_module._SIGKILL)]


class TestRegistration:
    def test_registered_as_delegate(self):
        registration = AgentRegistry.get("delegate")
        assert registration is not None
        assert registration.agent_class is DelegateAgent
        assert registration.config_class is DelegateAgentConfig

    def test_create_agent_dispatches(self):
        cfg = _config()
        agent = create_agent(AgentKind.DELEGATE, cfg)
        assert isinstance(agent, DelegateAgent)


def test_install_target_names_the_host_package():
    """``agents/delegate/package.json`` is the documented ``npm install`` target the resolver finds."""
    package_json = Path(agent_module.__file__).parent / "delegate" / "package.json"
    assert agent_module._HOST_PACKAGE in json.loads(package_json.read_text())["dependencies"]
