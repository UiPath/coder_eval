"""Tests for CodexAgent implementation using official OpenAI Codex SDK.

REQUIRES: `pip install 'coder-eval[codex]'` (or `uv sync --extra codex`).
The Codex SDK is an optional extra; this entire test module is skipped when
it isn't installed, so `make test` / CI stay green without the extra.
"""

import pytest


# The Codex SDK is an optional extra; skip the whole module when it isn't
# installed so `make test` / CI stay green without `uv sync --extra codex`.
pytest.importorskip("openai_codex")

from coder_eval.agent import AgentState
from coder_eval.agents.codex_agent import (
    _CLAUDE_TO_CODEX_TOOL_MAP,
    _PERMISSION_MODE_TO_APPROVAL,
    _PERMISSION_MODE_TO_SANDBOX,
    CodexAgent,
)
from coder_eval.models import AgentConfig, AgentKind, parse_agent_config


class TestCodexAgentInitialization:
    """Test CodexAgent initialization and basic properties."""

    def test_codex_agent_initialization(self):
        """Test that CodexAgent can be initialized with valid config."""
        config = parse_agent_config(
            type=AgentKind.CODEX,
            permission_mode="acceptEdits",
            allowed_tools=["Bash", "Read", "Write"],
        )
        agent = CodexAgent(config)

        assert agent.config == config
        assert agent.codex_client is None
        assert agent.get_state() == AgentState.WORKING
        assert agent.pending_turn is None

    def test_codex_agent_with_disallowed_tools(self):
        """Test initialization with disallowed_tools."""
        config = parse_agent_config(
            type=AgentKind.CODEX,
            permission_mode="acceptEdits",
            disallowed_tools=["Write", "Edit", "Bash"],
        )
        agent = CodexAgent(config)

        assert agent.config.disallowed_tools == ["Write", "Edit", "Bash"]
        assert agent.get_state() == AgentState.WORKING

    def test_codex_agent_with_all_permission_modes(self):
        """Test that all permission_mode values are accepted."""
        for mode in ["default", "acceptEdits", "plan", "bypassPermissions"]:
            config = parse_agent_config(type=AgentKind.CODEX, permission_mode=mode)
            agent = CodexAgent(config)
            assert agent.config.permission_mode == mode

    def test_codex_agent_returns_working_state(self):
        """Test that new agent is in WORKING state."""
        config = parse_agent_config(type=AgentKind.CODEX)
        agent = CodexAgent(config)

        assert agent.get_state() == AgentState.WORKING

    def test_codex_agent_with_instance_name(self):
        """Test that agent can be initialized with a custom instance name."""
        config = parse_agent_config(type=AgentKind.CODEX)
        agent = CodexAgent(config, instance_name="custom_codex")

        # Just verify it doesn't crash with custom instance_name
        assert agent.get_state() == AgentState.WORKING


class TestToolNameMapping:
    """Test tool name mapping between Claude Code and Codex SDK."""

    def test_bash_maps_to_shell(self):
        """Bash tool maps to shell in Codex."""
        assert _CLAUDE_TO_CODEX_TOOL_MAP["Bash"] == "shell"

    def test_write_maps_to_apply_patch(self):
        """Write tool maps to apply_patch in Codex."""
        assert _CLAUDE_TO_CODEX_TOOL_MAP["Write"] == "apply_patch"

    def test_edit_maps_to_apply_patch(self):
        """Edit tool maps to apply_patch in Codex."""
        assert _CLAUDE_TO_CODEX_TOOL_MAP["Edit"] == "apply_patch"

    def test_read_maps_to_shell(self):
        """Read tool maps to shell in Codex (no dedicated read tool)."""
        assert _CLAUDE_TO_CODEX_TOOL_MAP["Read"] == "shell"

    def test_grep_maps_to_shell(self):
        """Grep tool maps to shell in Codex."""
        assert _CLAUDE_TO_CODEX_TOOL_MAP["Grep"] == "shell"

    def test_glob_maps_to_shell(self):
        """Glob tool maps to shell in Codex."""
        assert _CLAUDE_TO_CODEX_TOOL_MAP["Glob"] == "shell"

    def test_all_tools_mapped(self):
        """Verify all expected tools have mappings."""
        expected_tools = {"Bash", "Write", "Edit", "Read", "Grep", "Glob"}
        actual_tools = set(_CLAUDE_TO_CODEX_TOOL_MAP.keys())
        assert expected_tools.issubset(actual_tools)


