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
    _CODEX_APPROVAL_MODE,
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

    def test_plan_sandbox_mode(self):
        """plan maps to read-only."""
        assert _PERMISSION_MODE_TO_SANDBOX["plan"] == "read-only"

    def test_bypass_permissions_sandbox_mode(self):
        """bypassPermissions maps to full-access."""
        assert _PERMISSION_MODE_TO_SANDBOX["bypassPermissions"] == "full-access"

    def test_default_sandbox_mode(self):
        """default maps to workspace-write."""
        assert _PERMISSION_MODE_TO_SANDBOX["default"] == "workspace-write"

    def test_all_modes_have_sandbox_mapping(self):
        """All permission modes have sandbox_mode mapping."""
        modes = {"default", "acceptEdits", "plan", "bypassPermissions"}
        assert set(_PERMISSION_MODE_TO_SANDBOX.keys()) == modes

    def test_approval_mode_constant_is_deny_all(self):
        """Approval is a single constant (deny_all) — no per-mode mapping; the
        sandbox is the trust boundary. Per-mode ApprovalMode.deny_all resolution
        is covered behaviorally in TestBuildThreadOptions."""
        assert _CODEX_APPROVAL_MODE == "deny_all"

    def test_every_permission_mode_resolves_to_deny_all(self):
        """Regardless of permission_mode, _build_thread_options yields deny_all."""
        from openai_codex.api import ApprovalMode  # pyright: ignore[reportPrivateImportUsage]

        for mode in ("default", "acceptEdits", "plan", "bypassPermissions"):
            agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, permission_mode=mode))
            assert agent._build_thread_options()["approval_mode"] == ApprovalMode.deny_all


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
        from openai_codex.api import ApprovalMode, Sandbox  # pyright: ignore[reportPrivateImportUsage]

        config = parse_agent_config(
            type=AgentKind.CODEX,
            permission_mode="acceptEdits",
        )
        agent = CodexAgent(config)

        options = agent._build_thread_options()

        assert options is not None
        assert options["sandbox"] == Sandbox.workspace_write
        assert options["approval_mode"] == ApprovalMode.deny_all

    def test_build_thread_options_with_plan(self):
        """_build_thread_options builds correct options for plan."""
        from openai_codex.api import ApprovalMode, Sandbox  # pyright: ignore[reportPrivateImportUsage]

        config = parse_agent_config(
            type=AgentKind.CODEX,
            permission_mode="plan",
        )
        agent = CodexAgent(config)

        options = agent._build_thread_options()

        assert options is not None
        assert options["sandbox"] == Sandbox.read_only
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
        """_build_thread_options defaults to workspace_write/deny_all when no permission_mode set."""
        from openai_codex.api import ApprovalMode, Sandbox  # pyright: ignore[reportPrivateImportUsage]

        config = parse_agent_config(type=AgentKind.CODEX)
        agent = CodexAgent(config)

        options = agent._build_thread_options()

        # Should have defaults even without explicit permission_mode
        assert options["sandbox"] == Sandbox.workspace_write
        assert options["approval_mode"] == ApprovalMode.deny_all

    def test_build_thread_options_with_permission_and_tools(self):
        """_build_thread_options combines permission_mode and tool config."""
        from openai_codex.api import ApprovalMode, Sandbox  # pyright: ignore[reportPrivateImportUsage]

        config = parse_agent_config(
            type=AgentKind.CODEX,
            permission_mode="plan",
            allowed_tools=["Read", "Bash"],
        )
        agent = CodexAgent(config)

        options = agent._build_thread_options()

        assert options is not None
        assert options["sandbox"] == Sandbox.read_only
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


def _file_change(status: str, path: str = "out.txt", change_id: str = "fc_1") -> SimpleNamespace:
    """A fileChange item root with the given PatchApplyStatus value string."""
    return SimpleNamespace(
        type="fileChange",
        id=change_id,
        changes=[SimpleNamespace(path=path)],
        status=status,
    )


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
        # Cache-bucket convention: the SDK reports a full prompt count of 100 with
        # 8 cached, so the fresh slice (100 - 8 = 92) is the cache-write and is
        # recorded as cache_creation; input_tokens stays 0 and the cached portion
        # is held in cache_read (see CodexAgent._token_usage_from_sdk).
        assert record.token_usage.input_tokens == 0
        assert record.token_usage.cache_creation_input_tokens == 92
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


def _sdk_usage(inp: int, out: int, cached: int) -> SimpleNamespace:
    """Build the ``sdk_token_usage`` shape ``_token_usage_from_sdk`` consumes
    (only its ``.total`` breakdown is read)."""
    return SimpleNamespace(total=SimpleNamespace(input_tokens=inp, output_tokens=out, cached_input_tokens=cached))


