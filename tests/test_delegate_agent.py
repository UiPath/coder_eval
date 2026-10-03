"""Unit tests for :class:`coder_eval.agents.delegate_agent.DelegateAgent`.

The Node host is never spawned for real: ``asyncio.create_subprocess_exec`` is
patched with a fake process whose stdout/stderr are pre-scripted byte lines and
whose stdin captures every write for assertion, mirroring this repo's existing
CLI-agent test convention (see ``tests/test_opencode_agent.py``). The frame
builders and the fake process live in
``tests/_fixtures/golden_streams/delegate_fixtures.py``, beside the golden scenarios.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest

from coder_eval.agents import delegate_agent as agent_module
from coder_eval.agents.delegate_agent import DelegateAgent, _resolve_host_bundle
from coder_eval.agents.registry import AgentRegistry, create_agent
from coder_eval.criteria.skill_triggered import _engaged_skill_names
from coder_eval.errors import AgentConfigError, AgentCrashError, TurnTimeoutError
from coder_eval.errors.categories import ErrorCategory
from coder_eval.errors.categorization import categorize_error
from coder_eval.errors.retry import should_retry
from coder_eval.models import AgentKind, DelegateAgentConfig
from coder_eval.reports.markdown import collect_agent_settings_rows
from coder_eval.streaming.events import AgentEndEvent, AgentEndStatus, AgentStartEvent
from tests._fixtures.golden_streams.delegate_fixtures import (
    _ev,
    _FakeProcess,
    _host_result,
    _line,
    _result,
    _tool_call,
    _tool_result,
    _totals,
    _usage,
)


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


_DELEGATE_ENV_NAMES = (
    "DELEGATE_STDIO_PATH",
    "DELEGATE_ENV",
    "DELEGATE_BACKEND_URL",
    *(
        f"{prefix}{name}"
        for prefix in ("", "DELEGATE_")
        for name in ("AUTH_TOKEN", "TENANT_ID", "ORG_ID", "ORG_SLUG", "TENANT_SLUG")
    ),
    "ORG_LOGICAL_NAME",
    "TENANT_NAME",
    "BACKEND_URL",
)


@pytest.fixture(autouse=True)
def _clean_delegate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter builds init options from these names, so the developer's own values must not leak in."""
    for name in _DELEGATE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _config(**overrides: Any) -> DelegateAgentConfig:
    return DelegateAgentConfig(type=AgentKind.DELEGATE, **overrides)


async def _started_agent(
    patch_exec, stdout_lines: list[bytes], tmp_path, **config_kwargs
) -> tuple[DelegateAgent, _FakeProcess]:
    proc = patch_exec([_line({"type": "init_ok"}), *stdout_lines])
    agent = DelegateAgent(_config(**config_kwargs), task_id="t1")
    await agent.start(str(tmp_path))
    return agent, proc


def _write_bundle(root: Path) -> Path:
    entry = root / "node_modules" / "@uipath" / "delegate-stdio" / "dist" / "delegate_stdio.mjs"
    entry.parent.mkdir(parents=True)
    entry.write_text("#!/usr/bin/env node")
    return entry