class TestPermissionModeMapping:
    """Test permission_mode to sandbox/approval_mode mapping."""

    def test_accept_edits_sandbox_mode(self):
        """acceptEdits maps to workspace-write."""
        assert _PERMISSION_MODE_TO_SANDBOX["acceptEdits"] == "workspace-write"

    def test_accept_edits_approval_mode(self):
        """acceptEdits maps to auto_review approval mode."""
        assert _PERMISSION_MODE_TO_APPROVAL["acceptEdits"] == "auto_review"

    def test_plan_sandbox_mode(self):
        """plan maps to read-only."""
        assert _PERMISSION_MODE_TO_SANDBOX["plan"] == "read-only"

    def test_plan_approval_mode(self):
        """plan maps to deny_all approval mode."""
        assert _PERMISSION_MODE_TO_APPROVAL["plan"] == "deny_all"

    def test_bypass_permissions_sandbox_mode(self):
        """bypassPermissions maps to danger-full-access."""
        assert _PERMISSION_MODE_TO_SANDBOX["bypassPermissions"] == "danger-full-access"

    def test_bypass_permissions_approval_mode(self):
        """bypassPermissions maps to auto_review approval mode."""
        assert _PERMISSION_MODE_TO_APPROVAL["bypassPermissions"] == "auto_review"

    def test_default_sandbox_mode(self):
        """default maps to workspace-write."""
        assert _PERMISSION_MODE_TO_SANDBOX["default"] == "workspace-write"

    def test_default_approval_mode(self):
        """default maps to auto_review approval mode."""
        assert _PERMISSION_MODE_TO_APPROVAL["default"] == "auto_review"

    def test_all_modes_have_sandbox_mapping(self):
        """All permission modes have sandbox_mode mapping."""
        modes = {"default", "acceptEdits", "plan", "bypassPermissions"}
        assert set(_PERMISSION_MODE_TO_SANDBOX.keys()) == modes

    def test_all_modes_have_approval_mapping(self):
        """All permission modes have approval_policy mapping."""
        modes = {"default", "acceptEdits", "plan", "bypassPermissions"}
        assert set(_PERMISSION_MODE_TO_APPROVAL.keys()) == modes


class TestCodexEnvironmentConfiguration:
    """Test _build_codex_env: only CODEX_API_KEY travels via env."""

    def test_build_codex_env_returns_none_without_key(self, monkeypatch):
        """No CODEX_API_KEY -> None (base URL alone is not enough)."""
        monkeypatch.delenv("CODEX_API_KEY", raising=False)
        monkeypatch.setenv("CODEX_BASE_URL", "https://custom.api/v1")

        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
        assert agent._build_codex_env() is None

    def test_build_codex_env_with_api_key(self, monkeypatch):
        """CODEX_API_KEY is delivered via env."""
        monkeypatch.setenv("CODEX_API_KEY", "test-key-123")

        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
        env = agent._build_codex_env()
        assert env == {"CODEX_API_KEY": "test-key-123"}

    def test_build_codex_env_omits_base_url(self, monkeypatch):
        """Base URL is applied via provider config, never via env."""
        monkeypatch.setenv("CODEX_API_KEY", "k")
        monkeypatch.setenv("CODEX_BASE_URL", "https://custom.api/v1")

        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
        env = agent._build_codex_env()
        assert env == {"CODEX_API_KEY": "k"}
        assert "CODEX_BASE_URL" not in env

    def test_build_codex_env_ignores_openai_and_azure_keys(self, monkeypatch):
        """Only CODEX_API_KEY is read; OPENAI_*/AZURE_* are not."""
        monkeypatch.delenv("CODEX_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key")

        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
        assert agent._build_codex_env() is None