class TestCodexCacheWriteBucketing:
    """CodexAgent buckets the fresh prompt slice (input - cached) as cache-write.

    OpenAI reports ``input_tokens`` inclusive of the cached prefix and bills no
    separate cache-write fee, so the fresh slice == the tokens written to cache
    this call. CodexAgent emits it as ``cache_creation_input_tokens`` with
    ``input_tokens == 0`` (mirroring Anthropic's cached-prompt accounting), and
    the cost is unchanged because cache_write rate == input rate for OpenAI.
    """

    def _agent(self, model: str = "gpt-5.5"):
        return CodexAgent(parse_agent_config(type=AgentKind.CODEX, model=model))

    def test_first_call_all_fresh_is_cache_write(self):
        # cached=0 (cold cache): the whole prompt is freshly processed AND written.
        usage = self._agent()._token_usage_from_sdk(_sdk_usage(1000, 40, 0))
        assert usage is not None
        assert usage.input_tokens == 0
        assert usage.cache_creation_input_tokens == 1000
        assert usage.cache_read_input_tokens == 0

    def test_fully_cached_prompt_has_no_cache_write(self):
        # cached == input: nothing fresh, so no cache-write, all cache-read.
        usage = self._agent()._token_usage_from_sdk(_sdk_usage(1000, 40, 1000))
        assert usage is not None
        assert usage.input_tokens == 0
        assert usage.cache_creation_input_tokens == 0
        assert usage.cache_read_input_tokens == 1000

    def test_partial_cache_fresh_slice_is_cache_write(self):
        usage = self._agent()._token_usage_from_sdk(_sdk_usage(1000, 40, 800))
        assert usage is not None
        assert usage.input_tokens == 0
        assert usage.cache_creation_input_tokens == 200  # 1000 - 800
        assert usage.cache_read_input_tokens == 800

    def test_cached_exceeding_input_clamps_to_zero(self):
        # Defensive: a malformed delta where cached > input must not go negative.
        usage = self._agent()._token_usage_from_sdk(_sdk_usage(100, 5, 150))
        assert usage is not None
        assert usage.cache_creation_input_tokens == 0
        assert usage.cache_read_input_tokens == 150

    def test_cost_prices_cache_write_at_input_rate(self):
        # gpt-5.5: input $5/MTok, cached $0.50/MTok. Fresh 1M as cache-write -> $5.
        usage = self._agent("gpt-5.5")._token_usage_from_sdk(_sdk_usage(1_000_000, 0, 0))
        assert usage is not None
        assert usage.total_cost_usd == pytest.approx(5.0)

    def test_empty_usage_returns_none(self):
        assert self._agent()._token_usage_from_sdk(None) is None
        assert self._agent()._token_usage_from_sdk(SimpleNamespace(total=None)) is None

    async def test_per_message_cache_write_on_first_submessage_only(self):
        # One generation (one tokenUsage) with two tool items + text. The fresh
        # slice is recorded once, on the first sub-message; follow-ups carry 0 so
        # per-message_id sums don't double-count. input_tokens is 0 throughout.
        cmd_root = SimpleNamespace(
            type="commandExecution", id="c1", command="echo hi", exit_code=0, aggregated_output="hi\n", duration_ms=5
        )
        file_root = SimpleNamespace(
            type="fileChange", id="f1", changes=[SimpleNamespace(path="a.py")], status="success"
        )
        # Per-message bucketing reads the per-generation `last` delta (turn-level
        # reads `total`); a real tokenUsage event carries both.
        last = SimpleNamespace(input_tokens=100, output_tokens=40, cached_input_tokens=8, reasoning_output_tokens=0)
        total = SimpleNamespace(input_tokens=100, output_tokens=40, cached_input_tokens=8)
        tok_notif = SimpleNamespace(
            method="thread/tokenUsage/updated",
            payload=SimpleNamespace(token_usage=SimpleNamespace(last=last, total=total)),
        )
        notifications = [
            _item_notification("item/started", cmd_root),
            _item_notification("item/completed", cmd_root),
            _item_notification("item/started", file_root),
            _item_notification("item/completed", file_root),
            _delta("done"),
            tok_notif,
            _turn_completed(),
        ]
        agent = _started_agent(parse_agent_config(type=AgentKind.CODEX), notifications)
        record = await agent.communicate("go")

        assistant_msgs = [m for m in record.messages if hasattr(m, "cache_creation_tokens")]
        assert assistant_msgs, "expected at least one AssistantMessage"
        # No message carries raw input; the fresh slice lives in cache_creation.
        assert all(m.input_tokens == 0 for m in assistant_msgs)
        # Per-generation fresh (100 - 8 = 92) recorded exactly once across the gen.
        assert sum(m.cache_creation_tokens for m in assistant_msgs) == 92
        assert sum(1 for m in assistant_msgs if m.cache_creation_tokens > 0) == 1
        assert sum(m.cache_read_tokens for m in assistant_msgs) == 8
        # Turn-level aggregate matches the per-message sum (single cold gen).
        assert record.token_usage is not None
        assert record.token_usage.cache_creation_input_tokens == 92

    async def test_cache_miss_reread_is_input_not_cache_write(self):
        # Two generations. Gen 2's prompt barely grew (1000 -> 1100) yet only 100
        # tokens hit cache (a miss / eviction, like after a spawn-wait pause), so
        # its fresh slice is 1000. Only the 100-token growth is a genuine cache
        # WRITE; the other 900 are a re-send of previously-cached content and must
        # be plain input — NOT an inflated 1000-token cache-write on the tool call.
        def _gen(item_id: str, text: str, inp: int, out: int, cached: int, tot_in: int, tot_out: int, tot_cached: int):
            msg = SimpleNamespace(type="agentMessage", id=item_id, text=text)
            last = SimpleNamespace(
                input_tokens=inp, output_tokens=out, cached_input_tokens=cached, reasoning_output_tokens=0
            )
            total = SimpleNamespace(input_tokens=tot_in, output_tokens=tot_out, cached_input_tokens=tot_cached)
            tok = SimpleNamespace(
                method="thread/tokenUsage/updated",
                payload=SimpleNamespace(token_usage=SimpleNamespace(last=last, total=total)),
            )
            return [_item_notification("item/started", msg), _item_notification("item/completed", msg), tok]

        notifications = [
            *_gen("m1", "first", inp=1000, out=10, cached=0, tot_in=1000, tot_out=10, tot_cached=0),
            *_gen("m2", "second", inp=1100, out=5, cached=100, tot_in=2100, tot_out=15, tot_cached=100),
            _turn_completed(),
        ]
        agent = _started_agent(parse_agent_config(type=AgentKind.CODEX), notifications)
        record = await agent.communicate("go")

        msgs = [m for m in record.messages if hasattr(m, "cache_creation_tokens")]
        # Gen 1 (cold): all 1000 fresh is a genuine write, no input.
        g1 = next(m for m in msgs if m.message_id and m.message_id.endswith("-msg-0"))
        assert (g1.cache_creation_tokens, g1.input_tokens) == (1000, 0)
        # Gen 2 (cache miss): only the 100-token growth is a write; 900 is input.
        g2 = next(m for m in msgs if m.message_id and m.message_id.endswith("-msg-1"))
        assert (g2.cache_creation_tokens, g2.input_tokens, g2.cache_read_tokens) == (100, 900, 100)
        # The turn total's split matches the per-message split (magnitudes unchanged).
        tu = record.token_usage
        assert tu is not None
        assert (tu.cache_creation_input_tokens, tu.input_tokens, tu.cache_read_input_tokens) == (1100, 900, 100)
        assert tu.output_tokens == 15


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