class TestResolveHostBundle:
    @pytest.fixture
    def no_installs(self, tmp_path, monkeypatch) -> Path:
        """Pin every search root to an empty ``tmp_path`` and hide any real global install."""
        monkeypatch.setattr(agent_module, "_candidate_install_roots", lambda: [tmp_path])
        monkeypatch.setattr("shutil.which", lambda _name: None)
        return tmp_path

    def test_explicit_path_env_var(self, tmp_path, monkeypatch):
        entry = tmp_path / "delegate_stdio.mjs"
        entry.write_text("#!/usr/bin/env node")
        monkeypatch.setenv("DELEGATE_STDIO_PATH", str(entry))
        assert _resolve_host_bundle() == entry.resolve()

    def test_explicit_path_to_another_file_raises(self, tmp_path, monkeypatch):
        sdk_entry = tmp_path / "node_modules" / "@uipath" / "delegate-sdk" / "dist" / "index.mjs"
        sdk_entry.parent.mkdir(parents=True)
        sdk_entry.write_text("export {}")
        monkeypatch.setenv("DELEGATE_STDIO_PATH", str(sdk_entry))
        with pytest.raises(AgentConfigError, match="must point at @uipath/delegate-stdio"):
            _resolve_host_bundle()

    def test_explicit_path_missing_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DELEGATE_STDIO_PATH", str(tmp_path / "nope.mjs"))
        with pytest.raises(AgentConfigError, match="does not point to a file"):
            _resolve_host_bundle()

    def test_install_in_a_cwd_ancestor_is_found(self, tmp_path, monkeypatch):
        entry = _write_bundle(tmp_path)
        nested = tmp_path / "project" / "sub"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)
        assert _resolve_host_bundle() == entry.resolve()

    def test_global_install_found_through_a_posix_symlink_shim(self, no_installs, tmp_path, monkeypatch):
        entry = _write_bundle(tmp_path / "prefix" / "lib")
        monkeypatch.setattr("shutil.which", lambda _name: str(entry))
        assert _resolve_host_bundle() == entry.resolve()

    def test_global_install_found_beside_a_windows_cmd_shim(self, no_installs, tmp_path, monkeypatch):
        prefix = tmp_path / "npm"
        entry = _write_bundle(prefix)
        monkeypatch.setattr("shutil.which", lambda _name: str(prefix / "delegate-stdio.cmd"))
        assert _resolve_host_bundle() == entry.resolve()

    def test_local_install_wins_over_global(self, no_installs, tmp_path, monkeypatch):
        local = _write_bundle(tmp_path)
        global_prefix = tmp_path / "npm"
        _write_bundle(global_prefix)
        monkeypatch.setattr("shutil.which", lambda _name: str(global_prefix / "delegate-stdio.cmd"))
        assert _resolve_host_bundle() == local.resolve()

    def test_not_found_raises_with_install_hint(self, no_installs):
        with pytest.raises(AgentConfigError, match=r"npm install -g @uipath/delegate-stdio"):
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

    @pytest.mark.parametrize(
        ("host_message", "error_type"),
        [
            ("Auth required: set AUTH_TOKEN/TENANT_ID/ORG_ID env vars", AgentConfigError),
            ('env="alpha" requires org/tenant slugs. Set ORG_LOGICAL_NAME and TENANT_NAME', AgentConfigError),
            ("401 Invalid token: Signature has expired", AgentConfigError),
            ("Request failed with status code 403", AgentConfigError),
            ("Token endpoint https://cloud.example/token returned HTTP 503: unavailable", AgentCrashError),
            ("fetch failed", AgentCrashError),
            ("connect ECONNREFUSED 127.0.0.1:54013", AgentCrashError),
            ("tenant c7a3f401-0000-4000-8000-000000000403 is not reachable", AgentCrashError),
            ("cache entry expired before the backend replied", AgentCrashError),
        ],
        ids=[
            "no-auth",
            "no-slugs",
            "expired-token",
            "status-403",
            "token-endpoint-5xx",
            "network",
            "port-with-401",
            "guid-with-401-403",
            "unrelated-expired",
        ],
    )
    async def test_only_an_init_error_a_retry_cannot_fix_is_non_retryable(
        self, patch_exec, tmp_path, host_message, error_type
    ):
        patch_exec([_line({"type": "error", "message": host_message, "stack": "Error: ..."})])
        agent = DelegateAgent(_config())
        with pytest.raises(error_type, match="Delegate SDK init failed") as excinfo:
            await agent.start(str(tmp_path))
        assert agent._process is None
        category = categorize_error(excinfo.value, {"component": "agent"})
        assert should_retry(category, 0) is (error_type is AgentCrashError), category

    async def test_a_config_init_error_names_the_variables_coder_eval_reads(self, patch_exec, tmp_path):
        """The host's message names the host's own variables, which coder_eval keeps out of its env."""
        message = 'env="alpha" requires org/tenant slugs. Set ORG_LOGICAL_NAME and TENANT_NAME env vars'
        patch_exec([_line({"type": "error", "message": message})])
        agent = DelegateAgent(_config())
        with pytest.raises(AgentConfigError, match="DELEGATE_ORG_SLUG / DELEGATE_TENANT_SLUG"):
            await agent.start(str(tmp_path))

    async def test_init_timeout_is_retryable(self, patch_exec, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_module, "_INIT_TIMEOUT_SEC", 0.05)
        patch_exec([], hang_after=True)
        agent = DelegateAgent(_config())
        with pytest.raises(AgentCrashError, match="did not respond") as excinfo:
            await agent.start(str(tmp_path))
        assert categorize_error(excinfo.value, {"component": "agent"}) is ErrorCategory.AGENT_CRASH
        assert agent._process is None

    async def test_eof_during_init_raises_crash(self, patch_exec, tmp_path):
        patch_exec([])  # EOF immediately
        agent = DelegateAgent(_config())
        with pytest.raises(AgentCrashError):
            await agent.start(str(tmp_path))

    async def test_effort_and_project_id_forwarded(self, patch_exec, tmp_path):
        _agent, proc = await _started_agent(
            patch_exec, [], tmp_path, sdk_options={"effort": "high"}, project_id="proj-1"
        )
        options = proc.stdin.written[0]["options"]
        assert options["effort"] == "high"
        assert options["projectId"] == "proj-1"

    async def test_sdk_options_are_the_init_options_sent(self, patch_exec, tmp_path):
        """The reports prefer ``sdk_options`` over ``agent_config``, so it must carry the model too."""
        assert DelegateAgent(_config()).get_sdk_options() is None
        agent, proc = await _started_agent(
            patch_exec, [], tmp_path, model="virtuoso-1-5", sdk_options={"effort": "high"}
        )
        assert agent.get_sdk_options() == proc.stdin.written[0]["options"]
        rows = dict(collect_agent_settings_rows(agent.get_sdk_options() or {}, is_sdk=True))
        assert (rows["Model"], rows["Effort"]) == ("virtuoso-1-5", "high")

    async def test_framework_owned_init_keys_win_over_sdk_options(self, patch_exec, tmp_path):
        """A writer that skips validation (``model_copy``) still cannot move the cwd or swap the model."""
        proc = patch_exec([_line({"type": "init_ok"})])
        config = _config(model="virtuoso-1-5").model_copy(
            update={"sdk_options": {"effort": "high", "workingDirectory": "/", "model": "other"}}
        )
        await DelegateAgent(config, task_id="t1").start(str(tmp_path))
        options = proc.stdin.written[0]["options"]
        assert (options["workingDirectory"], options["model"], options["effort"]) == (
            str(tmp_path),
            "virtuoso-1-5",
            "high",
        )

    async def test_enable_computer_use_default_false(self, patch_exec, tmp_path):
        _agent, proc = await _started_agent(patch_exec, [], tmp_path)
        assert proc.stdin.written[0]["options"]["enableComputerUse"] is False

    async def test_shell_path_prepend_forwarded(self, patch_exec, tmp_path):
        proc = patch_exec([_line({"type": "init_ok"})])
        agent = DelegateAgent(_config())
        await agent.start(str(tmp_path), env_path_prepend=["/mocks/bin"])
        assert proc.stdin.written[0]["options"]["shellPathPrepend"] == ["/mocks/bin"]

    async def test_unsupported_fields_warn(self, patch_exec, tmp_path, caplog):
        caplog.set_level(logging.WARNING)
        await _started_agent(patch_exec, [], tmp_path, system_prompt="be nice")
        assert any("has no Delegate SDK equivalent" in r.message for r in caplog.records)

    async def test_delegate_env_becomes_the_env_option(self, patch_exec, tmp_path, monkeypatch):
        monkeypatch.setenv("DELEGATE_ENV", "alpha")
        agent, proc = await _started_agent(patch_exec, [], tmp_path)
        assert proc.stdin.written[0]["options"]["env"] == "alpha"
        assert agent.get_environment_info()["delegate_env"] == "alpha"

    async def test_delegate_backend_url_becomes_the_backend_url_option(self, patch_exec, tmp_path, monkeypatch):
        monkeypatch.setenv("DELEGATE_BACKEND_URL", "https://user:secret@backend.example/delegate_")
        monkeypatch.setenv("DELEGATE_ENV", "alpha")
        agent, proc = await _started_agent(patch_exec, [], tmp_path)
        assert proc.stdin.written[0]["options"]["backendUrl"] == "https://user:secret@backend.example/delegate_"
        info = agent.get_environment_info()
        assert info["delegate_backend_url_host"] == "backend.example"
        assert "delegate_env" not in info

    async def test_the_host_never_reads_its_own_auth_or_backend_names(self, patch_exec, tmp_path, monkeypatch):
        """A BACKEND_URL or ORG_LOGICAL_NAME exported for another tool must not route the host."""
        host_names = ("AUTH_TOKEN", "TENANT_ID", "ORG_ID", "ORG_LOGICAL_NAME", "TENANT_NAME", "BACKEND_URL")
        for name in host_names:
            monkeypatch.setenv(name, "value-for-another-tool")
        _agent, proc = await _started_agent(patch_exec, [], tmp_path)
        env = proc.spawn_kwargs["env"]
        assert not [name for name in host_names if name in env]

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

    async def test_auth_reaches_the_host_as_the_auth_init_option(self, patch_exec, tmp_path, monkeypatch):
        """The token goes to the host on stdin only, so the agent's shells cannot read it from their env."""
        for name, value in {
            "DELEGATE_AUTH_TOKEN": "tok-1",
            "DELEGATE_TENANT_ID": "tenant-guid",
            "DELEGATE_ORG_ID": "org-guid",
            "DELEGATE_ORG_SLUG": "my-org",
            "DELEGATE_TENANT_SLUG": "my-tenant",
        }.items():
            monkeypatch.setenv(name, value)
        _agent, proc = await _started_agent(patch_exec, [], tmp_path)
        assert proc.stdin.written[0]["options"]["auth"] == {
            "accessToken": "tok-1",
            "tenantId": "tenant-guid",
            "organizationId": "org-guid",
            "orgLogicalName": "my-org",
            "tenantName": "my-tenant",
        }
        assert "tok-1" not in proc.spawn_kwargs["env"].values()

    async def test_the_namespaced_name_wins_and_the_bare_name_is_the_fallback(self, patch_exec, tmp_path, monkeypatch):
        monkeypatch.setenv("DELEGATE_AUTH_TOKEN", "namespaced-token")
        monkeypatch.setenv("AUTH_TOKEN", "bare-token")
        monkeypatch.setenv("ORG_SLUG", "bare-org")
        _agent, proc = await _started_agent(patch_exec, [], tmp_path)
        auth = proc.stdin.written[0]["options"]["auth"]
        assert auth == {"accessToken": "namespaced-token", "orgLogicalName": "bare-org"}

    async def test_no_auth_variables_send_no_auth_option(self, patch_exec, tmp_path):
        """The host then falls back to the saved login."""
        _agent, proc = await _started_agent(patch_exec, [], tmp_path)
        assert "auth" not in proc.stdin.written[0]["options"]

    async def test_sdk_options_redact_the_credentials(self, patch_exec, tmp_path, monkeypatch):
        """The run records sdk_options, so the token and a credential-bearing URL must not reach it."""
        monkeypatch.setenv("DELEGATE_AUTH_TOKEN", "tok-1")
        monkeypatch.setenv("DELEGATE_TENANT_ID", "tenant-guid")
        monkeypatch.setenv("DELEGATE_BACKEND_URL", "https://user:secret@backend.example/delegate_")
        agent, _proc = await _started_agent(patch_exec, [], tmp_path)
        options = agent.get_sdk_options() or {}
        assert options["auth"] == ["accessToken", "tenantId"]
        assert options["backendUrl"] == "backend.example"
        assert "tok-1" not in json.dumps(options)

    async def test_a_malformed_backend_url_records_no_host(self, patch_exec, tmp_path, monkeypatch):
        """The orchestrator reads these while it persists task.json, so a raise there would lose the file."""
        monkeypatch.setenv("DELEGATE_BACKEND_URL", "https://[fd00::1:8080/api")
        agent, _proc = await _started_agent(patch_exec, [], tmp_path)
        assert (agent.get_sdk_options() or {})["backendUrl"] is None
        assert agent.get_environment_info()["delegate_backend_url_host"] is None

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

    async def test_crash_category_ignores_the_stderr_tail(self, patch_exec, tmp_path, caplog):
        """The tail names the sandbox, so a task id containing "guardrail" must not make a crash non-retryable."""
        patch_exec(
            [_line({"type": "init_ok"}), _line({"type": "error", "message": "Delegate backend error: terminated"})],
            [b"[handleInit] Working directory: /work/skill-lowcode-guardrail-validator\n"],
        )
        agent = DelegateAgent(_config(), task_id="t1")
        await agent.start(str(tmp_path))
        await asyncio.sleep(0)
        assert agent._stderr_lines
        with caplog.at_level(logging.WARNING), pytest.raises(AgentCrashError) as excinfo:
            await agent.communicate("hi")
        assert categorize_error(excinfo.value, {"component": "agent"}) is ErrorCategory.AGENT_CRASH
        assert any("guardrail-validator" in r.getMessage() for r in caplog.records)

    _SESSION_CONFLICT = _line(
        {"type": "error", "message": "HTTP 409: A reply is already being generated for this conversation."}
    )

    async def test_session_conflict_before_a_finished_turn_drops_the_session_id(self, patch_exec, tmp_path):
        agent, _ = await _started_agent(patch_exec, [self._SESSION_CONFLICT], tmp_path)
        agent._session_id = "wedged"
        with pytest.raises(AgentCrashError, match="already being generated"):
            await agent.communicate("hi")
        await agent.discard_pending_turn()

        proc = patch_exec([_line({"type": "init_ok"}), _result(response="recovered")])
        record = await agent.communicate("hi again")
        assert record.agent_output == "recovered"
        assert proc.stdin.written[1]["sessionId"] is None

    async def test_session_conflict_after_a_finished_turn_keeps_the_session_id(self, patch_exec, tmp_path):
        """A new conversation would continue the task without the earlier turns, yet be graded as one trajectory."""
        events = [_result(response="one", sessionId="sess-1"), self._SESSION_CONFLICT]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        await agent.communicate("turn 1")
        with pytest.raises(AgentCrashError, match="already being generated"):
            await agent.communicate("turn 2")
        await agent.discard_pending_turn()

        proc = patch_exec([_line({"type": "init_ok"}), _result(response="two", sessionId="sess-1")])
        await agent.communicate("turn 2")
        assert proc.stdin.written[1]["sessionId"] == "sess-1"

    async def test_session_conflict_keeps_a_session_pinned_in_config(self, patch_exec, tmp_path):
        agent, _ = await _started_agent(patch_exec, [self._SESSION_CONFLICT], tmp_path, session_id="pinned")
        with pytest.raises(AgentCrashError, match="already being generated"):
            await agent.communicate("hi")
        assert agent._session_id == "pinned"

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

    async def test_result_for_an_unresolved_tool_takes_its_name_and_args(self, patch_exec, tmp_path):
        """The SDK sends no tool_call for a tool name it cannot resolve, only the failed result."""
        not_found = _ev(
            type="tool_result",
            toolId="s/x",
            toolName="Bash",
            toolResult={
                "responseType": "error",
                "content": "Tool Bash not found.",
                "name": "Bash",
                "args": {"command": "ls"},
            },
            toolStatus="failed",
        )
        agent, _ = await _started_agent(patch_exec, [not_found, _result(response="done")], tmp_path)
        record = await agent.communicate("hi")
        [command] = record.commands
        assert (command.tool_name, command.parameters, command.result_status) == ("Bash", {"command": "ls"}, "error")

    async def test_load_skill_is_recorded_as_the_canonical_skill_call(self, patch_exec, tmp_path):
        """Skill criteria read `Skill` + `skill`; the host sends `LoadSkill` + `name`."""
        events = [
            _tool_call("s1", "LoadSkill", name="uipath-rpa", plugin=""),
            _tool_result("s1"),
            _result(response="done"),
        ]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi")
        [command] = record.commands
        assert (command.tool_name, command.parameters) == ("Skill", {"skill": "uipath-rpa", "plugin": ""})
        assert _engaged_skill_names(command) == {"uipath-rpa"}

    async def test_skill_api_call_keeps_its_name_arg(self, patch_exec, tmp_path):
        """The host already reports ExecuteSkillApi as `Skill`; its `name` is not a skill load."""
        events = [
            _tool_call("s1", "Skill", name="process-knowledge", method="search"),
            _tool_result("s1"),
            _result(response="done"),
        ]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi")
        [command] = record.commands
        assert (command.tool_name, command.parameters) == ("Skill", {"name": "process-knowledge", "method": "search"})
        assert _engaged_skill_names(command) == set()

    async def test_result_with_only_the_sdk_id_fallback_name_stays_unknown(self, patch_exec, tmp_path):
        orphan = _ev(type="tool_result", toolId="s/x", toolName="s/x", toolResult="ok", toolStatus="completed")
        agent, _ = await _started_agent(patch_exec, [orphan, _result(response="done")], tmp_path)
        record = await agent.communicate("hi")
        assert record.commands[0].tool_name == "unknown"

    async def test_a_result_after_a_new_tool_call_still_matches_its_call(self, patch_exec, tmp_path):
        """The SDK can start a new tool while earlier results are pending; those results still arrive."""
        events = [
            _tool_call("a", path="x.md"),
            _tool_call("b", path="y.md"),
            _tool_result("a"),
            _tool_call("c", command="ls"),
            _tool_result("b"),
            _tool_result("c"),
            _result(response="done"),
        ]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi")
        assert [(c.tool_id, c.tool_name, c.result_status) for c in record.commands] == [
            ("a", "shell", "success"),
            ("b", "shell", "success"),
            ("c", "shell", "success"),
        ]
        assert record.commands[1].parameters == {"path": "y.md"}

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

    @staticmethod
    def _billed_round_trip(n: int) -> list[bytes]:
        """A round-trip as the host writes it: the call's usage frame precedes its tool call and result."""
        return [
            _ev(type="message", content="", isStepStart=True),
            _usage(10 * (n + 1), n + 1),
            _tool_call(f"t{n}"),
            _tool_result(f"t{n}"),
        ]

    async def test_max_turns_cut_keeps_the_usage_of_the_calls_under_the_cap(self, patch_exec, tmp_path, caplog):
        events = [
            *self._billed_round_trip(0),
            *self._billed_round_trip(1),
            *self._billed_round_trip(2),
            _result(response="done", usage={"input_tokens": 60, "output_tokens": 6}),
        ]
        agent, _proc = await _started_agent(patch_exec, events, tmp_path, model="virtuoso-1-5")
        with caplog.at_level(logging.WARNING):
            record = await agent.communicate("hi", max_turns=2)
        assert record.max_turns_exhausted is True
        assert record.num_turns == 3
        assert record.token_usage is not None
        assert (record.token_usage.uncached_input_tokens, record.token_usage.output_tokens) == (30, 3)
        assert record.token_usage.total_cost_usd is not None
        assert not any("usage" in r.message for r in caplog.records)

    async def test_early_stop_keeps_the_usage_of_the_finished_calls(self, patch_exec, tmp_path):
        events = [*self._billed_round_trip(0), _ev(type="message", content="next", isStepStart=True)]
        agent, _proc = await _started_agent(patch_exec, events, tmp_path)
        seen = {"n": 0}

        def should_stop() -> bool:
            seen["n"] += 1
            return seen["n"] == 4

        record = await agent.communicate("hi", should_stop=should_stop)
        assert record.token_usage is not None
        assert (record.token_usage.uncached_input_tokens, record.token_usage.output_tokens) == (10, 1)

    async def test_result_usage_replaces_the_frame_sum(self, patch_exec, tmp_path):
        """The result's usage is the turn total, so adding it to the frames would count each call twice."""
        events = [
            *self._billed_round_trip(0),
            _ev(type="message", content="done", isStepStart=True),
            _usage(20, 2),
            _result(response="done", usage={"input_tokens": 30, "output_tokens": 3}),
        ]
        agent, _proc = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi")
        assert record.token_usage is not None
        assert (record.token_usage.uncached_input_tokens, record.token_usage.output_tokens) == (30, 3)

    async def test_cut_turn_from_a_host_without_usage_frames_warns(self, patch_exec, tmp_path, caplog):
        events = [*self._round_trip(0), *self._round_trip(1), _result(response="done", usage={"output_tokens": 5})]
        agent, _proc = await _started_agent(patch_exec, events, tmp_path)
        with caplog.at_level(logging.WARNING):
            record = await agent.communicate("hi", max_turns=1)
        assert record.max_turns_exhausted is True
        assert record.token_usage is None
        assert any("1 finished model call(s)" in r.message for r in caplog.records)

    async def test_early_stop_before_any_finished_call_does_not_warn(self, patch_exec, tmp_path, caplog):
        events = [_ev(type="message", content="partial", isStepStart=True)]
        agent, _proc = await _started_agent(patch_exec, events, tmp_path)
        with caplog.at_level(logging.WARNING):
            await agent.communicate("hi", should_stop=lambda: True)
        assert not any("usage" in r.message for r in caplog.records)

    async def test_completed_turn_without_usage_warns(self, patch_exec, tmp_path, caplog):
        events = [_ev(type="message", content="hi", isStepStart=True), _result(response="hi", usage=None)]
        agent, _proc = await _started_agent(patch_exec, events, tmp_path)
        with caplog.at_level(logging.WARNING):
            record = await agent.communicate("hi")
        assert record.token_usage is None
        assert any("no token usage for a turn with 1 model call(s)" in r.message for r in caplog.records)

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

    @pytest.mark.parametrize(
        ("host_message", "error_type"),
        [
            ("Auth required: token expired", AgentConfigError),
            ("fetch failed", AgentCrashError),
        ],
        ids=["auth", "network"],
    )
    async def test_respawn_init_error_keeps_the_start_classification(
        self, patch_exec, tmp_path, host_message, error_type
    ):
        """The same init error must be as retryable on a mid-run respawn as at ``start()``."""
        agent, _ = await _started_agent(patch_exec, [], tmp_path)
        with pytest.raises(AgentCrashError):
            await agent.communicate("hi")
        await agent.discard_pending_turn()

        patch_exec([_line({"type": "error", "message": host_message})])
        with pytest.raises(error_type, match="Delegate SDK init failed"):
            await agent.communicate("hi again")
        assert agent._process is None

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

    async def test_reconciliation_invariant(self, patch_exec, tmp_path):
        """Summing the four buckets across messages must equal token_usage exactly."""
        events = [
            _ev(type="message", content="", isStepStart=True),
            _usage(100, 20, cache_read=40),
            _tool_call("a"),
            _tool_result("a"),
            _ev(type="message", content="done", isStepStart=True),
            _usage(50, 30, cache_write=10),
            _host_result("done", [_totals(100, 20, cache_read=40), _totals(50, 30, cache_write=10)]),
        ]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        record = await agent.communicate("hi")

        usage = record.token_usage
        assert usage is not None
        assert (usage.cache_read_input_tokens, usage.cache_creation_input_tokens) == (40, 10)
        assert sum(m.input_tokens for m in record.messages) == usage.uncached_input_tokens
        assert sum(m.output_tokens for m in record.messages) == usage.output_tokens
        assert sum(m.cache_creation_tokens for m in record.messages) == usage.cache_creation_input_tokens
        assert sum(m.cache_read_tokens for m in record.messages) == usage.cache_read_input_tokens

    async def test_usage_all_zero_is_none_and_warns(self, patch_exec, tmp_path, caplog):
        events = [_result(response="done", usage={"weird_bucket": 3})]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        with caplog.at_level("WARNING"):
            record = await agent.communicate("hi")
        assert record.token_usage is None
        assert any("usage payload matched none" in r.message for r in caplog.records)

    async def test_zero_usage_frame_in_known_buckets_does_not_warn(self, patch_exec, tmp_path, caplog):
        """A call can report zero tokens; that is not a renamed bucket."""
        events = [_usage(0, 0), _result(response="done")]
        agent, _ = await _started_agent(patch_exec, events, tmp_path)
        with caplog.at_level("WARNING"):
            record = await agent.communicate("hi")
        assert record.token_usage is None
        assert not any("usage payload matched none" in r.message for r in caplog.records)

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

    async def test_stop_cancels_a_blocked_stdout_drain_without_an_error_log(
        self, patch_exec, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.setattr(agent_module, "_STOP_TIMEOUT_SEC", 0.01)
        patch_exec([_line({"type": "init_ok"})], hang_after=True)
        agent = DelegateAgent(_config(), task_id="t1")
        await agent.start(str(tmp_path))
        with caplog.at_level(logging.ERROR):
            await agent.stop()
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


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

    async def test_kill_drops_the_handle_so_the_next_turn_respawns(self, patch_exec, tmp_path):
        agent, proc = await _started_agent(patch_exec, [], tmp_path)
        await agent.kill()
        assert proc._killed
        assert agent._process is None


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


def test_install_target_names_the_host_package_and_is_searched():
    """``agents/delegate/package.json`` is the documented ``npm install`` target the resolver finds."""
    package_json = agent_module._AGENT_INSTALL_ROOT / "package.json"
    assert agent_module._HOST_PACKAGE in json.loads(package_json.read_text())["dependencies"]
    assert agent_module._AGENT_INSTALL_ROOT in agent_module._candidate_install_roots()
