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
    _CODEX_APPROVAL_MODE,
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


# NOTE: SDK-independent tests (TestToolNameMapping, TestCodexTurnState) live in
# test_codex_agent_unit.py so they run in the base Quality Gate without the
# optional `openai_codex` extra. Keep new pure-logic tests there, not here.


class TestApprovalMode:
    """Approval is a single constant (deny_all) for every permission mode — the
    sandbox (always full-access; see TestSandboxAlwaysFullAccess) is the trust
    boundary, not a per-command reviewer."""

    def test_approval_mode_constant_is_deny_all(self):
        """Approval is a single constant (deny_all) — no per-mode mapping. Per-mode
        ApprovalMode.deny_all resolution is covered behaviorally below."""
        assert _CODEX_APPROVAL_MODE == "deny_all"

    def test_every_permission_mode_resolves_to_deny_all(self):
        """Regardless of permission_mode, _build_thread_options yields deny_all."""
        from openai_codex.api import ApprovalMode  # pyright: ignore[reportPrivateImportUsage]

        for mode in ("default", "acceptEdits", "plan", "bypassPermissions"):
            agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, permission_mode=mode))
            assert agent._build_thread_options()["approval_mode"] == ApprovalMode.deny_all


class TestSandboxAlwaysFullAccess:
    """coder_eval always owns the isolation boundary (a docker container or an
    ephemeral per-task tempdir), so Codex always runs full-access: for every
    permission mode, with or without the container marker, and on both Unix and
    Windows (where Codex has no OS sandbox at all). Its own in-process OS sandbox
    is redundant and, on constrained CI hosts / Windows, actively breaks — silently
    scoring 0. Hard isolation of untrusted actions is the docker driver's job."""

    # The in_container / os_name dimensions are invariance-by-construction guards:
    # _build_thread_options reads neither CODER_EVAL_IN_CONTAINER nor os.name after
    # the always-full-access change, so these axes assert that a future edit can't
    # reintroduce an env/OS-conditional sandbox branch without turning a case red.
    @pytest.mark.parametrize("mode", ["default", "acceptEdits", "plan", "bypassPermissions"])
    @pytest.mark.parametrize("in_container", [True, False])
    @pytest.mark.parametrize("os_name", ["posix", "nt"])
    def test_sandbox_is_full_access(self, monkeypatch, mode, in_container, os_name):
        import os as _os

        from openai_codex.api import Sandbox  # pyright: ignore[reportPrivateImportUsage]

        if in_container:
            monkeypatch.setenv("CODER_EVAL_IN_CONTAINER", "1")
        else:
            monkeypatch.delenv("CODER_EVAL_IN_CONTAINER", raising=False)
        monkeypatch.setattr(_os, "name", os_name)
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, permission_mode=mode))
        assert agent._build_thread_options()["sandbox"] == Sandbox("full-access")


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

    def test_build_codex_env_materializes_codex_home_under_drop_without_mocks(self, monkeypatch, tmp_path):
        """Regression: under the docker uid-drop, CODEX_HOME must be materialized even
        with NO mock PATH dirs (so ``_login_shell_home`` is None). The setup used to be
        nested inside the login-shell block and was skipped for mock-free tasks, so the
        dropped codex process EACCES'd initializing sqlite state under the root-owned
        ~/.codex."""
        monkeypatch.setenv("CODEX_API_KEY", "k")
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ch"))
        config = parse_agent_config(type=AgentKind.CODEX)
        config.agent_run_uid = 2000
        agent = CodexAgent(config)
        assert agent._login_shell_home is None  # no mocks -> login-shell home not set up
        env = agent._build_codex_env()
        assert env is not None
        assert env.get("CODEX_HOME") == str(tmp_path / "ch")
        assert (tmp_path / "ch").is_dir()  # created so the dropped uid can write it

    def test_build_codex_env_no_codex_home_without_drop_or_mocks(self, monkeypatch):
        """Off the drop and without mocks, CODEX_HOME stays unset (codex default)."""
        monkeypatch.setenv("CODEX_API_KEY", "k")
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
        env = agent._build_codex_env()
        assert env is not None and "CODEX_HOME" not in env

    def test_build_codex_env_prepends_path_when_env_path_prepend_set(self, monkeypatch):
        """env_path_prepend dirs land at the FRONT of PATH, in order, parent appended.

        This is the mock-shadowing contract: sandbox mock CLIs must resolve before
        the real ones. An inverted join order (mocks at the back) must fail here.
        """
        import os

        monkeypatch.setenv("CODEX_API_KEY", "k")
        monkeypatch.setenv("PATH", "/parent/bin")

        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
        agent._env_path_prepend = ["/sandbox/mocks", "/sandbox/bins"]

        env = agent._build_codex_env()
        assert env is not None
        assert env["CODEX_API_KEY"] == "k"
        assert env["PATH"] == f"/sandbox/mocks{os.pathsep}/sandbox/bins{os.pathsep}/parent/bin"

    def test_build_codex_env_returns_path_only_when_no_api_key(self, monkeypatch):
        """Prepend dirs set but no CODEX_API_KEY -> {"PATH": ...} alone, not None."""
        import os

        monkeypatch.delenv("CODEX_API_KEY", raising=False)
        monkeypatch.setenv("PATH", "/parent/bin")

        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
        agent._env_path_prepend = ["/sandbox/mocks"]

        env = agent._build_codex_env()
        assert env == {"PATH": f"/sandbox/mocks{os.pathsep}/parent/bin"}

    def test_build_codex_env_no_prepend_omits_path(self, monkeypatch):
        """Default (no env_path_prepend) never adds a PATH key — only the API key travels."""
        monkeypatch.setenv("CODEX_API_KEY", "k")
        monkeypatch.setenv("PATH", "/parent/bin")

        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
        env = agent._build_codex_env()
        assert env == {"CODEX_API_KEY": "k"}
        assert "PATH" not in env

    def test_build_codex_env_resolves_path_key_case_insensitively(self, monkeypatch):
        """A non-uppercase PATH key (e.g. Windows 'Path') is reused, not duplicated."""
        import os as _os

        from coder_eval.agents import codex_agent

        monkeypatch.setattr(codex_agent.os, "environ", {"Path": "/parent/bin"})

        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
        agent._env_path_prepend = ["/sandbox/mocks"]

        env = agent._build_codex_env()
        assert env == {"Path": f"/sandbox/mocks{_os.pathsep}/parent/bin"}

    @pytest.mark.asyncio
    async def test_start_propagates_env_path_prepend(self, monkeypatch, tmp_path):
        """start(env_path_prepend=[...]) stores the dirs on the instance for _build_codex_env."""
        from types import SimpleNamespace

        import openai_codex

        monkeypatch.delenv("CODEX_API_KEY", raising=False)
        monkeypatch.setattr(openai_codex, "Codex", lambda **_kw: SimpleNamespace(close=lambda: None))

        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
        await agent.start(str(tmp_path), env_path_prepend=["/sandbox/mocks", "/sandbox/bins"])

        assert agent._env_path_prepend == ["/sandbox/mocks", "/sandbox/bins"]


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

    def test_base_url_without_api_version_omits_query_params(self, monkeypatch):
        """Plain OpenAI / gateway endpoints get no api-version query param."""
        monkeypatch.setenv("CODEX_BASE_URL", "https://gw.local/openai/v1")
        monkeypatch.delenv("CODEX_API_VERSION", raising=False)
        monkeypatch.delenv("CODEX_WIRE_API", raising=False)
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="gpt-5-codex"))
        provider = agent._build_thread_options()["config"]["model_providers"]["custom"]
        assert "query_params" not in provider
        assert provider["wire_api"] == "responses"

    def test_azure_provider_injects_api_version(self, monkeypatch):
        """Azure: CODEX_API_VERSION adds the required api-version query param; the
        deployment name rides in as the model."""
        monkeypatch.setenv("CODEX_BASE_URL", "https://my-res.openai.azure.com/openai")
        monkeypatch.setenv("CODEX_API_VERSION", "2025-04-01-preview")
        monkeypatch.delenv("CODEX_WIRE_API", raising=False)
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="my-gpt5-deployment"))
        opts = agent._build_thread_options()
        assert opts["model_provider"] == "custom"
        assert opts["model"] == "my-gpt5-deployment"
        provider = opts["config"]["model_providers"]["custom"]
        assert provider["base_url"] == "https://my-res.openai.azure.com/openai"
        assert provider["env_key"] == "CODEX_API_KEY"
        assert provider["wire_api"] == "responses"
        assert provider["query_params"] == {"api-version": "2025-04-01-preview"}

    def test_wire_api_fixed_to_responses(self, monkeypatch):
        """wire_api is always 'responses' — the pinned codex binary dropped 'chat'
        support, so it's a fixed constant, not an operator knob."""
        monkeypatch.setenv("CODEX_BASE_URL", "https://my-res.openai.azure.com/openai")
        monkeypatch.setenv("CODEX_WIRE_API", "chat")  # ignored — no longer a knob
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="dep"))
        provider = agent._build_thread_options()["config"]["model_providers"]["custom"]
        assert provider["wire_api"] == "responses"

    def test_empty_api_version_falls_back(self, monkeypatch):
        """Empty (not just unset) CODEX_API_VERSION is treated as unset: no
        api-version query param."""
        monkeypatch.setenv("CODEX_BASE_URL", "https://my-res.openai.azure.com/openai")
        monkeypatch.setenv("CODEX_API_VERSION", "")
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="dep"))
        provider = agent._build_thread_options()["config"]["model_providers"]["custom"]
        assert "query_params" not in provider
        assert provider["wire_api"] == "responses"