class TestCustomProviderRouting:
    """Test that CODEX_BASE_URL injects a custom model provider."""

    def test_no_base_url_means_no_provider(self, monkeypatch):
        monkeypatch.delenv("CODEX_BASE_URL", raising=False)
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, permission_mode="acceptEdits"))
        opts = agent._build_thread_options()
        assert "model_provider" not in opts
        assert "model_providers" not in opts.get("config", {})

    def test_base_url_injects_custom_provider(self, monkeypatch):
        monkeypatch.setenv("CODEX_BASE_URL", "https://gw.local/openai/v1")
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="gpt-5-codex"))
        opts = agent._build_thread_options()
        assert opts["model_provider"] == "custom"
        assert opts["model"] == "gpt-5-codex"
        provider = opts["config"]["model_providers"]["custom"]
        assert provider["base_url"] == "https://gw.local/openai/v1"
        assert provider["env_key"] == "CODEX_API_KEY"
        assert provider["wire_api"] == "responses"

    def test_model_falls_back_to_codex_model_setting(self, monkeypatch):
        """agent.model wins; otherwise the settings-backed CODEX_MODEL is used."""
        from coder_eval.config import settings

        monkeypatch.setattr(settings, "codex_model", "fallback-model")
        # No model on the config -> fallback.
        assert CodexAgent(parse_agent_config(type=AgentKind.CODEX))._effective_model() == "fallback-model"
        # Explicit model wins over the fallback.
        assert CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="pinned"))._effective_model() == "pinned"


class TestThreadOptions:
    """Test _build_thread_options method."""

    def test_build_thread_options_with_accept_edits(self):
        """_build_thread_options builds correct options for acceptEdits."""
        from openai_codex.api import ApprovalMode, SandboxMode

        config = parse_agent_config(
            type=AgentKind.CODEX,
            permission_mode="acceptEdits",
        )
        agent = CodexAgent(config)

        options = agent._build_thread_options()

        assert options is not None
        assert options["sandbox"] == SandboxMode.workspace_write
        assert options["approval_mode"] == ApprovalMode.auto_review

    def test_build_thread_options_with_plan(self):
        """_build_thread_options builds correct options for plan."""
        from openai_codex.api import ApprovalMode, SandboxMode

        config = parse_agent_config(
            type=AgentKind.CODEX,
            permission_mode="plan",
        )
        agent = CodexAgent(config)

        options = agent._build_thread_options()

        assert options is not None
        assert options["sandbox"] == SandboxMode.read_only
        assert options["approval_mode"] == ApprovalMode.deny_all

    def test_build_thread_options_with_allowed_tools(self):
        """_build_thread_options includes enabled_tools from allowed_tools."""
        config = parse_agent_config(
            type=AgentKind.CODEX,
            allowed_tools=["Bash", "Read", "Write"],
        )
        agent = CodexAgent(config)

        options = agent._build_thread_options()

        assert options is not None
        assert "config" in options
        assert options["config"]["enabled_tools"] == ["shell", "shell", "apply_patch"]

    def test_build_thread_options_with_disallowed_tools(self):
        """_build_thread_options includes disabled_tools from disallowed_tools."""
        config = parse_agent_config(
            type=AgentKind.CODEX,
            disallowed_tools=["Write", "Edit", "Bash"],
        )
        agent = CodexAgent(config)

        options = agent._build_thread_options()

        assert options is not None
        assert "config" in options
        assert options["config"]["disabled_tools"] == ["apply_patch", "apply_patch", "shell"]

    def test_build_thread_options_with_no_permission_mode(self):
        """_build_thread_options defaults to workspace_write/auto_review when no permission_mode set."""
        from openai_codex.api import ApprovalMode, SandboxMode

        config = parse_agent_config(type=AgentKind.CODEX)
        agent = CodexAgent(config)

        options = agent._build_thread_options()

        # Should have defaults even without explicit permission_mode
        assert options["sandbox"] == SandboxMode.workspace_write
        assert options["approval_mode"] == ApprovalMode.auto_review

    def test_build_thread_options_with_permission_and_tools(self):
        """_build_thread_options combines permission_mode and tool config."""
        from openai_codex.api import ApprovalMode, SandboxMode

        config = parse_agent_config(
            type=AgentKind.CODEX,
            permission_mode="plan",
            allowed_tools=["Read", "Bash"],
        )
        agent = CodexAgent(config)

        options = agent._build_thread_options()

        assert options is not None
        assert options["sandbox"] == SandboxMode.read_only
        assert options["approval_mode"] == ApprovalMode.deny_all
        assert options["config"]["enabled_tools"] == ["shell", "shell"]


@pytest.mark.asyncio
async def test_discard_pending_turn():
    """Test discard_pending_turn clears pending_turn and decrements iteration."""
    from coder_eval.models import TurnRecord

    config = parse_agent_config(type=AgentKind.CODEX)
    agent = CodexAgent(config)

    partial = TurnRecord(
        iteration=1,
        user_input="test",
        agent_output="<partial>",
        crashed=True,
    )
    agent._iteration = 1
    agent.pending_turn = partial

    await agent.discard_pending_turn()

    assert agent.pending_turn is None
    assert agent._iteration == 0