class _RaisingStream:
    """A stream that yields a few notifications then raises mid-turn, before any
    turn/completed — exercising the crash funnel + token fallback."""

    def __init__(self, notifications):
        self._it = iter(notifications)

    def __iter__(self):
        return self

    def __next__(self):
        nxt = next(self._it)  # raises StopIteration when exhausted
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def close(self):
        pass


class TestCommunicateCrashTokenFallback:
    """On a mid-turn crash the SDK never returns its `total` usage, so _finalize
    falls back to _token_usage_from_messages over the per-generation tokens
    already flushed onto the captured AssistantMessages."""

    async def test_crash_emits_crashed_end_with_token_fallback(self):
        from coder_eval.streaming.events import AgentEndEvent, AgentEndStatus

        cmd_root = SimpleNamespace(
            type="commandExecution", id="c1", command="echo hi", exit_code=0, aggregated_output="hi\n", duration_ms=5
        )
        last = SimpleNamespace(input_tokens=100, output_tokens=40, cached_input_tokens=8, reasoning_output_tokens=0)
        total = SimpleNamespace(input_tokens=100, output_tokens=40, cached_input_tokens=8)
        tok_notif = SimpleNamespace(
            method="thread/tokenUsage/updated",
            payload=SimpleNamespace(token_usage=SimpleNamespace(last=last, total=total)),
        )
        # tokenUsage flushes a message (recording per-gen tokens), THEN the stream
        # raises before any turn/completed -> crash path with messages captured.
        notifications = [
            _item_notification("item/started", cmd_root),
            _item_notification("item/completed", cmd_root),
            _delta("partial work"),
            tok_notif,
            RuntimeError("stream blew up"),
        ]
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
        agent.working_directory = __import__("pathlib").Path(".")
        agent.codex_client = SimpleNamespace(close=lambda: None)
        agent.thread = SimpleNamespace(turn=lambda _u: SimpleNamespace(stream=lambda: _RaisingStream(notifications)))

        captured: list = []

        def _cb(event):
            captured.append(event)

        with pytest.raises(AgentCrashError):
            await agent.communicate("do it", stream_callback=SimpleNamespace(on_event=_cb))

        # A CRASHED AgentEndEvent closes the event tree.
        end_events = [e for e in captured if isinstance(e, AgentEndEvent)]
        assert end_events and end_events[-1].status == AgentEndStatus.CRASHED

        # The pending turn carries the tokens captured before the crash (fallback).
        assert agent.pending_turn is not None
        assert agent.pending_turn.crashed is True
        tu = agent.pending_turn.token_usage
        assert tu is not None
        # Fresh slice 100 - 8 = 92 -> cache_creation; cached 8 -> cache_read; out 40.
        assert tu.input_tokens == 0
        assert tu.cache_creation_input_tokens == 92
        assert tu.cache_read_input_tokens == 8
        assert tu.output_tokens == 40