class TestCodexEnvironmentInfo:
    """get_environment_info surfaces resolved custom-endpoint routing for run artifacts."""

    def test_no_base_url_emits_nothing(self, monkeypatch):
        monkeypatch.delenv("CODEX_BASE_URL", raising=False)
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="gpt-5-codex"))
        assert agent.get_environment_info() == {}

    def test_azure_routing_recorded(self, monkeypatch):
        """Host (not full URL), wire_api, api-version, and the deployment-name marker
        are recorded; the API key is never included."""
        monkeypatch.setenv("CODEX_BASE_URL", "https://key@my-res.openai.azure.com/openai")
        monkeypatch.setenv("CODEX_API_VERSION", "2025-04-01-preview")
        monkeypatch.setenv("CODEX_API_KEY", "super-secret")
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="my-gpt5-deployment"))
        info = agent.get_environment_info()
        assert info == {
            "codex_base_url_host": "my-res.openai.azure.com",
            "codex_wire_api": "responses",
            "codex_api_version": "2025-04-01-preview",
            "codex_model_is_deployment": True,
        }
        assert "super-secret" not in str(info)


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
        assert options["sandbox"] == Sandbox.full_access
        assert options["approval_mode"] == ApprovalMode.deny_all

    def test_build_thread_options_with_plan(self):
        """_build_thread_options builds correct options for plan (sandbox still full-access)."""
        from openai_codex.api import ApprovalMode, Sandbox  # pyright: ignore[reportPrivateImportUsage]

        config = parse_agent_config(
            type=AgentKind.CODEX,
            permission_mode="plan",
        )
        agent = CodexAgent(config)

        options = agent._build_thread_options()

        assert options is not None
        assert options["sandbox"] == Sandbox.full_access
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
        """_build_thread_options is full-access/deny_all even without a permission_mode."""
        from openai_codex.api import ApprovalMode, Sandbox  # pyright: ignore[reportPrivateImportUsage]

        config = parse_agent_config(type=AgentKind.CODEX)
        agent = CodexAgent(config)

        options = agent._build_thread_options()

        # Should have defaults even without explicit permission_mode
        assert options["sandbox"] == Sandbox.full_access
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
        assert options["sandbox"] == Sandbox.full_access
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