def test_get_state_returns_current_state():
    """Test get_state returns the agent's current state."""
    config = parse_agent_config(type=AgentKind.CODEX)
    agent = CodexAgent(config)

    assert agent.get_state() == AgentState.WORKING

    agent._state = AgentState.ERROR
    assert agent.get_state() == AgentState.ERROR


# ---------------------------------------------------------------------------
# Fake Codex SDK objects for exercising communicate() / _run_turn_with_streaming
# without a live SDK. These mirror the notification shapes the real stream emits.
# ---------------------------------------------------------------------------

import time  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from openai_codex.generated.v2_all import Turn, TurnCompletedNotification  # noqa: E402

from coder_eval.errors import AgentCrashError, TurnTimeoutError  # noqa: E402


def _item_notification(method: str, root: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(method=method, payload=SimpleNamespace(item=SimpleNamespace(root=root)))


def _delta(text: str) -> SimpleNamespace:
    return SimpleNamespace(method="item/agentMessage/delta", payload=SimpleNamespace(delta=text))


def _token_usage(inp: int, out: int, cached: int) -> SimpleNamespace:
    total = SimpleNamespace(input_tokens=inp, output_tokens=out, cached_input_tokens=cached)
    return SimpleNamespace(
        method="thread/tokenUsage/updated",
        payload=SimpleNamespace(token_usage=SimpleNamespace(total=total)),
    )


def _turn_completed(duration_ms: int = 1500) -> SimpleNamespace:
    turn = Turn(
        id="turn_1",
        status="completed",
        duration_ms=duration_ms,
        started_at=None,
        completed_at=None,
        error=None,
        items=[],
        items_view="full",
    )
    payload = TurnCompletedNotification(thread_id="th_1", turn=turn)
    return SimpleNamespace(method="turn/completed", payload=payload)


class _FakeTurnHandle:
    def __init__(self, notifications):
        self._notifications = notifications
        self.interrupted = False

    def stream(self):
        return iter(self._notifications)

    def interrupt(self):
        self.interrupted = True


class _FakeThread:
    def __init__(self, notifications):
        self._notifications = notifications
        self.last_handle = None

    def turn(self, _user_input):
        self.last_handle = _FakeTurnHandle(self._notifications)
        return self.last_handle


def _started_agent(config: AgentConfig, notifications) -> CodexAgent:
    """Build a CodexAgent wired with fakes, bypassing the real SDK start()."""
    agent = CodexAgent(config)
    agent.working_directory = __import__("pathlib").Path(".")
    agent.codex_client = SimpleNamespace(close=lambda: None)
    agent.thread = _FakeThread(notifications)
    return agent


class TestCommunicateHappyPath:
    """End-to-end communicate() with a fake stream."""

    async def test_happy_path_collects_output_commands_and_tokens(self):
        cmd_root = SimpleNamespace(
            type="commandExecution",
            id="cmd_abc",
            command="echo hi",
            exit_code=0,
            aggregated_output="hi\n",
            duration_ms=12,
        )
        file_root = SimpleNamespace(
            type="fileChange",
            id="fc_1",
            changes=[SimpleNamespace(path="foo.py")],
            status="success",
        )
        notifications = [
            _item_notification("item/started", cmd_root),
            _item_notification("item/completed", cmd_root),
            _item_notification("item/started", file_root),
            _item_notification("item/completed", file_root),
            _delta("Hello "),
            _delta("world"),
            _token_usage(100, 40, 8),
            _turn_completed(duration_ms=2000),
        ]
        agent = _started_agent(parse_agent_config(type=AgentKind.CODEX), notifications)

        record = await agent.communicate("do it")

        assert record.agent_output == "Hello world"
        # One shell command + one file change both recorded as telemetry.
        tool_names = sorted(c.tool_name for c in record.commands)
        assert tool_names == ["Bash", "Write"]
        # Distinct sequence numbers (no collision / freeze).
        assert sorted(c.sequence_number for c in record.commands) == [0, 1]
        assert record.token_usage is not None
        assert record.token_usage.input_tokens == 100
        assert record.token_usage.cache_read_input_tokens == 8
        assert agent.get_state() == AgentState.WORKING
        assert agent.pending_turn is None
        assert agent._active_turn_handle is None

    async def test_state_resets_to_working_after_a_prior_error(self):
        notifications = [_delta("ok"), _turn_completed()]
        agent = _started_agent(parse_agent_config(type=AgentKind.CODEX), notifications)
        agent._state = AgentState.ERROR

        await agent.communicate("retry")

        assert agent.get_state() == AgentState.WORKING


class TestCommunicateCrashFunnel:
    """A turn that never completes funnels through the pending-turn contract."""

    async def test_missing_turn_completed_raises_agent_crash_with_pending(self):
        # No turn/completed notification -> RuntimeError inside, surfaced as crash.
        notifications = [_delta("partial")]
        agent = _started_agent(parse_agent_config(type=AgentKind.CODEX), notifications)

        with pytest.raises(AgentCrashError):
            await agent.communicate("do it")

        assert agent.pending_turn is not None
        assert agent.pending_turn.crashed is True
        assert agent.get_state() == AgentState.ERROR

        # discard rolls back the iteration bump (flag-only branch still works).
        await agent.discard_pending_turn()
        assert agent._iteration == 0

    async def test_thread_start_failure_funnels_through_crash(self):
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
        agent.working_directory = __import__("pathlib").Path(".")
        agent.codex_client = SimpleNamespace(close=lambda: None)
        agent.thread = None  # force thread_start path

        def _boom(**_kwargs):
            raise RuntimeError("bad creds")

        agent.codex_client.thread_start = _boom

        with pytest.raises(AgentCrashError):
            await agent.communicate("do it")

        assert agent.pending_turn is not None
        await agent.discard_pending_turn()
        assert agent._iteration == 0


class _BlockingStream:
    """A stream whose first next() blocks longer than the turn timeout, so the
    watchdog cancels the awaiting task — verifying the to_thread offload lets the
    timeout actually land."""

    def __iter__(self):
        return self

    def __next__(self):
        time.sleep(5)
        raise StopIteration

    def close(self):
        pass


class TestCommunicateTimeoutFunnel:
    async def test_timeout_raises_turn_timeout_with_pending(self):
        handle = SimpleNamespace(stream=lambda: _BlockingStream(), interrupt=lambda: None)
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
        agent.working_directory = __import__("pathlib").Path(".")
        agent.codex_client = SimpleNamespace(close=lambda: None)
        agent.thread = SimpleNamespace(turn=lambda _u: handle)

        with pytest.raises(TurnTimeoutError):
            await agent.communicate("do it", timeout=0.2)

        assert agent.pending_turn is not None
        assert agent.pending_turn.crashed is True
        assert agent.get_state() == AgentState.ERROR


class TestDiscardIdempotency:
    async def test_double_discard_only_rolls_back_once(self):
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
        agent._iteration = 3
        agent._iteration_was_incremented = True

        await agent.discard_pending_turn()
        assert agent._iteration == 2

        await agent.discard_pending_turn()
        assert agent._iteration == 2  # idempotent


class TestTeardown:
    async def test_stop_closes_client(self):
        closed = {"n": 0}
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
        agent.codex_client = SimpleNamespace(close=lambda: closed.__setitem__("n", closed["n"] + 1))

        await agent.stop()

        assert closed["n"] == 1
        assert agent.codex_client is None
        assert agent.get_state() == AgentState.FINISHED

    def test_kill_sync_interrupts_turn_and_closes(self):
        closed = {"n": 0}
        handle = _FakeTurnHandle([])
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
        agent.codex_client = SimpleNamespace(close=lambda: closed.__setitem__("n", closed["n"] + 1))
        agent._active_turn_handle = handle

        agent.kill_sync()

        assert handle.interrupted is True
        assert closed["n"] == 1


class TestOrchestratorDispatch:
    async def test_create_agent_returns_codex_agent(self, tmp_path):
        from coder_eval.models import TaskDefinition
        from coder_eval.orchestrator import Orchestrator

        task = TaskDefinition(
            task_id="codex-dispatch",
            description="dispatch test",
            initial_prompt="hi",
            success_criteria=[{"type": "file_exists", "path": "out.txt", "description": "out.txt must exist"}],
            agent=parse_agent_config(type=AgentKind.CODEX),
        )
        orch = Orchestrator(task=task, run_dir=tmp_path / "run", variant_id="t")
        agent = await orch._create_agent()
        assert isinstance(agent, CodexAgent)