class TestTokenUsageFromMessages:
    """Direct unit test of the crash-path token fallback summation."""

    def _msg(self, *, cache_creation: int, cache_read: int, output: int):
        from datetime import datetime

        from coder_eval.models import AssistantMessage

        now = datetime.now()
        return AssistantMessage(
            started_at=now,
            completed_at=now,
            generation_duration_ms=0.0,
            input_tokens=0,
            output_tokens=output,
            cache_creation_tokens=cache_creation,
            cache_read_tokens=cache_read,
        )

    def test_sums_token_buckets_across_messages(self):
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="gpt-5.5"))
        messages = [
            self._msg(cache_creation=92, cache_read=8, output=40),
            self._msg(cache_creation=10, cache_read=4, output=6),
        ]

        usage = agent._token_usage_from_messages(messages)
        assert usage is not None
        assert usage.input_tokens == 0
        assert usage.cache_creation_input_tokens == 102
        assert usage.cache_read_input_tokens == 12
        assert usage.output_tokens == 46

    def test_empty_list_returns_none(self):
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
        assert agent._token_usage_from_messages([]) is None

    def test_all_zero_messages_return_none(self):
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
        messages = [self._msg(cache_creation=0, cache_read=0, output=0)]
        assert agent._token_usage_from_messages(messages) is None


def _reasoning_item(text: str = "", item_id: str = "r1") -> SimpleNamespace:
    """A reasoning item root. Text-less items become hidden-CoT placeholders."""
    return SimpleNamespace(type="reasoning", id=item_id, content=[text] if text else [], summary=[])


class TestFlushMessageReasoningSplit:
    """_flush_message splits a generation's output across a thinking sub-message
    (reasoning tokens) and an action/text sub-message, and resolves text-less
    reasoning placeholders to the OpenAI-policy string when reasoning was billed."""

    async def test_reasoning_lands_on_thinking_submessage_with_placeholder(self):
        from coder_eval.models import AssistantMessage

        # last carries reasoning_output_tokens > 0: 100 input / 8 cached, 50 output
        # of which 20 is reasoning. total mirrors last (single generation).
        last = SimpleNamespace(input_tokens=100, output_tokens=50, cached_input_tokens=8, reasoning_output_tokens=20)
        total = SimpleNamespace(input_tokens=100, output_tokens=50, cached_input_tokens=8)
        tok_notif = SimpleNamespace(
            method="thread/tokenUsage/updated",
            payload=SimpleNamespace(token_usage=SimpleNamespace(last=last, total=total)),
        )
        notifications = [
            _item_notification("item/completed", _reasoning_item(text="")),  # text-less placeholder
            _delta("final answer"),
            _item_notification("item/completed", SimpleNamespace(type="agentMessage", id="m1", text="final answer")),
            tok_notif,
            _turn_completed(),
        ]
        agent = _started_agent(parse_agent_config(type=AgentKind.CODEX), notifications)
        record = await agent.communicate("think then answer")

        assistant = [m for m in record.messages if isinstance(m, AssistantMessage)]
        assert assistant
        thinking = [m for m in assistant if any(b.block_type == "thinking" for b in m.content_blocks)]
        action = [m for m in assistant if any(b.block_type != "thinking" for b in m.content_blocks)]
        assert thinking, "expected a thinking sub-message"
        assert action, "expected an action/text sub-message"

        think_msg = thinking[0]
        # Reasoning output landed on the thinking row.
        assert think_msg.output_tokens == 20
        assert think_msg.reasoning_tokens == 20
        # Reasoning was billed -> placeholder text appears on the thinking block.
        think_blocks = [b for b in think_msg.content_blocks if b.block_type == "thinking"]
        assert any(b.thinking == "_Reasoning hidden by OpenAI policy_" for b in think_blocks)

        # The action row gets the remainder of the output (50 - 20 = 30).
        action_msg = action[0]
        assert action_msg.output_tokens == 30

        # Per-message_id sums reconcile to the turn total.
        assert sum(m.output_tokens for m in assistant) == 50
        # Cache write recorded once (first sub-message only): 100 - 8 = 92.
        assert sum(m.cache_creation_tokens for m in assistant) == 92
        assert sum(1 for m in assistant if m.cache_creation_tokens > 0) == 1
        assert sum(m.cache_read_tokens for m in assistant) == 8
        assert all(m.input_tokens == 0 for m in assistant)

        assert record.token_usage is not None
        assert record.token_usage.output_tokens == 50
        assert record.token_usage.cache_creation_input_tokens == 92
        assert record.token_usage.cache_read_input_tokens == 8