import os  # noqa: E402
import shlex  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
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
        # 8 cached, so the fresh slice (100 - 8 = 92) is plain uncached input;
        # Codex has no cache-write (cache_creation == 0); 8 is cache_read. The
        # derived input_tokens total = 92 + 8 = 100 (see _token_usage_from_sdk).
        assert record.token_usage.uncached_input_tokens == 92
        assert record.token_usage.cache_creation_input_tokens == 0
        assert record.token_usage.cache_read_input_tokens == 8
        assert record.token_usage.input_tokens == 100
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
    """CodexAgent buckets the fresh prompt slice (input - cached) as uncached input.

    OpenAI reports ``input_tokens`` inclusive of the cached prefix and bills no
    separate cache-write fee, so the fresh slice is plain ``uncached_input_tokens``
    with ``cache_creation_input_tokens == 0``. ``input_tokens`` (the derived total)
    equals the full prompt = uncached + cache_read.
    """

    def _agent(self, model: str = "gpt-5.5"):
        return CodexAgent(parse_agent_config(type=AgentKind.CODEX, model=model))

    def test_first_call_all_fresh_is_uncached_input(self):
        # cached=0 (cold cache): the whole prompt is freshly processed, no cache.
        usage = self._agent()._token_usage_from_sdk(_sdk_usage(1000, 40, 0))
        assert usage is not None
        assert usage.uncached_input_tokens == 1000
        assert usage.cache_creation_input_tokens == 0
        assert usage.cache_read_input_tokens == 0
        assert usage.input_tokens == 1000  # derived total

    def test_fully_cached_prompt_has_no_uncached_input(self):
        # cached == input: nothing fresh, all cache-read.
        usage = self._agent()._token_usage_from_sdk(_sdk_usage(1000, 40, 1000))
        assert usage is not None
        assert usage.uncached_input_tokens == 0
        assert usage.cache_creation_input_tokens == 0
        assert usage.cache_read_input_tokens == 1000
        assert usage.input_tokens == 1000  # derived total = cache_read

    def test_partial_cache_fresh_slice_is_uncached_input(self):
        usage = self._agent()._token_usage_from_sdk(_sdk_usage(1000, 40, 800))
        assert usage is not None
        assert usage.uncached_input_tokens == 200  # 1000 - 800
        assert usage.cache_creation_input_tokens == 0
        assert usage.cache_read_input_tokens == 800
        assert usage.input_tokens == 1000  # derived total

    def test_cached_exceeding_input_clamps_to_zero(self):
        # Defensive: a malformed delta where cached > input must not go negative.
        usage = self._agent()._token_usage_from_sdk(_sdk_usage(100, 5, 150))
        assert usage is not None
        assert usage.uncached_input_tokens == 0
        assert usage.cache_read_input_tokens == 150

    def test_cost_prices_uncached_input_at_input_rate(self):
        # gpt-5.5: input $5/MTok, cached $0.50/MTok. Fresh 1M as uncached input -> $5.
        usage = self._agent("gpt-5.5")._token_usage_from_sdk(_sdk_usage(1_000_000, 0, 0))
        assert usage is not None
        assert usage.total_cost_usd == pytest.approx(5.0)

    def test_empty_usage_returns_none(self):
        assert self._agent()._token_usage_from_sdk(None) is None
        assert self._agent()._token_usage_from_sdk(SimpleNamespace(total=None)) is None

    async def test_per_message_uncached_input_on_first_submessage_only(self):
        # One generation (one tokenUsage) with two tool items + text. The fresh
        # slice is recorded once, on the first sub-message; follow-ups carry 0 so
        # per-message_id sums don't double-count. cache_creation is 0 throughout.
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
        # Codex has no cache-write: the fresh slice lives in input_tokens, cache_creation is 0.
        assert all(m.cache_creation_tokens == 0 for m in assistant_msgs)
        # Per-generation fresh (100 - 8 = 92) recorded exactly once across the gen.
        assert sum(m.input_tokens for m in assistant_msgs) == 92
        assert sum(1 for m in assistant_msgs if m.input_tokens > 0) == 1
        assert sum(m.cache_read_tokens for m in assistant_msgs) == 8
        # Turn-level aggregate matches the per-message sum (single cold gen).
        assert record.token_usage is not None
        assert record.token_usage.uncached_input_tokens == 92
        assert record.token_usage.cache_creation_input_tokens == 0

    async def test_fresh_slice_is_uncached_input_across_generations(self):
        # Two generations. The fresh (uncached) slice each call = input - cached,
        # billed as plain uncached input with NO cache-write bucket (Codex has no
        # separate cache-write fee). Gen 2: fresh = 1100 - 100 = 1000.
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
        # Gen 1 (cold): all 1000 fresh is uncached input, no cache.
        g1 = next(m for m in msgs if m.message_id and m.message_id.endswith("-msg-0"))
        assert (g1.cache_creation_tokens, g1.input_tokens) == (0, 1000)
        # Gen 2: fresh 1000 is uncached input, 100 cache-read; no cache-write.
        g2 = next(m for m in msgs if m.message_id and m.message_id.endswith("-msg-1"))
        assert (g2.cache_creation_tokens, g2.input_tokens, g2.cache_read_tokens) == (0, 1000, 100)
        # Turn total: uncached 2000, no cache-write, cache-read 100; derived input == 2100.
        tu = record.token_usage
        assert tu is not None
        assert (tu.cache_creation_input_tokens, tu.uncached_input_tokens, tu.cache_read_input_tokens) == (0, 2000, 100)
        assert tu.input_tokens == 2100
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
        # Fresh slice 100 - 8 = 92 -> uncached_input; cached 8 -> cache_read; out 40; no cache-write.
        assert tu.uncached_input_tokens == 92
        assert tu.cache_creation_input_tokens == 0
        assert tu.cache_read_input_tokens == 8
        assert tu.input_tokens == 100  # derived total
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
        # Fresh slices (input + any cache_creation) fold into uncached; Codex has no cache-write.
        assert usage.uncached_input_tokens == 102
        assert usage.cache_creation_input_tokens == 0
        assert usage.cache_read_input_tokens == 12
        assert usage.output_tokens == 46
        assert usage.input_tokens == 114  # derived total = 102 + 12

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
        # Uncached input recorded once (first sub-message only): 100 - 8 = 92; no cache-write.
        assert sum(m.input_tokens for m in assistant) == 92
        assert sum(1 for m in assistant if m.input_tokens > 0) == 1
        assert all(m.cache_creation_tokens == 0 for m in assistant)
        assert sum(m.cache_read_tokens for m in assistant) == 8

        assert record.token_usage is not None
        assert record.token_usage.output_tokens == 50
        assert record.token_usage.uncached_input_tokens == 92
        assert record.token_usage.cache_creation_input_tokens == 0
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
    Claude's Task tool. The agent must surface these as tool calls AND nest the
    sub-agent's generations under the spawn (``parent_tool_use_id``) — the
    wait/messaging follow-ups reuse the same thread and are not new sub-agents."""

    async def test_spawn_nests_subagent_message_and_records_tool_calls(self, monkeypatch, tmp_path):
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

        # The sub-agent's returned result ("5050") nests under the spawn call
        # (parent_tool_use_id == the spawn's tool_use_id, carrying the spawned
        # model) — this is what makes the Agent row expandable in the evalboard
        # and what per-sub-agent attribution is derived from. Only the spawn
        # nests; the wait reuses the same thread and is not a new sub-agent.
        nested = [m for m in record.messages if getattr(m, "parent_tool_use_id", None) == "call_spawn"]
        assert len(nested) == 1
        assert nested[0].content_blocks[0].text == "5050"
        assert nested[0].model == "gpt-5.5"

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

        # No spawn → no nested sub-agent generations.
        assert not any(getattr(m, "parent_tool_use_id", None) for m in record.messages)
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
        # Per-sub-agent aggregate is now derived by summing the nested generations:
        # output 90 + 6 == 96, cache-read 3456 + 11648 == 15104.
        assert sum(m.output_tokens for m in nested) == 96
        assert sum(m.cache_read_tokens for m in nested) == 15104
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
        # parent (uncached 2000, cr 0, out 20) + child (uncached 8755, cr 15104, out 96).
        # Codex has no cache-write, so cache_creation stays 0; fresh slices fold into uncached.
        assert tu.cache_creation_input_tokens == 0
        assert tu.uncached_input_tokens == 2000 + 8755
        assert tu.cache_read_input_tokens == 0 + 15104
        assert tu.output_tokens == 20 + 96
        assert tu.input_tokens == (2000 + 8755) + (0 + 15104)  # derived total


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
        handle = SimpleNamespace(stream=_BlockingStream, interrupt=lambda: None)
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
        agent.working_directory = __import__("pathlib").Path(".")
        agent.codex_client = SimpleNamespace(close=lambda: None)
        agent.thread = SimpleNamespace(turn=lambda _u: handle)

        with pytest.raises(TurnTimeoutError):
            await agent.communicate("do it", timeout=0.2)

        assert agent.pending_turn is not None
        assert agent.pending_turn.crashed is True
        assert agent.get_state() == AgentState.ERROR


class _ImmediateTimeoutWatchdog:
    """A watchdog stub that fires on_timeout synchronously on __enter__ (setting
    state.timeout_hit) but does NOT cancel the task — so the pump runs to
    completion and the turn is handled by the POST-watchdog timeout block (the
    'watchdog fired but the pump finished before the cancel landed' race)."""

    def __init__(self, *, timeout_seconds=None, on_timeout=None, asyncio_task_to_cancel=None, label=""):
        self._on_timeout = on_timeout

    def __enter__(self):
        if self._on_timeout is not None:
            self._on_timeout()
        return self

    def __exit__(self, *exc):
        return False


class TestCommunicatePostWatchdogTimeoutRace:
    """Regression for the post-watchdog timeout race: when the watchdog fires but
    the pump completes before the cancel lands, the trailing `if timeout_hit`
    block must set _state=ERROR (consistent with every other timeout/crash path).
    Previously this path left _state unchanged — a latent inconsistency now fixed
    by routing it through the shared _finalize_and_raise_timeout kernel."""

    async def test_post_watchdog_timeout_sets_error_state_and_partial(self, monkeypatch):
        notifications = [_delta("done"), _turn_completed()]
        agent = _started_agent(parse_agent_config(type=AgentKind.CODEX), notifications)
        # Fire the watchdog synchronously without cancelling, so the pump returns
        # normally and the post-watchdog `if state.timeout_hit:` block fires.
        monkeypatch.setattr("coder_eval.agents.codex_agent.ThreadedWatchdog", _ImmediateTimeoutWatchdog)

        with pytest.raises(TurnTimeoutError):
            await agent.communicate("do it", timeout=30.0)

        # The fix: this race path now ends in ERROR (would be WORKING before).
        assert agent.get_state() == AgentState.ERROR
        assert agent.pending_turn is not None
        assert agent.pending_turn.crashed is True


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


class TestLoginShellMockPathHome:
    """Codex issues every shell command through the default shell as a login
    shell (``bash -lc`` on Linux, ``zsh -lc`` on macOS). The login shell
    re-sources the system profile chain (``/etc/profile``, macOS
    ``/etc/zprofile``'s path_helper), which unconditionally RESETS PATH —
    silently dropping the mock-CLI prepend passed via the app-server env, so
    bare commands resolve to the REAL CLIs (real-tenant contamination). The
    agent therefore generates a per-task HOME whose ``.bash_profile``/
    ``.profile`` (bash/sh) and ``.zshenv``/``.zprofile``/``.zshrc`` (zsh) run
    AFTER that chain and restore the prepend; ``_build_codex_env`` points HOME
    and ZDOTDIR at it and pins CODEX_HOME so codex state stays put."""

    ALL_PROFILE_NAMES = (".bash_profile", ".profile", ".zshenv", ".zprofile", ".zshrc")
    ZSH_PROFILE_NAMES = (".zshenv", ".zprofile", ".zshrc")

    @staticmethod
    def _force_posix(monkeypatch, supported: bool = True):
        monkeypatch.setattr(CodexAgent, "_login_shell_profiles_supported", staticmethod(lambda: supported))

    @staticmethod
    def _agent_with_prepend(prepend):
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
        agent._env_path_prepend = list(prepend)
        return agent

    def test_setup_writes_profiles_with_mock_prepend(self, monkeypatch, tmp_path):
        self._force_posix(monkeypatch)
        monkeypatch.setenv("HOME", str(tmp_path / "orig-home"))
        agent = self._agent_with_prepend(["/sandbox/mocks", "/sandbox/bins"])

        agent._setup_login_shell_home()
        try:
            home = agent._login_shell_home
            assert home is not None and home.is_dir()
            for name in self.ALL_PROFILE_NAMES:
                content = (home / name).read_text(encoding="utf-8")
                # The export must prepend the mock dirs (POSIX ':' joined) ahead
                # of whatever the system profile chain left in PATH.
                assert 'export PATH=/sandbox/mocks:/sandbox/bins:"$PATH"' in content
        finally:
            agent._cleanup_login_shell_home()

    def test_bash_profile_sources_original_home_first_found_chain(self, monkeypatch, tmp_path):
        """The generated .bash_profile mimics bash's first-found chain over the
        ORIGINAL home (so image/user setup isn't lost); the .profile twin (read
        by sh/dash login shells) sources only .profile — the bash-specific
        files may contain bashisms."""
        self._force_posix(monkeypatch)
        orig = tmp_path / "orig-home"
        monkeypatch.setenv("HOME", str(orig))
        agent = self._agent_with_prepend(["/sandbox/mocks"])

        agent._setup_login_shell_home()
        try:
            home = agent._login_shell_home
            assert home is not None
            qorig = shlex.quote(str(orig))
            bash_profile = (home / ".bash_profile").read_text(encoding="utf-8")
            for name in (".bash_profile", ".bash_login", ".profile"):
                assert f". {qorig}/{name}" in bash_profile
            profile = (home / ".profile").read_text(encoding="utf-8")
            assert f". {qorig}/.profile" in profile
            assert f". {qorig}/.bash_profile" not in profile
        finally:
            agent._cleanup_login_shell_home()

    def test_zsh_profiles_source_exact_counterparts_and_pin_zdotdir(self, monkeypatch, tmp_path):
        """Each generated zsh file sources its EXACT counterpart from the
        original home (zsh reads ALL of its startup files — no first-found
        chain), and .zshenv re-pins ZDOTDIR to the generated home AFTER
        sourcing, so a user .zshenv that redefines ZDOTDIR cannot steer the
        rest of the startup chain away from the generated files."""
        self._force_posix(monkeypatch)
        orig = tmp_path / "orig-home"
        monkeypatch.setenv("HOME", str(orig))
        monkeypatch.delenv("ZDOTDIR", raising=False)
        agent = self._agent_with_prepend(["/sandbox/mocks"])

        agent._setup_login_shell_home()
        try:
            home = agent._login_shell_home
            assert home is not None
            qorig = shlex.quote(str(orig))
            for name in self.ZSH_PROFILE_NAMES:
                content = (home / name).read_text(encoding="utf-8")
                assert f". {qorig}/{name}" in content
                # No cross-file sourcing and no bash chain.
                for other in self.ALL_PROFILE_NAMES:
                    if other != name:
                        assert f". {qorig}/{other}" not in content
            zshenv = (home / ".zshenv").read_text(encoding="utf-8")
            pin = zshenv.index(f"export ZDOTDIR={shlex.quote(str(home))}")
            assert pin > zshenv.index(f". {qorig}/.zshenv")  # re-pin AFTER sourcing
            assert pin < zshenv.index("export PATH=")
            for name in (".zprofile", ".zshrc"):
                assert "export ZDOTDIR" not in (home / name).read_text(encoding="utf-8")
        finally:
            agent._cleanup_login_shell_home()

    def test_zsh_profiles_source_from_original_zdotdir_when_set(self, monkeypatch, tmp_path):
        """A user with their own ZDOTDIR keeps their real zsh dotfiles there —
        the generated files must source from IT, not from the home."""
        self._force_posix(monkeypatch)
        orig = tmp_path / "orig-home"
        zdot = tmp_path / "orig-zdot"
        monkeypatch.setenv("HOME", str(orig))
        monkeypatch.setenv("ZDOTDIR", str(zdot))
        agent = self._agent_with_prepend(["/sandbox/mocks"])

        agent._setup_login_shell_home()
        try:
            home = agent._login_shell_home
            assert home is not None
            qzdot = shlex.quote(str(zdot))
            for name in self.ZSH_PROFILE_NAMES:
                content = (home / name).read_text(encoding="utf-8")
                assert f". {qzdot}/{name}" in content
            # The bash files are untouched by ZDOTDIR.
            bash_profile = (home / ".bash_profile").read_text(encoding="utf-8")
            assert str(zdot) not in bash_profile
        finally:
            agent._cleanup_login_shell_home()

    def test_mock_dirs_with_shell_metacharacters_are_quoted(self, monkeypatch, tmp_path):
        self._force_posix(monkeypatch)
        monkeypatch.setenv("HOME", str(tmp_path / "orig-home"))
        weird = "/sandbox/mo cks/$(evil)"
        agent = self._agent_with_prepend([weird])

        agent._setup_login_shell_home()
        try:
            home = agent._login_shell_home
            assert home is not None
            content = (home / ".bash_profile").read_text(encoding="utf-8")
            assert f'export PATH={shlex.quote(weird)}:"$PATH"' in content
        finally:
            agent._cleanup_login_shell_home()

    def test_profile_restores_original_home_before_sourcing(self, monkeypatch, tmp_path):
        """The generated profile's FIRST act is exporting the original HOME
        back — the temp HOME exists only so bash picks this file; everything
        sourced after (and the command body) must see the real $HOME."""
        self._force_posix(monkeypatch)
        orig = tmp_path / "orig-home"
        monkeypatch.setenv("HOME", str(orig))
        monkeypatch.delenv("ZDOTDIR", raising=False)
        agent = self._agent_with_prepend(["/sandbox/mocks"])

        agent._setup_login_shell_home()
        try:
            home = agent._login_shell_home
            assert home is not None
            for name in self.ALL_PROFILE_NAMES:
                content = (home / name).read_text(encoding="utf-8")
                export_home = content.index(f"export HOME={shlex.quote(str(orig))}")
                assert export_home < content.index(". ")  # before any sourcing
                assert export_home < content.index("export PATH=")
        finally:
            agent._cleanup_login_shell_home()

    def test_profile_without_original_home_only_prepends(self, monkeypatch):
        """HOME unset in the harness env: no restore, no sourcing — just the
        mock prepend."""
        content = CodexAgent._login_profile_content(".bash_profile", "", 'export PATH=/m:"$PATH"')
        assert "export HOME" not in content
        assert ". " not in content
        assert 'export PATH=/m:"$PATH"' in content

    def test_generated_profiles_are_lf_only(self, monkeypatch, tmp_path):
        """A profile built on ANY host must stay LF-only — bash chokes on \\r."""
        self._force_posix(monkeypatch)
        monkeypatch.setenv("HOME", str(tmp_path / "orig-home"))
        agent = self._agent_with_prepend(["/sandbox/mocks"])

        agent._setup_login_shell_home()
        try:
            home = agent._login_shell_home
            assert home is not None
            for name in self.ALL_PROFILE_NAMES:
                raw = (home / name).read_bytes()
                assert b"\r" not in raw
        finally:
            agent._cleanup_login_shell_home()

    def test_no_login_home_without_mock_dirs(self, monkeypatch):
        self._force_posix(monkeypatch)
        agent = self._agent_with_prepend([])
        agent._setup_login_shell_home()
        assert agent._login_shell_home is None

    def test_no_login_home_on_unsupported_platform(self, monkeypatch):
        self._force_posix(monkeypatch, supported=False)
        agent = self._agent_with_prepend(["/sandbox/mocks"])
        agent._setup_login_shell_home()
        assert agent._login_shell_home is None

    def test_build_codex_env_sets_home_and_pins_codex_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CODEX_API_KEY", raising=False)
        monkeypatch.setenv("PATH", "/parent/bin")
        agent = self._agent_with_prepend(["/sandbox/mocks"])
        agent._login_shell_home = tmp_path / "login-home"

        env = agent._build_codex_env()
        assert env is not None
        assert env["HOME"] == str(tmp_path / "login-home")
        # zsh (macOS default shell) picks its dotfiles via ZDOTDIR, not HOME.
        assert env["ZDOTDIR"] == str(tmp_path / "login-home")
        # Codex state (auth, rollout sessions) must NOT move with HOME — the
        # harness reads the same _codex_home() for sub-agent rollout recovery.
        assert env["CODEX_HOME"] == str(agent._codex_home())

    def test_build_codex_env_creates_missing_codex_home(self, monkeypatch, tmp_path):
        """The codex binary hard-errors on an explicitly set CODEX_HOME that
        does not exist (unset, it materializes the ~/.codex default itself).
        Runners that auth via CODEX_API_KEY never ran ``codex login``, so the
        dir may not exist — pinning it must create it first."""
        monkeypatch.delenv("CODEX_API_KEY", raising=False)
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "state" / ".codex"))
        agent = self._agent_with_prepend(["/sandbox/mocks"])
        agent._login_shell_home = tmp_path / "login-home"

        env = agent._build_codex_env()
        assert env is not None
        assert Path(env["CODEX_HOME"]).is_dir()

    def test_build_codex_env_without_login_home_leaves_home_alone(self, monkeypatch):
        monkeypatch.setenv("CODEX_API_KEY", "k")
        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
        env = agent._build_codex_env()
        assert env is not None
        assert "HOME" not in env
        assert "ZDOTDIR" not in env
        assert "CODEX_HOME" not in env

    def test_setup_is_rerunnable_and_cleanup_removes_dir(self, monkeypatch, tmp_path):
        self._force_posix(monkeypatch)
        monkeypatch.setenv("HOME", str(tmp_path / "orig-home"))
        agent = self._agent_with_prepend(["/sandbox/mocks"])

        agent._setup_login_shell_home()
        first = agent._login_shell_home
        assert first is not None
        agent._setup_login_shell_home()  # retried start() must not leak the old dir
        second = agent._login_shell_home
        assert second is not None
        assert not first.exists()

        agent._cleanup_login_shell_home()
        assert agent._login_shell_home is None
        assert not second.exists()

    def test_failed_profile_write_rolls_back_temp_home(self, monkeypatch, tmp_path):
        """A write failure must not orphan the mkdtemp dir — it is tracked
        before writing and removed on the way out."""
        self._force_posix(monkeypatch)
        monkeypatch.setenv("HOME", str(tmp_path / "orig-home"))
        agent = self._agent_with_prepend(["/sandbox/mocks"])

        created: list = []
        real_mkdtemp = tempfile.mkdtemp

        def tracking_mkdtemp(*args, **kwargs):
            d = real_mkdtemp(*args, **kwargs)
            created.append(d)
            return d

        monkeypatch.setattr("coder_eval.agents.codex_agent.tempfile.mkdtemp", tracking_mkdtemp)
        monkeypatch.setattr(
            CodexAgent,
            "_login_profile_content",
            staticmethod(lambda *_a, **_kw: (_ for _ in ()).throw(OSError("disk full"))),
        )

        with pytest.raises(OSError, match="disk full"):
            agent._setup_login_shell_home()

        assert agent._login_shell_home is None
        assert created and not Path(created[0]).exists()

    def test_kill_sync_cleans_login_home(self, monkeypatch, tmp_path):
        """The watchdog's terminal kill path must not leak the temp HOME."""
        self._force_posix(monkeypatch)
        monkeypatch.setenv("HOME", str(tmp_path / "orig-home"))
        agent = self._agent_with_prepend(["/sandbox/mocks"])
        agent.codex_client = SimpleNamespace(close=lambda: None)

        agent._setup_login_shell_home()
        home = agent._login_shell_home
        assert home is not None

        agent.kill_sync()

        assert agent._login_shell_home is None
        assert not home.exists()

    async def test_start_creates_and_stop_cleans_login_home(self, monkeypatch, tmp_path):
        """start() must compose the pieces: generated HOME + pinned CODEX_HOME
        + mock-first PATH must all land in the CodexConfig env handed to the
        SDK (not just exist on the agent), and stop() must clean up."""
        import openai_codex

        self._force_posix(monkeypatch)
        monkeypatch.setenv("HOME", str(tmp_path / "orig-home"))
        monkeypatch.delenv("CODEX_API_KEY", raising=False)
        captured: dict = {}

        def fake_codex(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(close=lambda: None)

        monkeypatch.setattr(openai_codex, "Codex", fake_codex)

        agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX))
        await agent.start(str(tmp_path), env_path_prepend=["/sandbox/mocks"])

        home = agent._login_shell_home
        assert home is not None and (home / ".bash_profile").is_file()
        config = captured.get("config")
        assert config is not None and config.env is not None
        assert config.env["HOME"] == str(home)
        assert config.env["ZDOTDIR"] == str(home)
        assert config.env["CODEX_HOME"] == str(agent._codex_home())
        path_key = next(k for k in config.env if k.upper() == "PATH")
        assert config.env[path_key].startswith("/sandbox/mocks")

        await agent.stop()
        assert agent._login_shell_home is None
        assert not home.exists()

    @pytest.mark.skipif(
        os.name != "posix" or not shutil.which("bash"),
        reason="requires a POSIX bash to exercise a real login shell",
    )
    def test_login_shell_restores_mock_prepend_end_to_end(self, monkeypatch, tmp_path):
        """Real ``bash -lc`` with the generated HOME, against a CONTROLLED
        original home (hermetic — the developer/CI dotfiles play no part):

        - the original .bash_profile resets PATH (worst case) and sources
          ``$HOME/.bashrc`` — which must resolve to the ORIGINAL home;
        - the mock dir still comes out FIRST on PATH;
        - the command body sees the original ``$HOME``.
        """
        self._force_posix(monkeypatch)
        orig = tmp_path / "orig-home"
        orig.mkdir()
        (orig / ".bash_profile").write_text(
            'export PATH="/usr/local/bin:/usr/bin:/bin"\n[ -r "$HOME/.bashrc" ] && . "$HOME/.bashrc"\n',
            encoding="utf-8",
            newline="\n",
        )
        (orig / ".bashrc").write_text("export BASHRC_SOURCED=1\n", encoding="utf-8", newline="\n")
        monkeypatch.setenv("HOME", str(orig))
        agent = self._agent_with_prepend([str(tmp_path / "mocks")])
        (tmp_path / "mocks").mkdir()

        agent._setup_login_shell_home()
        try:
            home = agent._login_shell_home
            assert home is not None
            result = subprocess.run(
                ["bash", "-lc", 'echo "$PATH|$HOME|${BASHRC_SOURCED:-0}"'],
                env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
                check=True,
            )
            path_value, home_value, bashrc_sourced = result.stdout.strip().split("|")
            assert path_value.split(":")[0] == str(tmp_path / "mocks")
            assert home_value == str(orig)
            assert bashrc_sourced == "1"
        finally:
            agent._cleanup_login_shell_home()

    @pytest.mark.skipif(
        os.name != "posix" or not shutil.which("zsh"),
        reason="requires zsh to exercise a real zsh login shell (macOS default)",
    )
    def test_zsh_login_shell_restores_mock_prepend_end_to_end(self, monkeypatch, tmp_path):
        """Real zsh with the generated home as ZDOTDIR, against a CONTROLLED
        original home whose .zshenv/.zprofile/.zshrc all RESET PATH (worst
        case). Three invocations mirror codex's real shapes:

        - ``zsh -lc`` — how codex runs each command on macOS;
        - ``zsh -lic`` — the shell-snapshot capture, which also reads .zshrc;
        - nested ``zsh -lc`` inside the command — must KEEP the prepend
          (ZDOTDIR stays exported), unlike the documented bash nested gap.

        In every shape the mock dir must come out FIRST on PATH, the command
        body must see the original ``$HOME``, and the original dotfiles must
        have been sourced.
        """
        self._force_posix(monkeypatch)
        orig = tmp_path / "orig-home"
        orig.mkdir()
        reset = 'export PATH="/usr/local/bin:/usr/bin:/bin"\n'
        (orig / ".zshenv").write_text(reset + "export ZSHENV_SOURCED=1\n", encoding="utf-8", newline="\n")
        (orig / ".zprofile").write_text(reset + "export ZPROFILE_SOURCED=1\n", encoding="utf-8", newline="\n")
        (orig / ".zshrc").write_text(reset + "export ZSHRC_SOURCED=1\n", encoding="utf-8", newline="\n")
        monkeypatch.setenv("HOME", str(orig))
        monkeypatch.delenv("ZDOTDIR", raising=False)
        agent = self._agent_with_prepend([str(tmp_path / "mocks")])
        (tmp_path / "mocks").mkdir()

        agent._setup_login_shell_home()
        try:
            home = agent._login_shell_home
            assert home is not None
            env = {"HOME": str(home), "ZDOTDIR": str(home), "PATH": "/usr/bin:/bin"}
            probe = 'echo "$PATH|$HOME|${ZSHENV_SOURCED:-0}${ZPROFILE_SOURCED:-0}${ZSHRC_SOURCED:-0}"'

            for args, sourced in ((["zsh", "-lc", probe], "110"), (["zsh", "-lic", probe], "111")):
                result = subprocess.run(args, env=env, capture_output=True, text=True, check=True)
                path_value, home_value, markers = result.stdout.strip().split("|")
                assert path_value.split(":")[0] == str(tmp_path / "mocks"), args
                assert home_value == str(orig), args
                assert markers == sourced, args

            nested = subprocess.run(
                ["zsh", "-lc", f"zsh -lc '{probe}'"],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            path_value = nested.stdout.strip().split("|")[0]
            assert path_value.split(":")[0] == str(tmp_path / "mocks")
        finally:
            agent._cleanup_login_shell_home()
