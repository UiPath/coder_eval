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
from typing import Any

import pytest

from coder_eval.agents import delegate_agent as agent_module
from coder_eval.agents.delegate_agent import DelegateAgent, _resolve_sdk_entry
from coder_eval.agents.registry import AgentRegistry, create_agent
from coder_eval.errors import AgentConfigError, AgentCrashError, TurnTimeoutError
from coder_eval.models import AgentKind, DelegateAgentConfig
from coder_eval.streaming.events import AgentEndEvent, AgentEndStatus, AgentStartEvent


def _line(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj) + "\n").encode("utf-8")


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
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/node")
        monkeypatch.setattr(agent_module, "_resolve_sdk_entry", lambda: agent_module._HOST_SCRIPT)
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


class TestResolveSdkEntry:
    def test_explicit_path_env_var(self, tmp_path, monkeypatch):
        entry = tmp_path / "index.mjs"
        entry.write_text("export const DelegateAgent = class {};")
        monkeypatch.setenv("DELEGATE_SDK_PATH", str(entry))
        assert _resolve_sdk_entry() == entry.resolve()

    def test_explicit_path_missing_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DELEGATE_SDK_PATH", str(tmp_path / "nope.mjs"))
        with pytest.raises(AgentConfigError, match="does not point to a file"):
            _resolve_sdk_entry()

    def test_node_modules_override(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DELEGATE_SDK_PATH", raising=False)
        monkeypatch.setenv("DELEGATE_SDK_NODE_MODULES", str(tmp_path))
        entry = tmp_path / "node_modules" / "@uipath" / "delegate-sdk" / "dist" / "index.mjs"
        entry.parent.mkdir(parents=True)
        entry.write_text("export const DelegateAgent = class {};")
        assert _resolve_sdk_entry() == entry.resolve()

    def test_node_modules_override_missing_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DELEGATE_SDK_PATH", raising=False)
        monkeypatch.setenv("DELEGATE_SDK_NODE_MODULES", str(tmp_path))
        with pytest.raises(AgentConfigError, match="not found"):
            _resolve_sdk_entry()

    def test_no_config_and_not_found_raises_with_search_list(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DELEGATE_SDK_PATH", raising=False)
        monkeypatch.delenv("DELEGATE_SDK_NODE_MODULES", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path))
        # A real npm install under the developer's actual home directory must
        # not make this test flaky -- pin every search root to tmp_path.
        monkeypatch.setattr(agent_module, "_candidate_install_roots", lambda: [tmp_path])
        with pytest.raises(AgentConfigError, match="Searched the cwd"):
            _resolve_sdk_entry()


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

    async def test_init_error_raises_agent_config_error(self, patch_exec, tmp_path):
        patch_exec([_line({"type": "init_error", "message": "backendUrl is required"})])
        agent = DelegateAgent(_config())
        with pytest.raises(AgentConfigError, match="backendUrl is required"):
            await agent.start(str(tmp_path))

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

    async def test_auth_token_forwards_ids_but_not_slugs_by_default(self, patch_exec, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTH_TOKEN", "tok-1")
        monkeypatch.setenv("TENANT_ID", "tenant-guid")
        monkeypatch.setenv("ORG_ID", "org-guid")
        monkeypatch.delenv("ORG_SLUG", raising=False)
        monkeypatch.delenv("TENANT_SLUG", raising=False)
        _agent, proc = await _started_agent(patch_exec, [], tmp_path)
        auth = proc.stdin.written[0]["options"]["auth"]
        assert auth == {"accessToken": "tok-1", "tenantId": "tenant-guid", "organizationId": "org-guid"}

    async def test_org_and_tenant_slug_forwarded_into_auth(self, patch_exec, tmp_path, monkeypatch):
        """Regression test: the SDK's `environment` resolution reads
        organizationName/tenantName off the `auth` object it's given, NOT
        ORG_SLUG/TENANT_SLUG from process.env directly (confirmed live against
        the installed @uipath/delegate-sdk) -- so these two env vars must be
        translated into `auth` fields here, or `environment: "alpha"` fails
        init with "--env alpha needs org/tenant slugs" even with a valid
        AUTH_TOKEN/TENANT_ID/ORG_ID triple.
        """
        monkeypatch.setenv("AUTH_TOKEN", "tok-1")
        monkeypatch.setenv("TENANT_ID", "tenant-guid")
        monkeypatch.setenv("ORG_ID", "org-guid")
        monkeypatch.setenv("ORG_SLUG", "my-org")
        monkeypatch.setenv("TENANT_SLUG", "my-tenant")
        _agent, proc = await _started_agent(patch_exec, [], tmp_path)
        auth = proc.stdin.written[0]["options"]["auth"]
        assert auth == {
            "accessToken": "tok-1",
            "tenantId": "tenant-guid",
            "organizationId": "org-guid",
            "organizationName": "my-org",
            "tenantName": "my-tenant",
        }


class TestCommunicate:
    async def test_happy_path_text_and_tool(self, patch_exec, tmp_path):
        events = [
            _line({"type": "thinking", "content": "let me think"}),
            _line({"type": "message", "content": "here is my answer"}),
            _line({"type": "tool_call", "toolName": "Bash", "toolId": "tool-1", "input": {"command": "ls"}}),
            _line({"type": "tool_result", "toolId": "tool-1", "output": "file.txt"}),
            _line({"type": "send_ok", "result": "here is my answer", "sessionId": "sess-1"}),
        ]
        agent, proc = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("do something")

        assert record.agent_output == "here is my answer"
        assert not record.crashed
        assert len(record.commands) == 1
        assert record.commands[0].tool_name == "Bash"
        assert record.commands[0].result_status == "success"
        sent = proc.stdin.written[1]
        assert sent == {"cmd": "send", "prompt": "do something", "sessionId": None}
        # Session id from send_ok is remembered for the NEXT turn.
        assert agent._session_id == "sess-1"

    async def test_send_error_raises_crash(self, patch_exec, tmp_path):
        events = [_line({"type": "send_error", "message": "network blip"})]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        with pytest.raises(AgentCrashError, match="network blip"):
            await agent.communicate("hi")
        assert agent.pending_turn is not None
        assert agent.pending_turn.crashed is True

    async def test_fatal_raises_crash(self, patch_exec, tmp_path):
        events = [_line({"type": "fatal", "message": "uncaught exception: boom"})]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        with pytest.raises(AgentCrashError):
            await agent.communicate("hi")

    async def test_eof_mid_turn_raises_crash(self, patch_exec, tmp_path):
        agent, _ = await _started_agent(patch_exec, [], tmp_path)
        with pytest.raises(AgentCrashError, match="closed its output stream"):
            await agent.communicate("hi")

    async def test_non_json_stdout_line_is_skipped_not_fatal(self, patch_exec, tmp_path):
        events = [
            b"[backendUrl] Module loaded - VITE_USE_CLOUD_URL: undefined\n",
            _line({"type": "send_ok", "result": "done"}),
        ]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi")
        assert record.agent_output == "done"

    async def test_tool_error_marks_error_status(self, patch_exec, tmp_path):
        events = [
            _line({"type": "tool_call", "toolName": "Bash", "toolId": "t1", "input": {}}),
            _line({"type": "tool_result", "toolId": "t1", "error": "command not found"}),
            _line({"type": "send_ok", "result": "done"}),
        ]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi")
        assert record.commands[0].result_status == "error"
        assert record.commands[0].error_message == "command not found"

    async def test_orphaned_tool_call_is_force_closed(self, patch_exec, tmp_path):
        events = [
            _line({"type": "tool_call", "toolName": "Bash", "toolId": "t1", "input": {}}),
            _line({"type": "send_ok", "result": "done"}),
        ]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi")
        assert record.commands[0].result_status == "unknown"

    async def test_cooperative_stop_ends_cleanly(self, patch_exec, tmp_path):
        events = [
            _line({"type": "message", "content": "partial"}),
            _line({"type": "message", "content": "more"}),
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
        events = [_line({"type": "message", "content": "partial"})]
        agent, _proc = await _started_agent(patch_exec, events, tmp_path)

        await agent.communicate("hi", should_stop=lambda: True)

        patch_exec([_line({"type": "init_ok"}), _line({"type": "send_ok", "result": "resumed cleanly"})])
        record = await agent.communicate("hi again")
        assert record.agent_output == "resumed cleanly"
        assert record.crashed is False

    async def test_should_stop_not_polled_means_full_completion(self, patch_exec, tmp_path):
        events = [_line({"type": "send_ok", "result": "done"})]
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
        patch_exec([_line({"type": "init_ok"}), _line({"type": "send_ok", "result": "clean turn"})])
        record = await agent.communicate("hi again")
        assert record.agent_output == "clean turn"

    @staticmethod
    def _round_trip(n: int) -> list[bytes]:
        """One backend round-trip: an empty reply, then its tool call and result."""
        return [
            _line({"type": "message", "content": ""}),
            _line({"type": "tool_call", "toolId": f"t{n}", "toolName": "shell", "input": {}}),
            _line({"type": "tool_result", "toolId": f"t{n}", "output": "ok"}),
        ]

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
            _line({"type": "thinking", "content": "wrap up"}),
            _line({"type": "message", "content": "all "}),
            _line({"type": "message", "content": "done"}),
            _line({"type": "send_ok", "result": "all done"}),
        ]
        agent, _proc = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi", max_turns=2)
        assert record.max_turns_exhausted is False
        assert record.num_turns == 2

    async def test_max_turns_stops_a_tool_only_reply_before_its_tool_runs(self, patch_exec, tmp_path):
        """A tool-only reply streams no text event, only its tool call."""
        events = [
            _line({"type": "message", "content": "on it"}),
            *[
                _line({"type": kind, "toolId": f"t{n}", "toolName": "shell", "output": "ok"})
                for n in range(3)
                for kind in ("tool_call", "tool_result")
            ],
        ]
        agent, _proc = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi", max_turns=2)
        assert record.max_turns_exhausted is True
        assert [c.tool_id for c in record.commands if c.result_status == "success"] == ["t0", "t1"]
        assert record.num_turns == 3

    async def test_max_turns_counts_a_batched_reply_once(self, patch_exec, tmp_path):
        events = [
            _line({"type": "message", "content": ""}),
            _line({"type": "tool_call", "toolId": "a", "toolName": "shell"}),
            _line({"type": "tool_call", "toolId": "b", "toolName": "shell"}),
            _line({"type": "tool_result", "toolId": "a", "output": "ok"}),
            _line({"type": "tool_result", "toolId": "b", "output": "ok"}),
            _line({"type": "message", "content": "done"}),
            _line({"type": "send_ok", "result": "done"}),
        ]
        agent, _proc = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi", max_turns=2)
        assert record.max_turns_exhausted is False
        assert record.num_turns == 2

    async def test_max_turns_still_counts_after_a_tool_never_returns(self, patch_exec, tmp_path):
        """Tool b never returns, so the next call opens on its first new tool call."""
        events = [
            _line({"type": "tool_call", "toolId": "a", "toolName": "shell"}),
            _line({"type": "tool_call", "toolId": "b", "toolName": "shell"}),
            _line({"type": "tool_result", "toolId": "a", "output": "ok"}),
            *[
                _line({"type": kind, "toolId": f"t{n}", "toolName": "shell", "output": "ok"})
                for n in range(3)
                for kind in ("tool_call", "tool_result")
            ],
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
        patch_exec([_line({"type": "init_ok"}), _line({"type": "send_ok", "result": "recovered"})])
        record = await agent.communicate("hi again")
        assert record.agent_output == "recovered"

    async def test_emits_exactly_one_start_and_end_event(self, patch_exec, tmp_path):
        events = [_line({"type": "send_ok", "result": "done"})]
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
            ({"promptTokens": 10, "completionTokens": 5}, 10, 5, 0, 0),
            ({"promptTokens": 10, "completionTokens": 5, "promptTokensCached": 4}, 6, 5, 4, 0),
            (
                {"promptTokens": 10, "completionTokens": 5, "promptTokensCached": 4, "cacheCreationTokens": 3},
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
        events = [_line({"type": "send_ok", "result": "done", "usage": usage_payload})]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi")
        assert record.token_usage is not None
        assert record.token_usage.uncached_input_tokens == expected_uncached_input
        assert record.token_usage.output_tokens == expected_output
        assert record.token_usage.cache_read_input_tokens == expected_cache_read
        assert record.token_usage.cache_creation_input_tokens == expected_cache_write

    async def test_usage_all_zero_is_none_and_warns(self, patch_exec, tmp_path, caplog):
        events = [_line({"type": "send_ok", "result": "done", "usage": {"weird_bucket": 3}})]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        with caplog.at_level("WARNING"):
            record = await agent.communicate("hi")
        assert record.token_usage is None
        assert any("usage payload matched none" in r.message for r in caplog.records)

    async def test_cost_wired_into_record_for_configured_model(self, patch_exec, tmp_path):
        events = [
            _line(
                {
                    "type": "send_ok",
                    "result": "done",
                    "usage": {"promptTokens": 1_000_000, "completionTokens": 1_000_000},
                }
            )
        ]
        agent, _ = await _started_agent(patch_exec, events, tmp_path, model="virtuoso-1-5")
        record = await agent.communicate("hi")
        assert record.model_used == "virtuoso-1-5"
        assert record.token_usage is not None
        assert record.token_usage.total_cost_usd == pytest.approx(0.95 + 4.0)

    async def test_send_error_delivers_end_events_to_stream_callback(self, patch_exec, tmp_path):
        events = [_line({"type": "send_error", "message": "network blip"})]
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


def test_host_script_ships_with_the_package():
    """Every test above patches the fixture to return ``_HOST_SCRIPT`` without
    ever checking the file exists on disk -- a build-config change that dropped
    it from the wheel would surface only as a runtime MODULE_NOT_FOUND inside a
    live run, never here.
    """
    assert agent_module._HOST_SCRIPT.is_file()