def _collab_call(
    tool: str,
    *,
    call_id: str = "call_1",
    model: str | None = None,
    prompt: str | None = None,
    result: str | None = None,
    status: str = "completed",
    child_thread: str = "thread_child",
) -> SimpleNamespace:
    """A collabAgentToolCall item root (Codex's native multi-agent tool).

    ``tool`` is the collab operation value ("spawnAgent" or "wait"); ``result``
    becomes a spawned agent's returned message (under agents_states). The spawn
    and the wait share ``child_thread`` so the result nests under the spawn."""
    states = {}
    if result is not None:
        states[child_thread] = SimpleNamespace(message=result, status="completed")
    return SimpleNamespace(
        type="collabAgentToolCall",
        id=call_id,
        tool=tool,
        model=model,
        prompt=prompt,
        status=status,
        receiver_thread_ids=[child_thread],
        agents_states=states,
    )


class TestCodexCollabSubAgent:
    """Codex spawns sub-agents via collabAgentToolCall (tool='spawnAgent'), not
    Claude's Task tool. The agent must surface these as tool calls AND record one
    (tokenless) sub_agent_usage entry per spawn — the wait/messaging follow-ups
    reuse the same thread and are not new sub-agents."""

    async def test_spawn_records_subagent_usage_and_tool_calls(self, monkeypatch, tmp_path):
        # Isolate CODEX_HOME to a dir with no sessions/ so inner-tool-call recovery
        # short-circuits (no rollout to read) instead of polling the real ~/.codex.
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        spawn = _collab_call("spawnAgent", call_id="call_spawn", model="gpt-5.5", prompt="sum 1..100")
        wait = _collab_call("wait", call_id="call_wait", result="5050")
        notifications = [
            _item_notification("item/started", spawn),
            _item_notification("item/completed", spawn),
            _item_notification("item/started", wait),
            _item_notification("item/completed", wait),
            _delta("done"),
            _turn_completed(),
        ]
        agent = _started_agent(parse_agent_config(type=AgentKind.CODEX), notifications)

        record = await agent.communicate("delegate it")

        # Both collab calls surface as Agent tool calls in the transcript.
        agent_calls = [c for c in record.commands if c.tool_name == "Agent"]
        assert len(agent_calls) == 2
        # Exactly ONE sub-agent recorded (the spawn, not the wait), tokenless,
        # carrying the spawned model.
        assert len(record.sub_agent_usage) == 1
        sa = record.sub_agent_usage[0]
        assert sa.tokens.output_tokens == 0
        assert sa.tokens.total_cost_usd is None
        assert "gpt-5.5" in sa.per_model

        # The sub-agent's returned result ("5050") nests under the spawn call
        # (parent_tool_use_id == the spawn's tool_use_id) — this is what makes the
        # Agent row expandable in the evalboard.
        nested = [m for m in record.messages if getattr(m, "parent_tool_use_id", None) == "call_spawn"]
        assert len(nested) == 1
        assert nested[0].content_blocks[0].text == "5050"

    async def test_wait_only_records_no_subagent(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        # A wait with no preceding spawn (defensive) must not invent a sub-agent.
        wait = _collab_call("wait", call_id="call_wait", result="5050")
        notifications = [
            _item_notification("item/started", wait),
            _item_notification("item/completed", wait),
            _turn_completed(),
        ]
        agent = _started_agent(parse_agent_config(type=AgentKind.CODEX), notifications)

        record = await agent.communicate("wait")

        assert record.sub_agent_usage == []
        assert any(c.tool_name == "Agent" for c in record.commands)

    async def test_orphan_tool_started_without_completed_is_closed_unresolved(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        # A collab op that emits item/started but never item/completed (a real
        # Codex behavior). Without orphan closure its transcript block would have
        # no telemetry and the evalboard would render it "unknown". The agent must
        # force-close it as unresolved at turn end, keeping the name + count.
        orphan = _collab_call("wait", call_id="call_orphan")
        notifications = [
            _item_notification("item/started", orphan),
            # ... no item/completed for the orphan ...
            _delta("done"),
            _turn_completed(),
        ]
        agent = _started_agent(parse_agent_config(type=AgentKind.CODEX), notifications)

        record = await agent.communicate("go")

        # The orphan survives as a named command with 'unknown' status (not dropped,
        # not "unknown" tool name).
        orphans = [c for c in record.commands if c.tool_id == "call_orphan"]
        assert len(orphans) == 1
        assert orphans[0].tool_name == "Agent"
        assert orphans[0].result_status == "unknown"
        # Its transcript tool_use block joins to that command by id, so the
        # evalboard resolves the name instead of falling back to "unknown".
        block_ids = [b.tool_use_id for m in record.messages for b in m.content_blocks if b.block_type == "tool_use"]
        assert "call_orphan" in block_ids


def _write_child_rollout(home, thread_id: str, items: list[dict]) -> None:
    """Write a minimal child-thread rollout JSONL under <home>/sessions.

    The thread id is embedded in the filename verbatim (that's how the agent
    locates it). Each entry is either a raw ResponseItem payload (function_call,
    function_call_output, …) — wrapped as a ``response_item`` line — or, if it
    already carries a top-level ``"type"`` (e.g. an ``event_msg`` token_count),
    written verbatim so generations can be interleaved with token boundaries."""
    import json as _json

    sessions = home / "sessions" / "2026" / "06" / "05"
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / f"rollout-2026-06-05T14-22-36-{thread_id}.jsonl"
    _line_types = {"response_item", "event_msg", "session_meta", "turn_context"}
    lines = [
        _json.dumps(it if it.get("type") in _line_types else {"type": "response_item", "payload": it}) for it in items
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _token_count_event(*, inp: int, cached: int, out: int, tot_in: int, tot_cached: int, tot_out: int) -> dict:
    """A child-rollout ``token_count`` event_msg (a generation boundary)."""
    return {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out},
                "total_token_usage": {
                    "input_tokens": tot_in,
                    "cached_input_tokens": tot_cached,
                    "output_tokens": tot_out,
                },
            },
        },
    }


class TestCodexSubAgentToolRecovery:
    """A Codex sub-agent runs on a child thread whose tool calls never reach the
    parent stream and are dropped from `thread.read` (Limited persistence). But
    the child rollout persists the raw function_call/local_shell_call items, so
    the agent recovers them post-turn and nests them under the spawning Agent
    call — exactly the inner shell command the user wants to see."""

    async def test_inner_shell_command_recovered_and_nested(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        child = "019e0000-aaaa-7000-8000-000000000001"
        _write_child_rollout(
            tmp_path,
            child,
            [
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "c_exec",
                    "arguments": '{"cmd":"python3 -c \\"print(sum(range(1,101)))\\""}',
                },
                {"type": "function_call_output", "call_id": "c_exec", "output": "5050\n"},
            ],
        )
        spawn = _collab_call("spawnAgent", call_id="call_spawn", model="gpt-5.5", child_thread=child)
        wait = _collab_call("wait", call_id="call_wait", result="5050", child_thread=child)
        notifications = [
            _item_notification("item/started", spawn),
            _item_notification("item/completed", spawn),
            _item_notification("item/started", wait),
            _item_notification("item/completed", wait),
            _delta("done"),
            _turn_completed(),
        ]
        agent = _started_agent(parse_agent_config(type=AgentKind.CODEX), notifications)

        record = await agent.communicate("delegate it")

        # The sub-agent's inner Bash command is recovered as telemetry...
        bash = [c for c in record.commands if c.tool_name == "Bash"]
        assert len(bash) == 1
        assert "print(sum(range(1,101)))" in bash[0].parameters.get("command", "")
        assert "5050" in (bash[0].result_summary or "")
        # ...and nests under the spawning Agent call (parent_tool_use_id), so the
        # evalboard renders it as an expandable child of the spawn.
        sub_tool_id = bash[0].tool_id
        nested = [m for m in record.messages if getattr(m, "parent_tool_use_id", None) == "call_spawn"]
        # Both the returned text ("5050") and the recovered tool-call message nest.
        assert any(sub_tool_id in m.tool_use_ids for m in nested)

    async def test_missing_rollout_is_silently_skipped(self, monkeypatch, tmp_path):
        # No rollout written for the child → recovery finds nothing and the turn
        # still completes with just the returned result nested (no crash).
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))  # no sessions/ → fast skip
        child = "019e0000-bbbb-7000-8000-000000000002"
        spawn = _collab_call("spawnAgent", call_id="call_spawn", model="gpt-5.5", child_thread=child)
        wait = _collab_call("wait", call_id="call_wait", result="5050", child_thread=child)
        notifications = [
            _item_notification("item/started", spawn),
            _item_notification("item/completed", spawn),
            _item_notification("item/started", wait),
            _item_notification("item/completed", wait),
            _turn_completed(),
        ]
        agent = _started_agent(parse_agent_config(type=AgentKind.CODEX), notifications)

        record = await agent.communicate("delegate it")

        assert not [c for c in record.commands if c.tool_name == "Bash"]
        assert any(getattr(m, "parent_tool_use_id", None) == "call_spawn" for m in record.messages)

    async def test_generations_carry_per_generation_tokens_in_order(self, monkeypatch, tmp_path):
        # The sub-agent had TWO generations: gen1 produced the python3 tool call
        # (90 output tokens), gen2 produced the "5050" reply (6 output tokens).
        # Recovery must nest them in order, each with its OWN per-generation
        # tokens — not collapse them into tokenless rows (the python3 generation's
        # output tokens must be 90, not 0).
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        child = "019e0000-cccc-7000-8000-000000000003"
        _write_child_rollout(
            tmp_path,
            child,
            [
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "c_py",
                    "arguments": '{"cmd":"python3 -c \\"print(sum(range(1,101)))\\""}',
                },
                {"type": "function_call_output", "call_id": "c_py", "output": "5050\n"},
                # gen1 boundary: the tool-call generation (90 output tokens).
                _token_count_event(inp=11860, cached=3456, out=90, tot_in=11860, tot_cached=3456, tot_out=90),
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "5050"}]},
                # gen2 boundary: the final reply (6 output tokens). Totals cumulative.
                _token_count_event(inp=11999, cached=11648, out=6, tot_in=23859, tot_cached=15104, tot_out=96),
            ],
        )
        spawn = _collab_call("spawnAgent", call_id="call_spawn", model="gpt-5.5", child_thread=child)
        wait = _collab_call("wait", call_id="call_wait", result="5050", child_thread=child)
        notifications = [
            _item_notification("item/started", spawn),
            _item_notification("item/completed", spawn),
            _item_notification("item/started", wait),
            _item_notification("item/completed", wait),
            _delta("done"),
            _turn_completed(),
        ]
        agent = _started_agent(parse_agent_config(type=AgentKind.CODEX), notifications)

        record = await agent.communicate("delegate it")

        nested = [m for m in record.messages if getattr(m, "parent_tool_use_id", None) == "call_spawn"]
        assert len(nested) == 2
        # Gen 1: the python3 tool call, carrying its real 90 output tokens.
        g1 = nested[0]
        assert g1.content_blocks[0].block_type == "tool_use"
        assert g1.content_blocks[0].tool_use_id.endswith("c_py")
        assert g1.output_tokens == 90
        # Gen 2: the "5050" reply AFTER the tool call, with its own 6 output tokens.
        g2 = nested[1]
        assert g2.content_blocks[0].block_type == "text"
        assert g2.content_blocks[0].text == "5050"
        assert g2.output_tokens == 6
        # The aggregate sub_agent_usage tokens come from the child's cumulative total.
        sa = record.sub_agent_usage[0]
        assert sa.tokens.output_tokens == 96
        assert sa.tokens.cache_read_input_tokens == 15104
        assert sa.tool_uses == 1
        # The inner python3 command is recovered as a Bash tool row + folded into total.
        bash = [c for c in record.commands if c.tool_name == "Bash"]
        assert len(bash) == 1 and "range(1,101)" in bash[0].parameters.get("command", "")
        assert record.token_usage is not None and record.token_usage.output_tokens >= 96

    async def test_fold_does_not_double_count_parent_plus_child(self, monkeypatch, tmp_path):
        # The turn total after folding must equal the PARENT total (from the SDK
        # stream) PLUS the recovered child total — exactly, no more, no less.
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        child = "019e0000-dddd-7000-8000-000000000004"
        # Child cumulative: cache_read 15104, fresh 8755 (23859-15104), output 96.
        _write_child_rollout(
            tmp_path,
            child,
            [
                {"type": "function_call", "name": "exec_command", "call_id": "c_py", "arguments": '{"cmd":"x"}'},
                {"type": "function_call_output", "call_id": "c_py", "output": "5050"},
                _token_count_event(inp=23859, cached=15104, out=96, tot_in=23859, tot_cached=15104, tot_out=96),
            ],
        )
        spawn = _collab_call("spawnAgent", call_id="call_spawn", model="gpt-5.5", child_thread=child)
        wait = _collab_call("wait", call_id="call_wait", result="5050", child_thread=child)
        # Parent generation: a single token_count (cached=0 → fresh==input, no
        # resplit ambiguity). Parent total: cache_creation 2000, output 20.
        last = SimpleNamespace(input_tokens=2000, output_tokens=20, cached_input_tokens=0, reasoning_output_tokens=0)
        total = SimpleNamespace(input_tokens=2000, output_tokens=20, cached_input_tokens=0)
        parent_tok = SimpleNamespace(
            method="thread/tokenUsage/updated",
            payload=SimpleNamespace(token_usage=SimpleNamespace(last=last, total=total)),
        )
        notifications = [
            _item_notification("item/started", spawn),
            _item_notification("item/completed", spawn),
            _item_notification("item/started", wait),
            _item_notification("item/completed", wait),
            parent_tok,
            _turn_completed(),
        ]
        agent = _started_agent(parse_agent_config(type=AgentKind.CODEX), notifications)

        record = await agent.communicate("delegate it")

        tu = record.token_usage
        assert tu is not None
        # parent (cw 2000, cr 0, out 20) + child (cw 8755, cr 15104, out 96).
        assert tu.cache_creation_input_tokens == 2000 + 8755
        assert tu.cache_read_input_tokens == 0 + 15104
        assert tu.output_tokens == 20 + 96
        assert tu.input_tokens == 0


class TestCodexGenericToolCapture:
    """Any tool-like item — not just commandExecution/fileChange — must be
    captured as a tool call (MCP, web search, future kinds), so nothing is
    silently dropped."""

    async def test_mcp_and_websearch_items_become_tool_calls(self):
        mcp = SimpleNamespace(
            type="mcpToolCall",
            id="mcp_1",
            server="files",
            tool="read_file",
            arguments={"path": "x"},
            status="completed",
            error=None,
            duration_ms=5,
        )
        web = SimpleNamespace(type="webSearch", id="ws_1", query="codex parallel tool calls")
        notifications = [
            _item_notification("item/started", mcp),
            _item_notification("item/completed", mcp),
            _item_notification("item/started", web),
            _item_notification("item/completed", web),
            _turn_completed(),
        ]
        agent = _started_agent(parse_agent_config(type=AgentKind.CODEX), notifications)

        record = await agent.communicate("use tools")

        names = sorted(c.tool_name for c in record.commands)
        assert names == ["Mcp", "WebSearch"]
        # MCP call carries its server:tool summary; web search carries the query.
        mcp_tel = next(c for c in record.commands if c.tool_name == "Mcp")
        assert "read_file" in (mcp_tel.result_summary or "")

    async def test_failed_mcp_call_records_error(self):
        mcp = SimpleNamespace(
            type="mcpToolCall",
            id="mcp_2",
            server="files",
            tool="read_file",
            arguments={},
            status="failed",
            error="boom",
            duration_ms=1,
        )
        notifications = [
            _item_notification("item/started", mcp),
            _item_notification("item/completed", mcp),
            _turn_completed(),
        ]
        agent = _started_agent(parse_agent_config(type=AgentKind.CODEX), notifications)

        record = await agent.communicate("use tools")

        tel = next(c for c in record.commands if c.tool_name == "Mcp")
        assert tel.result_status == "error"
        assert tel.error_message == "boom"

    async def test_unknown_tool_kind_falls_back_to_raw_type_name(self):
        # A brand-new Codex tool type we don't know about still surfaces.
        novel = SimpleNamespace(type="someNewTool", id="nt_1", status="completed")
        notifications = [
            _item_notification("item/started", novel),
            _item_notification("item/completed", novel),
            _turn_completed(),
        ]
        agent = _started_agent(parse_agent_config(type=AgentKind.CODEX), notifications)

        record = await agent.communicate("use tools")

        assert any(c.tool_name == "someNewTool" for c in record.commands)


class TestApplyPatchTelemetryHonesty:
    """A failed apply_patch must be recorded as an error in telemetry, not
    silently scored as a successful Write (the old ``status != "error"`` test
    never matched the real PatchApplyStatus values). It does NOT fail or retry
    the turn — apply_patch is always accepted now (deny_all, no reviewer), so
    "declined" should not occur, and "failed" (diff mismatch) self-heals within
    the turn; grading checks the actual files."""

    async def test_failed_file_change_records_error_telemetry_without_crashing(self):
        fc = _file_change("failed", path="out.txt")
        notifications = [
            _item_notification("item/started", fc),
            _item_notification("item/completed", fc),
            _delta("apply_patch failed once"),
            _turn_completed(),
        ]
        agent = _started_agent(parse_agent_config(type=AgentKind.CODEX), notifications)

        record = await agent.communicate("write out.txt")

        # Turn completes normally (no crash / no retry), but the Write telemetry
        # honestly reflects the failure.
        assert agent.get_state() == AgentState.WORKING
        writes = [c for c in record.commands if c.tool_name == "Write"]
        assert writes and all(c.result_status == "error" for c in writes)


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
