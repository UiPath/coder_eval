"""Tests for :mod:`coder_eval.agents.delegate_sdk_agent`.

Subprocess-level tests use an in-memory fake that mimics the host's JSON-lines
protocol — no real Node process is spawned.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from coder_eval.agents import _delegate_s2s_token_file, delegate_sdk_agent
from coder_eval.agents.delegate_sdk_agent import (
    DelegateSdkAgent,
    _describe_saved_auth,
    _is_auth_init_error,
    _maybe_pin_npm_globalconfig,
    _resolve_bundled_skills_path,
    _resolve_stall_timeout,
    _resolve_stdio_bundle,
    _resolve_stdio_verbose,
    _strip_gateway_creds,
)
from coder_eval.errors import AgentConfigError, AgentCrashError, TurnTimeoutError
from coder_eval.errors.categories import ErrorCategory
from coder_eval.errors.categorization import categorize_error
from coder_eval.models import (
    AgentState,
    AssistantMessage,
    DelegateSdkAgentConfig,
    DirectRoute,
    ReconciliationMessage,
    parse_agent_config,
)
from coder_eval.streaming.collector import EventCollector
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentStartEvent,
    StreamEvent,
    TextChunkEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
)


# ---- helpers ---------------------------------------------------------------


def _make_config(**overrides: Any) -> DelegateSdkAgentConfig:
    """Build a DelegateSdkAgentConfig for delegate-sdk with any field overrides."""
    defaults: dict[str, Any] = {
        "type": "delegate-sdk",
        "permission_mode": "default",  # AgentConfig requires a value
    }
    defaults.update(overrides)
    cfg = parse_agent_config(**defaults)
    assert isinstance(cfg, DelegateSdkAgentConfig)
    return cfg


class _CapturingCallback:
    """Simple StreamCallback that records every event for later assertion."""

    def __init__(self) -> None:
        self.events: list[StreamEvent] = []

    def on_event(self, event: StreamEvent) -> None:
        self.events.append(event)


# ---- _resolve_bundled_skills_path -----------------------------------------


class TestResolveBundledSkillsPath:
    def test_empty_plugins_returns_none(self) -> None:
        assert _resolve_bundled_skills_path(None) is None
        assert _resolve_bundled_skills_path([]) is None

    def test_single_plugin_appends_skills_suffix(self, tmp_path: Path) -> None:
        plugin_root = tmp_path / "my-plugin"
        plugin_root.mkdir()
        result = _resolve_bundled_skills_path([{"type": "local", "path": str(plugin_root)}])
        assert result is not None
        assert Path(result) == (plugin_root / "skills").resolve()

    def test_expands_env_var_in_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        plugin_root = tmp_path / "envvar-plugin"
        plugin_root.mkdir()
        monkeypatch.setenv("DELEGATE_TEST_PLUGIN_DIR", str(plugin_root))
        result = _resolve_bundled_skills_path([{"type": "local", "path": "$DELEGATE_TEST_PLUGIN_DIR"}])
        assert result is not None
        assert Path(result) == (plugin_root / "skills").resolve()

    def test_multiple_plugins_first_wins_with_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        with caplog.at_level(logging.WARNING, logger="coder_eval.agents.delegate_sdk_agent"):
            result = _resolve_bundled_skills_path(
                [
                    {"type": "local", "path": str(first)},
                    {"type": "local", "path": str(second)},
                ]
            )
        assert result is not None
        assert Path(result) == (first / "skills").resolve()
        assert any("only one plugin" in rec.message for rec in caplog.records)

    def test_plugin_without_path_returns_none(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="coder_eval.agents.delegate_sdk_agent"):
            result = _resolve_bundled_skills_path([{"type": "local"}])
        assert result is None
        assert any("missing 'path'" in rec.message for rec in caplog.records)


# ---- host bundle resolution ----------------------------------------------


def _make_bundle(install_root: Path) -> Path:
    """Create a stub host bundle under ``install_root/node_modules/@uipath/...``."""
    bundle = install_root / "node_modules" / "@uipath" / "delegate-stdio" / "dist" / "delegate_stdio.mjs"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_text("// stub", encoding="utf-8")
    return bundle


class TestStdioBundleResolution:
    """Pin the host discovery contract: DELEGATE_STDIO_PATH wins; then an
    explicit DELEGATE_STDIO_NODE_MODULES is probed exactly; otherwise the cwd's
    ancestors (and ``~``) are walked the way Node resolves modules. AgentConfigError
    (RuntimeError subclass) is typed so the categorizer routes it to the
    non-retryable AGENT_CONFIG_ERROR category by isinstance.
    """

    def test_missing_host_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DELEGATE_STDIO_PATH", raising=False)
        monkeypatch.setenv("DELEGATE_STDIO_NODE_MODULES", str(tmp_path))
        with pytest.raises(AgentConfigError, match="not found"):
            _resolve_stdio_bundle()

    def test_explicit_path_not_a_file_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DELEGATE_STDIO_PATH", str(tmp_path / "nope.mjs"))
        with pytest.raises(AgentConfigError, match="does not point to a file"):
            _resolve_stdio_bundle()

    def test_explicit_path_resolves(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bundle = tmp_path / "delegate_stdio.mjs"
        bundle.write_text("// stub", encoding="utf-8")
        monkeypatch.setenv("DELEGATE_STDIO_PATH", str(bundle))
        assert _resolve_stdio_bundle() == bundle.resolve()

    def test_node_modules_resolution(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bundle = _make_bundle(tmp_path)
        monkeypatch.delenv("DELEGATE_STDIO_PATH", raising=False)
        monkeypatch.setenv("DELEGATE_STDIO_NODE_MODULES", str(tmp_path))
        assert _resolve_stdio_bundle() == bundle.resolve()

    def test_walks_up_ancestors_to_find_bundle(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """No env config: an install in an *ancestor* of the cwd is auto-located.

        This is the exact onboarding failure the walk-up fixes — `npm install` in
        a directory with no package.json lands the bundle in an ancestor, and the
        old cwd-only probe missed it.
        """
        bundle = _make_bundle(tmp_path)  # installed at the ancestor root
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        home = tmp_path / "home"  # a home WITHOUT the bundle, so the ancestor walk must find it
        home.mkdir()
        monkeypatch.delenv("DELEGATE_STDIO_PATH", raising=False)
        monkeypatch.delenv("DELEGATE_STDIO_NODE_MODULES", raising=False)
        monkeypatch.chdir(deep)
        monkeypatch.setattr(Path, "home", lambda: home)
        assert _resolve_stdio_bundle() == bundle.resolve()

    def test_walks_up_to_home_when_not_an_ancestor(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Home is probed even when it is not an ancestor of the cwd (the Windows case)."""
        home = tmp_path / "home"
        home.mkdir()
        bundle = _make_bundle(home)  # installed in ~ (npm's fallback target)
        workdir = tmp_path / "work" / "deep"  # cwd in a sibling subtree, home is NOT above it
        workdir.mkdir(parents=True)
        monkeypatch.delenv("DELEGATE_STDIO_PATH", raising=False)
        monkeypatch.delenv("DELEGATE_STDIO_NODE_MODULES", raising=False)
        monkeypatch.chdir(workdir)
        monkeypatch.setattr(Path, "home", lambda: home)
        assert _resolve_stdio_bundle() == bundle.resolve()

    def test_no_config_not_found_error_lists_searched_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The diagnostic error names the locations searched (cwd, ancestors, home)."""
        workdir = tmp_path / "work"
        workdir.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.delenv("DELEGATE_STDIO_PATH", raising=False)
        monkeypatch.delenv("DELEGATE_STDIO_NODE_MODULES", raising=False)
        monkeypatch.chdir(workdir)
        monkeypatch.setattr(Path, "home", lambda: home)
        with pytest.raises(AgentConfigError, match="Searched the cwd, its ancestors, and home"):
            _resolve_stdio_bundle()


# ---- _maybe_pin_npm_globalconfig ---------------------------------------------


class TestMaybePinNpmGlobalconfig:
    """The NPM_CONFIG_GLOBALCONFIG pin keeps the global npmrc (registry + token)
    reachable from Delegate-runtime shells despite the interop's per-command
    npm_config_prefix override. It must be strictly additive: operator value wins,
    and no npmrc on disk means no env change."""

    def test_pins_posix_layout(self, tmp_path: Path) -> None:
        """<prefix>/lib/node_modules/@uipath ⇒ <prefix>/etc/npmrc (nodesource/deb layout)."""
        tools_dir = tmp_path / "usr" / "lib" / "node_modules" / "@uipath"
        tools_dir.mkdir(parents=True)
        npmrc = tmp_path / "usr" / "etc" / "npmrc"
        npmrc.parent.mkdir(parents=True)
        npmrc.write_text("@uipath:registry=https://npm.pkg.github.com/\n", encoding="utf-8")

        env: dict[str, str] = {}
        assert _maybe_pin_npm_globalconfig(env, str(tools_dir)) == npmrc
        assert env["NPM_CONFIG_GLOBALCONFIG"] == str(npmrc)

    def test_pins_windows_layout(self, tmp_path: Path) -> None:
        """<prefix>/node_modules/@uipath ⇒ <prefix>/etc/npmrc (Windows global layout)."""
        tools_dir = tmp_path / "nodejs" / "node_modules" / "@uipath"
        tools_dir.mkdir(parents=True)
        npmrc = tmp_path / "nodejs" / "etc" / "npmrc"
        npmrc.parent.mkdir(parents=True)
        npmrc.write_text("@uipath:registry=https://npm.pkg.github.com/\n", encoding="utf-8")

        env: dict[str, str] = {}
        assert _maybe_pin_npm_globalconfig(env, str(tools_dir)) == npmrc
        assert env["NPM_CONFIG_GLOBALCONFIG"] == str(npmrc)

    def test_operator_value_wins(self, tmp_path: Path) -> None:
        """An already-set NPM_CONFIG_GLOBALCONFIG is never overwritten (additive-only)."""
        tools_dir = tmp_path / "usr" / "lib" / "node_modules" / "@uipath"
        tools_dir.mkdir(parents=True)
        npmrc = tmp_path / "usr" / "etc" / "npmrc"
        npmrc.parent.mkdir(parents=True)
        npmrc.write_text("", encoding="utf-8")

        env = {"NPM_CONFIG_GLOBALCONFIG": "/operator/npmrc"}
        assert _maybe_pin_npm_globalconfig(env, str(tools_dir)) is None
        assert env["NPM_CONFIG_GLOBALCONFIG"] == "/operator/npmrc"

    def test_no_npmrc_leaves_env_unchanged(self, tmp_path: Path) -> None:
        """No global npmrc on disk ⇒ nothing to pin ⇒ env untouched."""
        tools_dir = tmp_path / "usr" / "lib" / "node_modules" / "@uipath"
        tools_dir.mkdir(parents=True)

        env: dict[str, str] = {}
        assert _maybe_pin_npm_globalconfig(env, str(tools_dir)) is None
        assert "NPM_CONFIG_GLOBALCONFIG" not in env

    def test_no_plugin_tools_dir_is_noop(self) -> None:
        """Without a discovered @uipath tools dir there is no anchor — env untouched."""
        env: dict[str, str] = {}
        assert _maybe_pin_npm_globalconfig(env, None) is None
        assert env == {}


# ---- _strip_gateway_creds ----------------------------------------------------


class TestStripGatewayCreds:
    """The eval's LLMGW_* gateway S2S credentials are for the judge/proxy. The
    Delegate host authenticates with its own UiPath user token, and everything in
    its env reaches the shells its interop spawns for the agent's Bash/PowerShell
    tool calls — i.e. the code under test. run.py already withholds LLMGW_* from
    docker; this covers tempdir tasks, which inherit the full eval env."""

    def test_removes_llmgw_triple_and_keeps_the_rest(self) -> None:
        env = {
            "LLMGW_CLIENT_ID": "id",
            "LLMGW_CLIENT_SECRET": "secret",
            "LLMGW_URL": "https://gw",
            "AUTH_TOKEN": "tok",
            "DELEGATE_AUTH_TOKEN_FILE": "/live/.auth",
        }
        assert _strip_gateway_creds(env) == ("LLMGW_CLIENT_ID", "LLMGW_CLIENT_SECRET", "LLMGW_URL")
        assert not any(k.startswith("LLMGW_") for k in env)
        assert env == {"AUTH_TOKEN": "tok", "DELEGATE_AUTH_TOKEN_FILE": "/live/.auth"}

    def test_partial_set_still_stripped(self) -> None:
        """Strip whatever is present — the secret is the sensitive part, and it
        does not become harmless just because its siblings are absent."""
        env = {"LLMGW_CLIENT_SECRET": "secret", "AUTH_TOKEN": "tok"}
        assert _strip_gateway_creds(env) == ("LLMGW_CLIENT_SECRET",)
        assert "LLMGW_CLIENT_SECRET" not in env

    def test_noop_without_llmgw_vars(self) -> None:
        env = {"AUTH_TOKEN": "tok"}
        assert _strip_gateway_creds(env) == ()
        assert env == {"AUTH_TOKEN": "tok"}


# ---- S2S token-file refresher wiring ----------------------------------------


class TestTokenRefresherWiring:
    """start() must publish the refresher's token file to the host env, and
    teardown must stop the refresher and remove the file."""

    @staticmethod
    def _fake_jwt(claims: dict[str, Any]) -> str:
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return f"eyJhbGciOiJub25lIn0.{payload}.sig"

    @classmethod
    def _arm_env(cls, monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
        """Put the adapter's env in the state that activates the refresher."""
        env = {
            "LLMGW_CLIENT_ID": "eval-client",
            "LLMGW_CLIENT_SECRET": "s3cret",
            "LLMGW_URL": "https://alpha.uipath.com",
            "AUTH_TOKEN": cls._fake_jwt({"client_id": "eval-client", "exp": int(time.time()) + 3600}),
        }
        env.update(overrides)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        for name in ("DELEGATE_AUTH_TOKEN_FILE", "AUTH_TOKEN_FILE"):
            if name not in overrides:
                monkeypatch.delenv(name, raising=False)

    @staticmethod
    def _make_started_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DelegateSdkAgent:
        monkeypatch.setattr(delegate_sdk_agent, "_resolve_stdio_bundle", lambda: tmp_path / "delegate_stdio.mjs")

        async def _fake_spawn_and_init(self: DelegateSdkAgent) -> None:
            return None

        monkeypatch.setattr(DelegateSdkAgent, "_spawn_and_init", _fake_spawn_and_init)
        return DelegateSdkAgent(_make_config(), DirectRoute())

    @pytest.mark.asyncio
    async def test_start_publishes_token_file_and_stop_removes_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._arm_env(monkeypatch)
        minted = self._fake_jwt({"client_id": "eval-client", "exp": int(time.time()) + 7200})
        monkeypatch.setattr(_delegate_s2s_token_file, "_mint_s2s_token", lambda creds: minted)
        agent = self._make_started_agent(tmp_path, monkeypatch)

        await agent.start(str(tmp_path))

        try:
            host_env = agent._host_env
            assert host_env is not None
            token_file = host_env["DELEGATE_AUTH_TOKEN_FILE"]
            assert host_env["AUTH_TOKEN_FILE"] == token_file
            assert "LLMGW_CLIENT_SECRET" not in host_env
            assert await asyncio.to_thread(Path(token_file).read_text, encoding="utf-8") == minted
        finally:
            await agent.stop()

        assert agent._token_refresher is None
        assert not Path(token_file).exists()

    @pytest.mark.asyncio
    async def test_respawn_keeps_the_refresher_and_its_token_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The replacement host re-reads the same file, so respawn must not tear it down."""
        self._arm_env(monkeypatch)
        monkeypatch.setattr(_delegate_s2s_token_file, "_mint_s2s_token", lambda creds: "tok")
        agent = self._make_started_agent(tmp_path, monkeypatch)
        await agent.start(str(tmp_path))

        try:
            host_env = agent._host_env
            assert host_env is not None
            token_file = host_env["DELEGATE_AUTH_TOKEN_FILE"]

            await agent._respawn_host()

            assert agent._token_refresher is not None
            assert Path(token_file).exists()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_start_survives_a_refresher_that_cannot_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Freshness is an enhancement — a broken refresher must not fail Agent start."""
        self._arm_env(monkeypatch)

        async def _boom(self: _delegate_s2s_token_file.S2sTokenFileRefresher) -> str:
            raise OSError("no space left on device")

        monkeypatch.setattr(_delegate_s2s_token_file.S2sTokenFileRefresher, "start", _boom)
        agent = self._make_started_agent(tmp_path, monkeypatch)

        await agent.start(str(tmp_path))

        try:
            assert agent._token_refresher is None
            host_env = agent._host_env
            assert host_env is not None
            assert "DELEGATE_AUTH_TOKEN_FILE" not in host_env
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_start_leaves_env_alone_when_external_token_file_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._arm_env(monkeypatch, DELEGATE_AUTH_TOKEN_FILE="/external/refresher/token")
        # Belt and braces: if the gate above ever regresses, this keeps the test
        # from making a real 30s POST to the IdP before the assertion fails.
        monkeypatch.setattr(_delegate_s2s_token_file, "_mint_s2s_token", lambda creds: "tok")
        agent = self._make_started_agent(tmp_path, monkeypatch)

        await agent.start(str(tmp_path))

        try:
            assert agent._token_refresher is None
            host_env = agent._host_env
            assert host_env is not None
            assert host_env["DELEGATE_AUTH_TOKEN_FILE"] == "/external/refresher/token"
        finally:
            await agent.stop()


# ---- _warn_unsupported_fields ----------------------------------------------


class TestUnsupportedFieldsWarning:
    def test_warns_for_each_unsupported_field_set(self, caplog: pytest.LogCaptureFixture) -> None:
        config = _make_config(
            allowed_tools=["Read"],
            system_prompt="Act as a pirate.",
        )
        agent = DelegateSdkAgent(config)
        with caplog.at_level(logging.WARNING, logger="coder_eval.agents.delegate_sdk_agent"):
            agent._warn_unsupported_fields()
        messages = " ".join(rec.message for rec in caplog.records)
        assert "allowed_tools" in messages
        assert "system_prompt" in messages

    def test_no_warning_when_all_defaults(self, caplog: pytest.LogCaptureFixture) -> None:
        """Default config — including ``permission_mode='default'`` — must not warn.

        ``permission_mode`` is intentionally NOT in ``_UNSUPPORTED_FIELDS`` since
        the Delegate SDK has no permission concept and the default is truthy;
        listing it would log a WARNING on every run.
        """
        config = _make_config()  # permission_mode="default"
        agent = DelegateSdkAgent(config)
        with caplog.at_level(logging.WARNING, logger="coder_eval.agents.delegate_sdk_agent"):
            agent._warn_unsupported_fields()
        assert caplog.records == []

    def test_non_default_permission_mode_does_not_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        """Even non-default permission_mode is silently ignored — no SDK equivalent."""
        config = _make_config(permission_mode="bypassPermissions")
        agent = DelegateSdkAgent(config)
        with caplog.at_level(logging.WARNING, logger="coder_eval.agents.delegate_sdk_agent"):
            agent._warn_unsupported_fields()
        assert "permission_mode" not in " ".join(rec.message for rec in caplog.records)


# ---- _build_init_options ---------------------------------------------------


class TestBuildInitOptions:
    def test_model_passes_through(self) -> None:
        agent = DelegateSdkAgent(_make_config(model="claude_sonnet_4_5"))
        opts = agent._build_init_options()
        assert opts["model"] == "claude_sonnet_4_5"
        assert opts["enableSkills"] is False
        # max_turns is no longer an init option (moved to per-communicate() call).
        assert "maxSteps" not in opts

    def test_plugins_map_to_bundled_skills_path(self, tmp_path: Path) -> None:
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        agent = DelegateSdkAgent(
            _make_config(plugins=[{"type": "local", "path": str(plugin_root)}]),
        )
        opts = agent._build_init_options()
        assert opts["enableSkills"] is True
        assert Path(opts["bundledSkillsPath"]) == (plugin_root / "skills").resolve()

    def test_env_endpoints_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BACKEND_URL", "http://backend.example")
        monkeypatch.setenv("INTEROP_URL", "http://interop.example")
        agent = DelegateSdkAgent(_make_config())
        opts = agent._build_init_options()
        assert opts["backendUrl"] == "http://backend.example"
        assert opts["interopUrl"] == "http://interop.example"

    def test_no_model_field_when_unset(self) -> None:
        agent = DelegateSdkAgent(_make_config())
        opts = agent._build_init_options()
        assert "model" not in opts
        assert "maxSteps" not in opts

    def test_env_slug_defaults_to_alpha(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DELEGATE_SDK_ENV", raising=False)
        agent = DelegateSdkAgent(_make_config())
        opts = agent._build_init_options()
        assert opts["env"] == "alpha"

    @pytest.mark.parametrize("value", ["staging", "production"])
    def test_env_slug_from_environment(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("DELEGATE_SDK_ENV", value)
        agent = DelegateSdkAgent(_make_config())
        opts = agent._build_init_options()
        assert opts["env"] == value

    def test_env_slug_falls_back_to_alpha_when_blank(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DELEGATE_SDK_ENV", "")
        agent = DelegateSdkAgent(_make_config())
        opts = agent._build_init_options()
        assert opts["env"] == "alpha"

    def test_shell_path_prepend_omitted_when_sandbox_has_no_mock_dirs(self) -> None:
        # Nothing to inject: the option must be absent rather than an empty list,
        # so hosts that predate it (and the SDK's option merge) see a clean config.
        agent = DelegateSdkAgent(_make_config())

        opts = agent._build_init_options()

        assert "shellPathPrepend" not in opts

    def test_shell_path_prepend_forwards_env_path_prepend_in_order(self) -> None:
        """The sandbox's mock_path_dirs (Agent ABC ``env_path_prepend``) reach the
        agent's shell commands as the host's ``shellPathPrepend`` init option.
        A prepend on this process could not work — shell tools execute inside the
        interop service, whose PATH was fixed at spawn — so the SDK injects the
        composed PATH per command instead. Order is PATH precedence."""
        agent = DelegateSdkAgent(_make_config())

        opts = agent._build_init_options(env_path_prepend=["/sandbox/mocks", "/sandbox/bin"])

        assert opts["shellPathPrepend"] == ["/sandbox/mocks", "/sandbox/bin"]

    @pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
    def test_effort_forwarded_from_sdk_options(self, effort: str) -> None:
        # `sdk_options.effort` is the cross-agent surface: the host maps it
        # onto useAppStore.effort, which the ChatFramework already serialises
        # as user_config.effort on every chat request.
        agent = DelegateSdkAgent(_make_config(sdk_options={"effort": effort}))
        opts = agent._build_init_options()
        assert opts["effort"] == effort

    def test_effort_omitted_when_sdk_options_empty(self) -> None:
        agent = DelegateSdkAgent(_make_config())
        opts = agent._build_init_options()
        assert "effort" not in opts

    def test_other_sdk_options_keys_silently_dropped(self) -> None:
        # Non-effort sdk_options keys are Claude Code SDK-specific and have no
        # delegate equivalent. They must not appear in the host init options
        # — the host would reject unknown fields — and the agent should not
        # warn either, since shared experiment YAMLs may legitimately carry
        # Claude-only keys that we silently ignore on the delegate path.
        agent = DelegateSdkAgent(
            _make_config(sdk_options={"effort": "high", "max_thinking_tokens": 8000}),
        )
        opts = agent._build_init_options()
        assert opts["effort"] == "high"
        assert "max_thinking_tokens" not in opts


# ---- working directory forwarding ------------------------------------------


class TestWorkingDirectoryOption:
    def test_init_options_carry_working_directory(self, tmp_path: Path) -> None:
        # The sandbox path rides init options (host chdir + SDK shell-cwd
        # seeding) instead of a prompt prefix — the orchestrator already
        # injects "Your working directory is: ..." into every prompt, so a
        # prefix here would duplicate it.
        agent = DelegateSdkAgent(_make_config())
        agent.working_directory = tmp_path
        opts = agent._build_init_options()
        assert opts["workingDirectory"] == str(tmp_path)

    def test_init_options_omit_working_directory_before_start(self) -> None:
        agent = DelegateSdkAgent(_make_config())
        opts = agent._build_init_options()
        assert "workingDirectory" not in opts

    def test_project_and_session_id_forwarded_when_set(self) -> None:
        agent = DelegateSdkAgent(_make_config(project_id="proj-1", session_id="sess-1"))
        opts = agent._build_init_options()
        assert opts["projectId"] == "proj-1"
        assert opts["sessionId"] == "sess-1"

    def test_project_and_session_id_omitted_when_empty(self) -> None:
        # Empty (the default) must be dropped so the SDK keeps its per-session
        # wiki dir and generates a fresh session — the whole point of the guard.
        agent = DelegateSdkAgent(_make_config())
        opts = agent._build_init_options()
        assert "projectId" not in opts
        assert "sessionId" not in opts


# ---- communicate() via fake subprocess -------------------------------------


class _FakeStreamWriter:
    """Async writer that records lines written to it."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.lines.append(data.decode("utf-8"))

    async def drain(self) -> None:  # pragma: no cover - trivial
        return None

    def close(self) -> None:  # pragma: no cover - trivial
        self.closed = True


class _FakeStreamReader:
    """Async reader that yields pre-queued byte lines."""

    def __init__(self, lines: list[bytes]) -> None:
        self._queue: list[bytes] = list(lines)

    async def readline(self) -> bytes:
        if not self._queue:
            return b""  # EOF
        return self._queue.pop(0)


class _NeverStreamReader:
    """Reader whose readline never returns — simulates a turn that never sends
    a result, so the wall-clock deadline fires. Blocks until cancelled."""

    def __init__(self) -> None:
        self._never = asyncio.Event()

    async def readline(self) -> bytes:
        await self._never.wait()  # pragma: no cover - cancelled at teardown
        return b""


class _LimitOverrunReader:
    """Reader that raises ``LimitOverrunError`` once on the first ``readline``,
    then signals EOF — simulates a single host line exceeding the StreamReader
    byte limit. Drives the drain tasks' over-limit recovery branch."""

    def __init__(self, consumed: int = 10) -> None:
        self._raised = False
        self._consumed = consumed

    async def readline(self) -> bytes:
        if not self._raised:
            self._raised = True
            raise asyncio.LimitOverrunError("line too long", self._consumed)
        return b""  # EOF on the next read

    async def readexactly(self, n: int) -> bytes:
        return b"x" * n


class _FakeProcess:
    """Minimal duck-type for asyncio.subprocess.Process used by DelegateSdkAgent."""

    def __init__(self, stdout_lines: list[dict[str, Any]]) -> None:
        encoded = [(json.dumps(obj) + "\n").encode("utf-8") for obj in stdout_lines]
        self.stdout = _FakeStreamReader(encoded)
        self.stderr = _FakeStreamReader([])
        self.stdin = _FakeStreamWriter()
        self.returncode: int | None = None

    async def wait(self) -> int:  # pragma: no cover - trivial
        self.returncode = 0
        return 0

    def kill(self) -> None:  # pragma: no cover - trivial
        self.returncode = -9


def _install_fake_process(agent: DelegateSdkAgent, stdout_lines: list[dict[str, Any]], tmp_path: Path) -> _FakeProcess:
    """Replace agent's subprocess with a fake that replays ``stdout_lines``.

    Also starts the stdout/stderr drain tasks — ``_drain_stdout`` is the only
    writer to ``_stdout_queue``, which ``_read_line()`` blocks on. Without it,
    every ``communicate()`` call hangs on ``queue.get()`` forever rather than
    consuming the scripted host output. Must be called from inside a running
    event loop (i.e., from ``@pytest.mark.asyncio`` tests).
    """
    proc = _FakeProcess(stdout_lines)
    agent._process = proc  # type: ignore[assignment]
    agent.working_directory = tmp_path
    agent._stdout_task = asyncio.create_task(agent._drain_stdout(proc.stdout))  # type: ignore[arg-type]
    agent._stderr_task = asyncio.create_task(agent._drain_stderr(proc.stderr))  # type: ignore[arg-type]
    return proc


_WAF_BLOCK_PAGE_HEAD = (
    '<!DOCTYPE html>\n\t<html>\n\t\t<head>\n\t\t\t<meta charset="utf-8" />\n'
    "\t\t\t<title>Continue with UiPath Platform</title>\n"
    "\t\t\t<style>@font-face{font-family:Roboto;src:url(data:font/woff2;base64," + "d09GMgABAAAAAClQ" * 8 + ")}\n"
)
"""Head of UiPath's Cloudflare block page: the ``<title>`` lands in the first
few hundred bytes, then ~48 KB of base64 web-font CSS (abbreviated here) runs
before any visible text."""

_WAF_BLOCK_PAGE_TAIL = (
    '\t\t</head>\n\t\t<body>\n\t\t\t<div class="accessDenied">Access denied</div>\n'
    '\t\t\t<div class="errorMessage">We are sorry. UiPath platform is not available in '
    "your country.</div>\n\t\t</body>\n\t</html>"
)
"""Visible tail of the block page — only reachable when the host's ~50 KB crash
message is long enough to survive the font CSS above it."""


class TestCommunicate:
    @pytest.mark.asyncio
    async def test_event_translation_and_command_telemetry(self, tmp_path: Path) -> None:
        """Events from the host must produce the right StreamEvents + CommandTelemetry."""
        stdout_script = [
            {"type": "event", "event": {"type": "session_start", "sessionId": "sess-1"}},
            {"type": "event", "event": {"type": "thinking", "content": "Let me think..."}},
            {
                "type": "event",
                "event": {
                    "type": "tool_call",
                    "toolName": "ReadFile",
                    "toolId": "tool-abc",
                    "toolArgs": {"path": "README.md"},
                },
            },
            {
                "type": "event",
                "event": {
                    "type": "tool_result",
                    "toolName": "ReadFile",
                    "toolId": "tool-abc",
                    "toolStatus": "completed",
                    "toolResult": "file contents here",
                },
            },
            {"type": "event", "event": {"type": "message", "content": "All done."}},
            {"type": "event", "event": {"type": "done", "sessionId": "sess-1"}},
            {"type": "result", "response": "All done.", "sessionId": "sess-1", "durationMs": 42},
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)

        callback = _CapturingCallback()
        record = await agent.communicate("do the thing", stream_callback=callback)

        # Host input: one "send" command with the prompt passed through
        # verbatim (the working directory rides init options, not the prompt)
        stdin_lines = agent._process.stdin.lines  # type: ignore[union-attr]
        assert len(stdin_lines) == 1
        sent = json.loads(stdin_lines[0])
        assert sent["cmd"] == "send"
        assert sent["prompt"] == "do the thing"
        assert sent["sessionId"] is None  # no prior session

        # Session id recorded for next turn
        assert agent._session_id == "sess-1"

        # Streaming events were translated correctly
        assert any(isinstance(e, TextChunkEvent) and e.text == "Let me think..." for e in callback.events)
        assert any(
            isinstance(e, ToolStartEvent) and e.tool.tool_name == "ReadFile" and e.tool.tool_id == "tool-abc"
            for e in callback.events
        )
        assert any(
            isinstance(e, ToolEndEvent) and e.tool.tool_id == "tool-abc" and e.status == ToolEndStatus.OK
            for e in callback.events
        )

        # TurnRecord fields. Host sent no usage block → token_usage stays
        # None; one untagged message event ⇒ legacy count of 1 (back-compat
        # path for un-rebuilt hosts, asserted explicitly below).
        assert record.agent_output == "All done."
        assert record.token_usage is None
        assert record.assistant_turn_count == 1
        # No assistantStepCount in the result → num_turns falls back to the
        # running count (still non-None so reports don't undercount).
        assert record.num_turns == 1
        assert len(record.commands) == 1
        cmd = record.commands[0]
        assert cmd.tool_name == "ReadFile"
        assert cmd.tool_id == "tool-abc"
        assert cmd.result_status == "success"
        assert cmd.duration_ms is not None
        assert cmd.parameters == {"path": "README.md"}

    @pytest.mark.asyncio
    async def test_emits_exactly_one_agent_end_event_on_success(self, tmp_path: Path) -> None:
        """A successful turn emits exactly one AgentEndEvent (the finalization payload).

        The agent is the sole emitter; the EventCollector reduces the stream into
        the returned TurnRecord, so the AgentEndEvent must carry the turn's
        cumulative usage / command count / duration consistently with the record.
        """
        stdout_script = [
            {
                "type": "event",
                "event": {"type": "tool_call", "toolName": "ReadFile", "toolId": "t1", "toolArgs": {"path": "f"}},
            },
            {"type": "event", "event": {"type": "message", "content": "done"}},
            {
                "type": "result",
                "response": "done",
                "sessionId": "s",
                "durationMs": 5,
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)

        callback = _CapturingCallback()
        record = await agent.communicate("go", stream_callback=callback)

        ends = [e for e in callback.events if isinstance(e, AgentEndEvent)]
        assert len(ends) == 1
        event = ends[0]
        assert event.task_id == "delegate-sdk"
        assert event.iteration == record.iteration
        assert len(record.commands) == 1
        assert event.duration_seconds == record.duration_seconds
        assert not event.usage.is_empty()  # usage block carried non-zero tokens
        assert record.token_usage == event.usage  # collector reads usage back from the event

    @pytest.mark.asyncio
    async def test_state_resets_to_working_after_prior_error(self, tmp_path: Path) -> None:
        """A clean turn returns the agent to WORKING even after a prior attempt errored.

        The orchestrator retries AGENT_CRASH / AGENT_API_ERROR by calling
        communicate() again without re-running start(); discard_pending_turn()
        rolls back the iteration but does not reset _state. So a retried success
        must clear the ERROR the failed attempt left behind, or get_state() would
        keep reporting ERROR. Mirrors CodexAgent's analogous regression test.
        """
        stdout_script = [
            {"type": "result", "response": "recovered", "sessionId": "s", "durationMs": 1},
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)
        agent._state = AgentState.ERROR  # simulate a prior failed attempt

        await agent.communicate("retry")

        assert agent.get_state() == AgentState.WORKING

    @pytest.mark.asyncio
    async def test_assistant_turn_count_uses_is_step_start_flag(self, tmp_path: Path) -> None:
        """Host-tagged ``isStepStart`` differentiates LLM round-trips from streaming chunks.

        Two new AI session-messages with five intervening streaming deltas
        must surface as ``assistant_turn_count == 2`` (one per round-trip),
        not 7 (one per emitted event). Mirrors :class:`ClaudeCodeAgent`'s
        "one turn per AssistantMessage" semantics.
        """
        stdout_script = [
            # Round-trip #1: new AI message, then four streaming-delta updates.
            {"type": "event", "event": {"type": "message", "content": "Hel", "isStepStart": True}},
            {"type": "event", "event": {"type": "message", "content": "lo", "isStepStart": False}},
            {"type": "event", "event": {"type": "message", "content": " ", "isStepStart": False}},
            {"type": "event", "event": {"type": "message", "content": "wor", "isStepStart": False}},
            {"type": "event", "event": {"type": "message", "content": "ld", "isStepStart": False}},
            # Round-trip #2 after a tool roundtrip elsewhere: another new AI message.
            {"type": "event", "event": {"type": "message", "content": "Done.", "isStepStart": True}},
            {"type": "result", "response": "Hello world\nDone.", "sessionId": "s", "durationMs": 1},
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)
        record = await agent.communicate("go")
        assert record.assistant_turn_count == 2

    @pytest.mark.asyncio
    async def test_result_assistant_step_count_overrides_running_count(self, tmp_path: Path) -> None:
        """When the ``result`` message carries ``assistantStepCount`` it wins.

        The host tracks step boundaries authoritatively from its session-store
        snapshot; the in-turn counter is only a fallback for crash partials. So
        a result-time count of 3 must overwrite a running count derived from
        events, even if those events were ambiguous (e.g. mixed-flag legacy).
        """
        stdout_script = [
            {"type": "event", "event": {"type": "message", "content": "first", "isStepStart": True}},
            {"type": "event", "event": {"type": "message", "content": "second", "isStepStart": True}},
            {
                "type": "result",
                "response": "ok",
                "sessionId": "s",
                "durationMs": 1,
                "assistantStepCount": 3,
            },
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)
        record = await agent.communicate("go")
        assert record.assistant_turn_count == 3
        # num_turns mirrors the authoritative step count so experiment reports
        # (reports_experiment.py sums t.num_turns) get a real count, not 0.
        assert record.num_turns == 3

    @pytest.mark.asyncio
    async def test_result_usage_populates_token_usage(self, tmp_path: Path) -> None:
        """``usage`` payload on the result must be mapped onto :class:`TokenUsage`."""
        stdout_script = [
            {
                "type": "result",
                "response": "ok",
                "sessionId": "s",
                "durationMs": 1,
                "assistantStepCount": 1,
                "usage": {
                    "input_tokens": 123,
                    "output_tokens": 456,
                    "cache_creation_input_tokens": 7,
                    "cache_read_input_tokens": 8,
                },
                "model": "claude_sonnet_4_5",
            },
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)
        record = await agent.communicate("go")
        assert record.token_usage is not None
        assert record.token_usage.uncached_input_tokens == 123
        assert record.token_usage.input_tokens == 123 + 7 + 8
        assert record.token_usage.output_tokens == 456
        assert record.token_usage.cache_creation_input_tokens == 7
        assert record.token_usage.cache_read_input_tokens == 8
        # The framework prices the normalized claude-sonnet-4-5 at $3.00 input /
        # $15.00 output / $3.75 cache write / $0.30 cache read per MTok. It carries
        # the undated alias alongside the dated -20250929 id as of coder-eval 0.9.5;
        # before that only the dated id was present and this reported None.
        assert record.token_usage.total_cost_usd == pytest.approx(
            (123 * 3.0 + 456 * 15.0 + 7 * 3.75 + 8 * 0.30) / 1_000_000
        )
        assert record.model_used == "claude_sonnet_4_5"

    @pytest.mark.asyncio
    async def test_result_all_zero_usage_treated_as_absent(self, tmp_path: Path) -> None:
        """An all-zero ``usage`` block must not overwrite a possibly-better prior value.

        The framework reports zeros when it hasn't yet ingested the backend's
        ``session_usage`` message for the turn; treating that as authoritative
        would clobber a real value from an earlier turn (relevant once we
        carry forward across the session, but the rule applies per-turn too).
        """
        stdout_script = [
            {
                "type": "result",
                "response": "ok",
                "sessionId": "s",
                "durationMs": 1,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)
        record = await agent.communicate("go")
        assert record.token_usage is None

    @pytest.mark.asyncio
    async def test_max_turns_forwarded_to_host_as_max_steps(self, tmp_path: Path) -> None:
        """``communicate(max_turns=...)`` must reach the host as ``maxSteps`` in the send command.

        The host applies it to the SDK via ``agent.setMaxSteps`` before
        ``sendMessage``. End-to-end enforcement therefore depends on the
        Python adapter actually serialising the value — covered here so a
        future refactor that drops it doesn't silently re-introduce the bug.
        """
        stdout_script = [
            {"type": "result", "response": "ok", "sessionId": "s", "durationMs": 1, "maxStepsReached": False},
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)
        await agent.communicate("go", max_turns=7)
        stdin_lines = agent._process.stdin.lines  # type: ignore[union-attr]
        sent = json.loads(stdin_lines[0])
        assert sent["cmd"] == "send"
        assert sent["maxSteps"] == 7

    @pytest.mark.asyncio
    async def test_max_steps_reached_sets_max_turns_exhausted(self, tmp_path: Path) -> None:
        """``maxStepsReached: true`` on the result lifts ``TurnRecord.max_turns_exhausted``."""
        stdout_script = [
            {
                "type": "result",
                "response": "stopped at the cap",
                "sessionId": "s",
                "durationMs": 1,
                "assistantStepCount": 5,
                "maxStepsReached": True,
            },
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)
        record = await agent.communicate("go", max_turns=5)
        assert record.max_turns_exhausted is True
        assert record.assistant_turn_count == 5

    @pytest.mark.asyncio
    async def test_max_steps_reached_absent_keeps_default(self, tmp_path: Path) -> None:
        """Old host bundles without ``maxStepsReached`` default to ``False`` (back-compat)."""
        stdout_script = [
            {"type": "result", "response": "ok", "sessionId": "s", "durationMs": 1},
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)
        record = await agent.communicate("go", max_turns=5)
        assert record.max_turns_exhausted is False

    @pytest.mark.asyncio
    async def test_max_turns_none_omits_max_steps_from_send(self, tmp_path: Path) -> None:
        """Caller passing ``max_turns=None`` must not put ``maxSteps`` in the send payload.

        The host interprets an omitted ``maxSteps`` as "no cap this turn"
        (it calls ``setMaxSteps(undefined)`` which clamps to 0/unbounded).
        Sending an explicit zero / null would muddy the contract.
        """
        stdout_script = [
            {"type": "result", "response": "ok", "sessionId": "s", "durationMs": 1},
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)
        await agent.communicate("go")  # max_turns defaults to None
        stdin_lines = agent._process.stdin.lines  # type: ignore[union-attr]
        sent = json.loads(stdin_lines[0])
        assert "maxSteps" not in sent

    @pytest.mark.asyncio
    async def test_model_used_falls_back_to_config(self, tmp_path: Path) -> None:
        """When the host doesn't echo ``model``, fall back to AgentConfig.model."""
        stdout_script = [
            {"type": "result", "response": "ok", "sessionId": "s", "durationMs": 1},
        ]
        agent = DelegateSdkAgent(_make_config(model="claude_sonnet_4_5"))
        _install_fake_process(agent, stdout_script, tmp_path)
        record = await agent.communicate("go")
        assert record.model_used == "claude_sonnet_4_5"

    @pytest.mark.asyncio
    async def test_virtuoso_pricing_populates_cost(self, tmp_path: Path) -> None:
        """``virtuoso-1-5`` is in the local pricing table, so cost is computed.

        The Delegate SDK doesn't surface ``total_cost_usd`` on the host
        result; the agent computes it locally from the reported model name
        plus token counts. Pinned to virtuoso-1-5 here because that's the SDK
        default model the alpha backend ships.
        """
        stdout_script = [
            {
                "type": "result",
                "response": "ok",
                "sessionId": "s",
                "durationMs": 1,
                "usage": {
                    "input_tokens": 1_000_000,
                    "output_tokens": 1_000_000,
                    "cache_read_input_tokens": 1_000_000,
                },
                "model": "virtuoso-1-5",
            },
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)
        record = await agent.communicate("go")
        assert record.token_usage is not None
        # virtuoso-1-5 priced at $0.95 input / $4.00 output / $0.16 cache read
        # per MTok (Fireworks implicit caching: reads are billed, writes are not).
        assert record.token_usage.total_cost_usd == pytest.approx(0.95 + 4.0 + 0.16)

    @pytest.mark.asyncio
    async def test_gemini_pricing_populates_cost(self, tmp_path: Path) -> None:
        """``gemini-3-5-flash`` is in the local pricing table, so cost is computed.

        Mirrors the virtuoso case: a delegate-sdk-only model (no Bedrock proxy
        route) priced from the Autopilot backend's rate card. Vertex implicit
        caching: cache reads are billed, writes are not.
        """
        stdout_script = [
            {
                "type": "result",
                "response": "ok",
                "sessionId": "s",
                "durationMs": 1,
                "usage": {
                    "input_tokens": 1_000_000,
                    "output_tokens": 1_000_000,
                    "cache_read_input_tokens": 1_000_000,
                },
                "model": "gemini-3-5-flash",
            },
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)
        record = await agent.communicate("go")
        assert record.token_usage is not None
        # gemini-3-5-flash priced at $1.50 input / $9.00 output / $0.15 cache
        # read per MTok.
        assert record.token_usage.total_cost_usd == pytest.approx(1.50 + 9.0 + 0.15)

    @pytest.mark.asyncio
    async def test_underscored_model_id_still_priced(self, tmp_path: Path) -> None:
        """The backend echoes underscored model ids; the pricing lookup must normalize.

        Without the underscore→hyphen normalization a priced Anthropic model
        (``claude-sonnet-4-6``) silently reports ``total_cost_usd=None`` on
        every delegate run, breaking cross-agent cost comparison.
        """
        stdout_script = [
            {
                "type": "result",
                "response": "ok",
                "sessionId": "s",
                "durationMs": 1,
                "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
                "model": "claude_sonnet_4_6",
            },
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)
        record = await agent.communicate("go")
        assert record.token_usage is not None
        # claude-sonnet-4-6 priced at $3 input / $15 output per MTok.
        assert record.token_usage.total_cost_usd == pytest.approx(3.0 + 15.0)
        # The raw echoed form is preserved for reporting; only pricing normalizes.
        assert record.model_used == "claude_sonnet_4_6"

    @pytest.mark.asyncio
    async def test_tool_result_failure_marks_error(self, tmp_path: Path) -> None:
        stdout_script = [
            {
                "type": "event",
                "event": {
                    "type": "tool_call",
                    "toolName": "WriteFile",
                    "toolId": "tool-1",
                    "toolArgs": {"path": "x"},
                },
            },
            {
                "type": "event",
                "event": {
                    "type": "tool_result",
                    "toolName": "WriteFile",
                    "toolId": "tool-1",
                    "toolStatus": "failed",
                    "toolResult": "permission denied",
                },
            },
            {"type": "result", "response": "", "sessionId": "s", "durationMs": 1},
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)

        record = await agent.communicate("go")
        assert len(record.commands) == 1
        assert record.commands[0].result_status == "error"
        assert record.commands[0].error_message == "permission denied"

    @pytest.mark.asyncio
    async def test_tool_result_summary_stored_untruncated(self, tmp_path: Path) -> None:
        """``result_summary`` keeps the full tool output (no 200-char cap).

        Matches ClaudeCodeAgent and the CLAUDE.md invariant: sub-agent returns
        and other large tool results are preserved whole; the StreamReader byte
        limit is the only bound, applied upstream.
        """
        big_result = "X" * 5000
        stdout_script = [
            {
                "type": "event",
                "event": {"type": "tool_call", "toolName": "ReadFile", "toolId": "t1", "toolArgs": {"path": "f"}},
            },
            {
                "type": "event",
                "event": {
                    "type": "tool_result",
                    "toolName": "ReadFile",
                    "toolId": "t1",
                    "toolStatus": "completed",
                    "toolResult": big_result,
                },
            },
            {"type": "result", "response": "done", "sessionId": "s", "durationMs": 1},
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)
        record = await agent.communicate("go")
        assert record.commands[0].result_summary == big_result

    @pytest.mark.asyncio
    async def test_host_error_message_raises(self, tmp_path: Path) -> None:
        stdout_script = [
            {"type": "error", "message": "host blew up", "stack": "…"},
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)

        with pytest.raises(AgentCrashError, match="host blew up"):
            await agent.communicate("go")
        # Partial turn must be stashed for the orchestrator to drain.
        assert agent.pending_turn is not None
        assert agent.pending_turn.crashed is True
        assert agent.pending_turn.crash_reason is not None
        assert "host blew up" in agent.pending_turn.crash_reason
        # No result message arrived → num_turns stays None (matches claude).
        assert agent.pending_turn.num_turns is None

    @pytest.mark.asyncio
    async def test_session_conflict_error_kills_host_and_drops_session_id(self, tmp_path: Path) -> None:
        """A backend 409 'reply already being generated' kills the wedged host, clears
        the session id, and flags the retry for a respawn. Dropping the id alone is
        not enough: a live host resumes its in-memory currentSessionId even on a
        ``sessionId: null`` resend, so only a fresh host gets a fresh conversation
        (build 12685191)."""
        stdout_script = [
            {
                "type": "error",
                "message": (
                    "Delegate backend error: Agentic Loop failed with HTTP status 409, "
                    "Error message: <A reply is already being generated for this conversation.>"
                ),
            },
        ]
        agent = DelegateSdkAgent(_make_config())
        proc = _install_fake_process(agent, stdout_script, tmp_path)
        agent._session_id = "wedged-session"

        with pytest.raises(AgentCrashError, match="HTTP status 409"):
            await agent.communicate("go")
        assert agent._session_id is None
        assert proc.returncode is not None  # wedged host was killed
        assert agent._process is None  # handle dropped for the entry guard
        assert agent._respawn_before_retry is True

    @pytest.mark.asyncio
    async def test_session_conflict_on_first_turn_still_kills_host(self, tmp_path: Path) -> None:
        """The build-12685191 shape: the conflict hits on the task's FIRST turn, so
        there is no session id to drop — the host kill + respawn flag must fire
        regardless, or every retry resumes the wedged conversation."""
        stdout_script = [
            {
                "type": "error",
                "message": (
                    "Delegate backend error: Agentic Loop failed with HTTP status 409, "
                    "Error message: <A reply is already being generated for this conversation.>"
                ),
            },
        ]
        agent = DelegateSdkAgent(_make_config())
        proc = _install_fake_process(agent, stdout_script, tmp_path)
        assert agent._session_id is None  # first turn — no result message yet

        with pytest.raises(AgentCrashError, match="HTTP status 409"):
            await agent.communicate("go")
        assert proc.returncode is not None
        assert agent._process is None
        assert agent._respawn_before_retry is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "message",
        [
            pytest.param(
                "Delegate SDK reported error during turn: Delegate backend error: Agentic Loop "
                "failed with HTTP status 403, Error message: <" + _WAF_BLOCK_PAGE_HEAD + _WAF_BLOCK_PAGE_TAIL + ">",
                id="agentic-loop-whole-page",
            ),
            pytest.param(
                # Mid-turn tool-result PATCH: the URL prefix pushes the page past the
                # host's ~50 KB message cap, so it ends mid-font-CSS and the country
                # sentence never arrives — only the <title> identifies the block.
                "Delegate SDK reported error during turn: PATCH https://alpha.uipath.com/codereval/"
                "DefaultTenant/delegate_/v1/chat/sessions/e12a9aa9-0e83-4bea-960d-d749e424e8eb/messages/"
                "tool/ExecuteBashCommand_8 403 Forbidden: " + _WAF_BLOCK_PAGE_HEAD + "…(18180 more chars)",
                id="tool-patch-truncated-before-country-text",
            ),
        ],
    )
    async def test_waf_block_403_rewrites_reason_as_content_filter(self, tmp_path: Path, message: str) -> None:
        """A Cloudflare WAF block (the misleading 'not available in your country' 403
        page) is deterministic per payload, so no retry shape can succeed — the crash
        reason is rewritten to stamp the categorizer's "content filter" signature
        (→ non-retryable AGENT_INVALID_OUTPUT) and explain the real cause instead of
        the country/auth wording (build 12796038: 61 tasks, 2 futile attempts each)."""
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, [{"type": "error", "message": message}], tmp_path)
        agent._session_id = "blocked-session"

        with pytest.raises(AgentCrashError, match="content filter") as exc_info:
            await agent.communicate("go")
        # The rewrite must not tickle earlier categorizer patterns (auth/timeout),
        # and must drop the block-page HTML noise.
        rewritten = str(exc_info.value).lower()
        assert "401" not in rewritten
        assert "unauthorized" not in rewritten
        assert "<!doctype" not in rewritten
        # No respawn machinery: the failure is non-retryable by category, not by
        # host state — session id and respawn flag stay untouched.
        assert agent._session_id == "blocked-session"
        assert agent._respawn_before_retry is False

    @pytest.mark.asyncio
    async def test_sse_connect_timeout_rewrites_reason_as_retryable_connection_error(self, tmp_path: Path) -> None:
        """The host's SSE connect watchdog ("SSE connect timeout after 30s") marks a
        transient backend availability window, not a budget breach — but its "timeout"
        wording routes the crash to the non-retryable AGENT_TIMEOUT. The reason is
        rewritten to defang that wording and stamp the "connection" signature
        (→ retryable AGENT_API_ERROR), keeping the live host and session so the retry
        resumes the same conversation (build 12874194: 2/7 Windows tasks died on
        90s backend-silence windows)."""
        agent = DelegateSdkAgent(_make_config())
        proc = _install_fake_process(
            agent,
            [{"type": "error", "message": "Delegate backend error: SSE connect timeout after 30s"}],
            tmp_path,
        )
        agent._session_id = "live-session"

        with pytest.raises(AgentCrashError, match="connection failure") as exc_info:
            await agent.communicate("go")
        rewritten = str(exc_info.value)
        # The defanged original (with its window length) survives, but no "timeout"
        # substring remains anywhere for the categorizer's AGENT_TIMEOUT arm.
        assert "SSE connect time-out after 30s" in rewritten
        assert "timeout" not in rewritten.lower()
        # The contract that matters: the real framework categorizer routes the
        # rewritten crash to the retryable API-error category.
        assert categorize_error(exc_info.value, {"component": "agent"}) is ErrorCategory.AGENT_API_ERROR
        # Live host + session kept — the resend is a genuine conversation
        # continuation, unlike the session-conflict fresh-host recovery.
        assert agent._session_id == "live-session"
        assert agent._process is proc
        assert agent._respawn_before_retry is False

    @pytest.mark.asyncio
    async def test_host_exit_crash_is_not_rewritten_by_the_sse_marker_in_its_stderr_tail(self, tmp_path: Path) -> None:
        """The SSE fingerprint is a log-line-shaped string, so it also shows up in the
        20-line host stderr tail that _build_crash_message embeds — including for a
        watchdog window the SDK already recovered from. Rewriting a dead-host crash
        there would drop the exit code, assert a live host the same call nulls, and
        turn a terminal crash into retries the entry guard can only fail."""
        agent = DelegateSdkAgent(_make_config())
        # No stdout messages ⇒ the drain's EOF sentinel ⇒ the host-exit crash path.
        _install_fake_process(agent, [], tmp_path)
        agent._stderr_lines.extend(
            [
                "[delegate] WARN SSE connect timeout after 30s — retrying (1/3)",
                "[delegate] FATAL uncaught TypeError: Cannot read properties of undefined",
            ]
        )

        with pytest.raises(AgentCrashError) as exc_info:
            await agent.communicate("go")
        reason = str(exc_info.value)
        assert "Delegate SDK host crashed" in reason
        assert "uncaught TypeError" in reason, "the real cause must survive"
        assert "connection failure" not in reason, "the SSE rewrite must not hijack a host-exit crash"
        # And it stays out of the retryable API-error bucket the rewrite routes to.
        assert categorize_error(exc_info.value, {"component": "agent"}) is not ErrorCategory.AGENT_API_ERROR
        # Dead handle dropped, per the host-exit contract.
        assert agent._process is None

    @pytest.mark.asyncio
    async def test_session_conflict_wins_over_the_sse_marker(self, tmp_path: Path) -> None:
        """Only the fresh-host shape recovers a wedged conversation, so a reason
        carrying both fingerprints must take the session-conflict branch."""
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(
            agent,
            [
                {
                    "type": "error",
                    "message": "SSE connect timeout after 30s; message is already being generated for this session",
                }
            ],
            tmp_path,
        )
        agent._session_id = "wedged-session"

        with pytest.raises(AgentCrashError):
            await agent.communicate("go")

        assert agent._session_id is None
        assert agent._respawn_before_retry is True

    @pytest.mark.asyncio
    async def test_non_conflict_error_keeps_session_id_and_host(self, tmp_path: Path) -> None:
        """Crashes without the conflict fingerprint keep the session id AND the live
        host — resuming the conversation on retry is legitimate there."""
        agent = DelegateSdkAgent(_make_config())
        proc = _install_fake_process(agent, [{"type": "error", "message": "host blew up"}], tmp_path)
        agent._session_id = "healthy-session"

        # The fake's wait() (run by the drain's EOF path) sets returncode, so
        # asserting on returncode is unreliable — count kill calls instead.
        kills = 0
        original_kill = proc.kill

        def _spy_kill() -> None:
            nonlocal kills
            kills += 1
            original_kill()

        proc.kill = _spy_kill  # type: ignore[method-assign]

        with pytest.raises(AgentCrashError):
            await agent.communicate("go")
        assert agent._session_id == "healthy-session"
        assert kills == 0  # host left alive for the retry
        assert agent._process is proc
        assert agent._respawn_before_retry is False

    @pytest.mark.asyncio
    async def test_error_event_then_result_raises(self, tmp_path: Path) -> None:
        stdout_script = [
            {"type": "event", "event": {"type": "error", "error": "network down"}},
            {"type": "result", "response": "", "sessionId": "s", "durationMs": 1},
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)

        with pytest.raises(AgentCrashError, match="network down"):
            await agent.communicate("go")
        assert agent.pending_turn is not None
        assert agent.pending_turn.crashed is True

    @pytest.mark.asyncio
    async def test_host_subprocess_exit_raises_agent_crash(self, tmp_path: Path) -> None:
        """Host stdout EOF mid-turn must surface as AgentCrashError + pending_turn."""
        # Empty stdout script + stderr that hints at why → drain emits the None
        # sentinel immediately; communicate() sees msg is None and raises.
        agent = DelegateSdkAgent(_make_config())
        proc = _install_fake_process(agent, [], tmp_path)
        proc.returncode = 1  # simulate "host already exited"
        agent._stderr_lines = ["fatal: out of memory"]

        with pytest.raises(AgentCrashError, match="host crashed"):
            await agent.communicate("go")
        assert agent.pending_turn is not None
        assert agent.pending_turn.crashed is True

    @pytest.mark.asyncio
    async def test_communicate_timeout_kills_host_and_stashes_partial(self, tmp_path: Path) -> None:
        """Wall-clock deadline must kill the host, stash a crashed partial, and raise.

        Drives the ``except TimeoutError`` branch: a reader that never yields a
        result + a tiny ``timeout`` makes ``asyncio.wait_for`` fire. Per the
        Agent contract this is one of the two failure modes — the host is
        force-killed, a ``crashed=True`` partial lands on ``pending_turn``, and
        ``TurnTimeoutError`` is raised.
        """
        agent = DelegateSdkAgent(_make_config())
        proc = _FakeProcess([])
        proc.stdout = _NeverStreamReader()  # type: ignore[assignment]
        agent._process = proc  # type: ignore[assignment]
        agent.working_directory = tmp_path
        agent._stdout_task = asyncio.create_task(agent._drain_stdout(proc.stdout))  # type: ignore[arg-type]
        agent._stderr_task = asyncio.create_task(agent._drain_stderr(proc.stderr))  # type: ignore[arg-type]

        # Spy on kill(): the fake's wait() resets returncode to 0 afterwards, so
        # asserting on returncode is unreliable — count the kill call instead.
        kills = 0
        original_kill = proc.kill

        def _spy_kill() -> None:
            nonlocal kills
            kills += 1
            original_kill()

        proc.kill = _spy_kill  # type: ignore[method-assign]

        try:
            with pytest.raises(TurnTimeoutError):
                await agent.communicate("go", timeout=0.05)
            assert kills >= 1, "host was not killed on timeout"
            assert agent.pending_turn is not None
            assert agent.pending_turn.crashed is True
            assert agent.get_state() == AgentState.ERROR
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_retry_after_host_crash_fails_fast(self, tmp_path: Path) -> None:
        """An AGENT_CRASH retry re-enters communicate() on the dead, un-respawned host.

        The orchestrator's retry loop only re-calls communicate() — never
        start() — and nothing respawns the host. The crash branches drop the
        process handle, so the retried call must fail fast with the typed
        AgentCrashError (drain/discard still fire) instead of writing into a
        broken pipe (mis-categorized as retryable AGENT_API_ERROR) or blocking
        forever on the already-consumed EOF sentinel.
        """
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, [], tmp_path)  # immediate EOF → mid-turn crash

        with pytest.raises(AgentCrashError, match="host crashed"):
            await agent.communicate("first attempt")
        await agent.discard_pending_turn()  # what the orchestrator's failure hook does

        # Retry on the same agent, no start() — typed fail-fast, no iteration leak.
        with pytest.raises(AgentCrashError, match="not running"):
            await agent.communicate("retry")
        assert agent.pending_turn is None  # the guard fires before a turn opens
        assert agent._iteration == 0

    @pytest.mark.asyncio
    async def test_retry_after_timeout_fails_fast(self, tmp_path: Path) -> None:
        """After a turn timeout force-kills the host, a retried communicate() fails fast."""
        agent = DelegateSdkAgent(_make_config())
        proc = _FakeProcess([])
        proc.stdout = _NeverStreamReader()  # type: ignore[assignment]
        agent._process = proc  # type: ignore[assignment]
        agent.working_directory = tmp_path
        agent._stdout_task = asyncio.create_task(agent._drain_stdout(proc.stdout))  # type: ignore[arg-type]
        agent._stderr_task = asyncio.create_task(agent._drain_stderr(proc.stderr))  # type: ignore[arg-type]
        try:
            with pytest.raises(TurnTimeoutError):
                await agent.communicate("go", timeout=0.05)
            await agent.discard_pending_turn()

            with pytest.raises(AgentCrashError, match="not running"):
                await agent.communicate("retry", timeout=0.05)
            assert agent._iteration == 0
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_send_failure_still_emits_event_boundary(self, tmp_path: Path) -> None:
        """A pre-loop send failure must still produce AgentStart/AgentEnd + pending_turn.

        Regression: _send_command used to run before the try block (and before
        the AgentStartEvent), so a broken stdin pipe propagated as a bare
        BrokenPipeError — no terminal event, pending_turn unset, and the
        iteration bump leaked because the orchestrator only drains/discards on
        AgentCrashError / TurnTimeoutError.
        """
        agent = DelegateSdkAgent(_make_config())
        proc = _install_fake_process(agent, [], tmp_path)

        def _broken_write(_data: bytes) -> None:
            raise BrokenPipeError("stdin gone")

        proc.stdin.write = _broken_write  # type: ignore[method-assign]

        callback = _CapturingCallback()
        with pytest.raises(AgentCrashError, match="stdin gone"):
            await agent.communicate("go", stream_callback=callback)

        assert len([e for e in callback.events if isinstance(e, AgentStartEvent)]) == 1
        assert len([e for e in callback.events if isinstance(e, AgentEndEvent)]) == 1
        assert agent.pending_turn is not None
        assert agent.pending_turn.crashed is True

        await agent.discard_pending_turn()
        assert agent._iteration == 0

    @pytest.mark.asyncio
    async def test_orphaned_tool_call_force_closed_unresolved(self, tmp_path: Path) -> None:
        """A tool_call that never receives a tool_result is force-closed by _finalize.

        The command survives in the record (result_status="unknown", zeroed
        duration) and the event tree stays balanced via a status=UNRESOLVED
        ToolEndEvent — required for renderers/collector symmetry.
        """
        stdout_script = [
            {
                "type": "event",
                "event": {"type": "tool_call", "toolName": "Bash", "toolId": "orphan-1", "toolArgs": {"cmd": "ls"}},
            },
            {"type": "result", "response": "done", "sessionId": "s", "durationMs": 1},
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)

        callback = _CapturingCallback()
        record = await agent.communicate("go", stream_callback=callback)

        assert len(record.commands) == 1
        orphan = record.commands[0]
        assert orphan.tool_id == "orphan-1"
        assert orphan.result_status == "unknown"
        assert orphan.duration_ms == 0.0
        unresolved = [
            e for e in callback.events if isinstance(e, ToolEndEvent) and e.status == ToolEndStatus.UNRESOLVED
        ]
        assert len(unresolved) == 1
        assert unresolved[0].tool.tool_id == "orphan-1"

    @pytest.mark.asyncio
    async def test_discard_pending_turn_rolls_back_iteration(self, tmp_path: Path) -> None:
        """After a crash, discard_pending_turn must clear the slot and roll back the counter."""
        stdout_script = [{"type": "error", "message": "boom"}]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)

        with pytest.raises(AgentCrashError):
            await agent.communicate("go")
        assert agent.pending_turn is not None
        assert agent._iteration == 1

        await agent.discard_pending_turn()
        assert agent.pending_turn is None
        assert agent._iteration == 0

    @pytest.mark.asyncio
    async def test_discard_rolls_back_iteration_when_partial_build_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A swallowed partial-build still rolls back the iteration counter.

        When the EventCollector's build_turn_record() raises, _finalize swallows
        it and leaves pending_turn=None — so discard_pending_turn() cannot key the
        rollback off pending_turn alone. The _iteration_was_incremented flag
        carries the signal; without it the counter stays bumped and the retry
        reuses a wrong iteration number.
        """
        stdout_script = [{"type": "error", "message": "boom"}]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)

        # Force the partial-build to fail so _finalize swallows it.
        def _boom(*_args: Any, **_kwargs: Any) -> object:
            raise RuntimeError("build failed")

        monkeypatch.setattr(EventCollector, "build_turn_record", _boom)

        with pytest.raises(AgentCrashError):
            await agent.communicate("go")
        # Partial build was swallowed → no pending_turn, but the bump happened.
        assert agent.pending_turn is None
        assert agent._iteration == 1
        assert agent._iteration_was_incremented is True

        await agent.discard_pending_turn()
        assert agent._iteration == 0
        assert agent._iteration_was_incremented is False

    @pytest.mark.asyncio
    async def test_discard_pending_turn_double_call_is_idempotent(self, tmp_path: Path) -> None:
        """A second discard_pending_turn() must not roll the iteration back twice.

        The orchestrator's failure path may drain + discard, and stop() also
        nulls the slot — so a redundant call has to be a no-op once both signals
        (pending_turn, _iteration_was_incremented) are already cleared.
        """
        stdout_script = [{"type": "error", "message": "boom"}]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)

        with pytest.raises(AgentCrashError):
            await agent.communicate("go")
        assert agent._iteration == 1

        await agent.discard_pending_turn()
        assert agent._iteration == 0
        assert agent.pending_turn is None
        assert agent._iteration_was_incremented is False

        # Second call: no pending_turn and no increment flag → no further rollback.
        await agent.discard_pending_turn()
        assert agent._iteration == 0
        assert agent.pending_turn is None
        assert agent._iteration_was_incremented is False

    @pytest.mark.asyncio
    async def test_communicate_without_start_raises(self) -> None:
        agent = DelegateSdkAgent(_make_config())
        with pytest.raises(RuntimeError, match="Agent not started"):
            await agent.communicate("x")

    @pytest.mark.asyncio
    async def test_second_turn_reuses_session_id(self, tmp_path: Path) -> None:
        stdout_script = [
            {"type": "result", "response": "first", "sessionId": "sess-X", "durationMs": 1},
            {"type": "result", "response": "second", "sessionId": "sess-X", "durationMs": 1},
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)

        await agent.communicate("first")
        await agent.communicate("second")

        stdin_lines = agent._process.stdin.lines  # type: ignore[union-attr]
        first_sent = json.loads(stdin_lines[0])
        second_sent = json.loads(stdin_lines[1])
        assert first_sent["sessionId"] is None
        assert second_sent["sessionId"] == "sess-X"
        # Prompts pass through verbatim on every turn (no working-dir prefix)
        assert first_sent["prompt"] == "first"
        assert second_sent["prompt"] == "second"


# ---- per-generation transcript (TurnRecord.messages) ------------------------


class TestTranscriptMessages:
    """AssistantMessage reconstruction + per-round-trip token attribution.

    The evalboard renders ``TurnRecord.messages`` as the message timeline and
    sums its token buckets as the source of truth, so the delegate agent must
    surface one ``AssistantMessage`` per backend round-trip — with buckets
    zipped from the host's ``turnUsages`` — instead of leaving the transcript
    empty and booking the whole turn on the reconciliation entry.
    """

    @pytest.mark.asyncio
    async def test_messages_built_per_round_trip_with_token_attribution(self, tmp_path: Path) -> None:
        stdout_script = [
            # Round-trip 1: thinking, then its step-start text, then a tool call.
            {"type": "event", "event": {"type": "thinking", "content": "hmm "}},
            {"type": "event", "event": {"type": "message", "content": "Let me read it.", "isStepStart": True}},
            {
                "type": "event",
                "event": {"type": "tool_call", "toolName": "Read", "toolId": "t1", "toolArgs": {"path": "f"}},
            },
            {
                "type": "event",
                "event": {
                    "type": "tool_result",
                    "toolName": "Read",
                    "toolId": "t1",
                    "toolStatus": "completed",
                    "toolResult": "data",
                },
            },
            # Round-trip 2: final answer streamed in two deltas.
            {"type": "event", "event": {"type": "message", "content": "All ", "isStepStart": True}},
            {"type": "event", "event": {"type": "message", "content": "done.", "isStepStart": False}},
            {
                "type": "result",
                "response": "All done.",
                "sessionId": "s",
                "durationMs": 5,
                "usage": {
                    "input_tokens": 150,
                    "output_tokens": 50,
                    "cache_creation_input_tokens": 10,
                    "cache_read_input_tokens": 45,
                },
                "turnUsages": [
                    {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_creation_input_tokens": 10,
                        "cache_read_input_tokens": 5,
                    },
                    {
                        "input_tokens": 50,
                        "output_tokens": 30,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 40,
                    },
                ],
            },
        ]
        agent = DelegateSdkAgent(_make_config(model="claude-sonnet-4-6"))
        _install_fake_process(agent, stdout_script, tmp_path)

        record = await agent.communicate("go")

        assistants = [m for m in record.messages if isinstance(m, AssistantMessage)]
        assert len(assistants) == 2
        first, second = assistants

        # Round-trip 1: thinking + text + tool_use blocks in emission order.
        assert [b.block_type for b in first.content_blocks] == ["thinking", "text", "tool_use"]
        assert first.content_blocks[0].thinking == "hmm "
        assert first.content_blocks[1].text == "Let me read it."
        assert first.content_blocks[2].tool_use_id == "t1"
        assert first.tool_use_ids == ["t1"]
        assert (first.input_tokens, first.output_tokens) == (100, 20)
        assert (first.cache_creation_tokens, first.cache_read_tokens) == (10, 5)
        assert first.model == "claude-sonnet-4-6"
        assert first.generation_duration_ms >= 0.0

        # Round-trip 2: streaming deltas merged into one text block.
        assert [b.block_type for b in second.content_blocks] == ["text"]
        assert second.content_blocks[0].text == "All done."
        assert (second.input_tokens, second.output_tokens) == (50, 30)
        assert (second.cache_creation_tokens, second.cache_read_tokens) == (0, 40)

        # Synthetic per-generation message_ids (CodexAgent pattern) keep the
        # evalboard's grouping 1:1 instead of falling back to gap heuristics.
        assert first.message_id == "delegate-1-msg-0"
        assert second.message_id == "delegate-1-msg-1"

        # Per-message buckets sum to the turn total, so the collector emits NO
        # reconciliation entry — the transcript itself reconciles to the bill.
        assert not any(isinstance(m, ReconciliationMessage) for m in record.messages)
        assert record.token_usage is not None

        # The full transcript-sums-to-total invariant the evalboard relies on:
        # ALL four buckets summed across EVERY message (assistant + any
        # reconciliation) equal token_usage exactly — not just uncached input.
        # A future bucket that the transcript builder forgets to attribute would
        # break this even though the per-message spot-checks above still pass.
        tu = record.token_usage
        assert sum(getattr(m, "input_tokens", 0) for m in record.messages) == tu.uncached_input_tokens == 150
        assert sum(getattr(m, "output_tokens", 0) for m in record.messages) == tu.output_tokens == 50
        assert (
            sum(getattr(m, "cache_creation_tokens", 0) for m in record.messages) == tu.cache_creation_input_tokens == 10
        )
        assert sum(getattr(m, "cache_read_tokens", 0) for m in record.messages) == tu.cache_read_input_tokens == 45
        assert record.token_usage.output_tokens == 50

    @pytest.mark.asyncio
    async def test_tool_only_round_trips_become_separate_generations(self, tmp_path: Path) -> None:
        """Consecutive tool-only rounds split on the tool_result boundary, and a
        thinking event arriving before its round's step-start text joins that
        round's segment instead of opening a second one."""
        stdout_script = [
            {"type": "event", "event": {"type": "tool_call", "toolName": "Read", "toolId": "t1", "toolArgs": {}}},
            {
                "type": "event",
                "event": {
                    "type": "tool_result",
                    "toolName": "Read",
                    "toolId": "t1",
                    "toolStatus": "completed",
                    "toolResult": "a",
                },
            },
            {"type": "event", "event": {"type": "tool_call", "toolName": "Edit", "toolId": "t2", "toolArgs": {}}},
            {
                "type": "event",
                "event": {
                    "type": "tool_result",
                    "toolName": "Edit",
                    "toolId": "t2",
                    "toolStatus": "completed",
                    "toolResult": "b",
                },
            },
            {"type": "event", "event": {"type": "thinking", "content": "wrapping up"}},
            {"type": "event", "event": {"type": "message", "content": "Done.", "isStepStart": True}},
            {
                "type": "result",
                "response": "Done.",
                "sessionId": "s",
                "durationMs": 5,
                "usage": {"input_tokens": 60, "output_tokens": 6},
                "turnUsages": [
                    {"input_tokens": 10, "output_tokens": 1},
                    {"input_tokens": 20, "output_tokens": 2},
                    {"input_tokens": 30, "output_tokens": 3},
                ],
            },
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)

        record = await agent.communicate("go")

        assistants = [m for m in record.messages if isinstance(m, AssistantMessage)]
        assert len(assistants) == 3
        assert assistants[0].tool_use_ids == ["t1"]
        assert assistants[1].tool_use_ids == ["t2"]
        assert [b.block_type for b in assistants[2].content_blocks] == ["thinking", "text"]
        assert [m.output_tokens for m in assistants] == [1, 2, 3]
        assert [m.input_tokens for m in assistants] == [10, 20, 30]
        assert not any(isinstance(m, ReconciliationMessage) for m in record.messages)

    @pytest.mark.asyncio
    async def test_turn_usages_misalignment_skips_attribution(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A segment/turnUsages count mismatch must not misattribute buckets —
        stamping is skipped wholesale and the reconciliation entry carries the
        full turn total (the pre-fix behaviour, now with visible messages)."""
        stdout_script = [
            {"type": "event", "event": {"type": "message", "content": "Done.", "isStepStart": True}},
            {
                "type": "result",
                "response": "Done.",
                "sessionId": "s",
                "durationMs": 5,
                "usage": {"input_tokens": 30, "output_tokens": 3},
                "turnUsages": [
                    {"input_tokens": 10, "output_tokens": 1},
                    {"input_tokens": 20, "output_tokens": 2},
                ],
            },
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)

        with caplog.at_level(logging.WARNING, logger="coder_eval.agents.delegate_sdk_agent"):
            record = await agent.communicate("go")

        assistants = [m for m in record.messages if isinstance(m, AssistantMessage)]
        assert len(assistants) == 1
        assert (assistants[0].input_tokens, assistants[0].output_tokens) == (0, 0)
        reconciliations = [m for m in record.messages if isinstance(m, ReconciliationMessage)]
        assert len(reconciliations) == 1
        assert (reconciliations[0].input_tokens, reconciliations[0].output_tokens) == (30, 3)
        assert any("turnUsages" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_no_turn_usages_builds_messages_without_tokens(self, tmp_path: Path) -> None:
        """Back-compat: an un-rebuilt host sends no ``turnUsages`` — messages
        still appear (content + timing) and the reconciliation entry carries
        the turn total, preserving the transcript-sums-to-total invariant."""
        stdout_script = [
            {"type": "event", "event": {"type": "message", "content": "Done.", "isStepStart": True}},
            {
                "type": "result",
                "response": "Done.",
                "sessionId": "s",
                "durationMs": 5,
                "usage": {"input_tokens": 30, "output_tokens": 3},
            },
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)

        record = await agent.communicate("go")

        assistants = [m for m in record.messages if isinstance(m, AssistantMessage)]
        assert len(assistants) == 1
        assert assistants[0].content_blocks[0].text == "Done."
        assert (assistants[0].input_tokens, assistants[0].output_tokens) == (0, 0)
        reconciliations = [m for m in record.messages if isinstance(m, ReconciliationMessage)]
        assert len(reconciliations) == 1
        assert (reconciliations[0].input_tokens, reconciliations[0].output_tokens) == (30, 3)

    @pytest.mark.asyncio
    async def test_crash_partial_includes_transcript_so_far(self, tmp_path: Path) -> None:
        """A mid-turn host death stashes a partial TurnRecord whose messages
        carry the generations reconstructed before the crash (token-less — the
        result message never arrived)."""
        stdout_script = [
            {"type": "event", "event": {"type": "message", "content": "Working...", "isStepStart": True}},
            # EOF follows (no result) → AgentCrashError.
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)

        with pytest.raises(AgentCrashError):
            await agent.communicate("go")

        partial = agent.pending_turn
        assert partial is not None
        assistants = [m for m in partial.messages if isinstance(m, AssistantMessage)]
        assert len(assistants) == 1
        assert assistants[0].content_blocks[0].text == "Working..."
        assert (assistants[0].input_tokens, assistants[0].output_tokens) == (0, 0)


# ---- stop() ----------------------------------------------------------------


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_sends_destroy_and_waits(self, tmp_path: Path) -> None:
        stdout_script = [{"type": "destroyed"}]
        agent = DelegateSdkAgent(_make_config())
        proc = _install_fake_process(agent, stdout_script, tmp_path)

        await agent.stop()

        # destroy was sent
        assert proc.stdin.lines
        cmd = json.loads(proc.stdin.lines[-1])
        assert cmd == {"cmd": "destroy"}
        assert agent.get_state().value == "finished"
        assert agent._process is None

    @pytest.mark.asyncio
    async def test_stop_clears_pending_turn(self, tmp_path: Path) -> None:
        """Per the Agent contract, stop() must clear any leftover pending_turn."""
        from coder_eval.models import TurnRecord

        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, [{"type": "destroyed"}], tmp_path)
        agent.pending_turn = TurnRecord(
            iteration=1,
            user_input="x",
            agent_output="",
            commands=[],
            timestamp=datetime.now(),
            duration_seconds=0.0,
            assistant_turn_count=0,
            max_turns_exhausted=False,
            crashed=True,
            crash_reason="leftover from a prior failed turn",
        )

        await agent.stop()

        assert agent.pending_turn is None


# ---- start() lifecycle -----------------------------------------------------


def _patch_host_spawn(
    monkeypatch: pytest.MonkeyPatch, stdout_lines: list[dict[str, Any]], tmp_path: Path
) -> _FakeProcess:
    """Stub host discovery + subprocess spawn so ``start()`` can run without Node.

    ``_resolve_stdio_bundle`` returns a stub file and ``create_subprocess_exec``
    yields a :class:`_FakeProcess` replaying ``stdout_lines`` through the real
    drain → queue → ``_read_until`` path that ``start()`` exercises.
    """
    bundle = tmp_path / "delegate_stdio.mjs"
    bundle.write_text("// stub", encoding="utf-8")
    monkeypatch.setattr("coder_eval.agents.delegate_sdk_agent._resolve_stdio_bundle", lambda: bundle)
    proc = _FakeProcess(stdout_lines)

    async def _fake_exec(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return proc


class TestStart:
    @pytest.mark.asyncio
    async def test_successful_init_handshake(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A clean ``init`` → ``init_ok`` handshake leaves the agent WORKING and spawned."""
        proc = _patch_host_spawn(monkeypatch, [{"type": "init_ok"}], tmp_path)
        agent = DelegateSdkAgent(_make_config(model="claude_sonnet_4_5"))
        try:
            await agent.start(str(tmp_path))

            assert agent.get_state() == AgentState.WORKING
            assert agent.working_directory == tmp_path
            # The init command was serialised to the host with the built options.
            sent = json.loads(proc.stdin.lines[0])
            assert sent["cmd"] == "init"
            assert sent["options"]["model"] == "claude_sonnet_4_5"
            assert sent["options"]["workingDirectory"] == str(tmp_path)
            # get_sdk_options() exposes exactly what was sent.
            assert agent.get_sdk_options() == sent["options"]
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_env_path_prepend_reaches_the_host_as_shell_path_prepend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``start(env_path_prepend=...)`` lands in the init frame the host receives.

        This is the whole delivery path for sandbox mock CLIs on the delegate-sdk
        agent — the adapter cannot prepend on its own PATH, because shell commands
        run inside the interop service.
        """
        proc = _patch_host_spawn(monkeypatch, [{"type": "init_ok"}], tmp_path)
        mocks = tmp_path / "mocks"
        agent = DelegateSdkAgent(_make_config())
        try:
            await agent.start(str(tmp_path), env_path_prepend=[str(mocks)])

            sent = json.loads(proc.stdin.lines[0])
            assert sent["options"]["shellPathPrepend"] == [str(mocks)]
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_start_without_env_path_prepend_omits_the_option(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _patch_host_spawn(monkeypatch, [{"type": "init_ok"}], tmp_path)
        agent = DelegateSdkAgent(_make_config())
        try:
            await agent.start(str(tmp_path))

            sent = json.loads(proc.stdin.lines[0])
            assert "shellPathPrepend" not in sent["options"]
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_non_auth_error_on_init_raises_runtime_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-auth ``{"type":"error"}`` ack stays a (retryable) RuntimeError, not a crash.

        Init errors that aren't auth-shaped (transient backend hiccups, etc.) keep
        the old behaviour so the orchestrator's AGENT_API_ERROR retry can recover.
        """
        _patch_host_spawn(monkeypatch, [{"type": "error", "message": "backend 500 internal error"}], tmp_path)
        agent = DelegateSdkAgent(_make_config())
        try:
            with pytest.raises(RuntimeError, match="Delegate SDK init failed: backend 500 internal error") as ei:
                await agent.start(str(tmp_path))
            # Specifically NOT the non-retryable config error.
            assert not isinstance(ei.value, AgentConfigError)
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_auth_error_on_init_short_circuits_with_expiry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An auth init failure raises the non-retryable AgentConfigError and reports expiry.

        The host's generic "Auth required" is enriched with the saved login's
        age so an *expired* token is distinguishable from an *absent* one, and the
        AgentConfigError (vs RuntimeError) routes to the non-retryable
        AGENT_CONFIG_ERROR category — no ~40s of API-error backoff.
        """
        auth_file = tmp_path / "sdk-auth.json"
        expired_at = int(time.time()) - 3 * 86_400  # 3 days ago
        # synchronous test-setup write before any awaited call; no event loop to block
        auth_file.write_text(json.dumps({"expiresAt": expired_at}), encoding="utf-8")  # noqa: CE002
        monkeypatch.setattr("coder_eval.agents.delegate_sdk_agent._saved_auth_file", lambda: auth_file)
        _patch_host_spawn(
            monkeypatch,
            [{"type": "error", "message": "Auth required: set AUTH_TOKEN/TENANT_ID/ORG_ID env vars or run …"}],
            tmp_path,
        )
        agent = DelegateSdkAgent(_make_config())
        try:
            with pytest.raises(AgentConfigError) as ei:
                await agent.start(str(tmp_path))
            msg = str(ei.value)
            assert "expired" in msg
            assert "delegate-cli login --env" in msg
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_auth_error_on_init_reports_absent_login(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When there is no saved login file, the auth error says so explicitly."""
        missing = tmp_path / "does-not-exist.json"
        monkeypatch.setattr("coder_eval.agents.delegate_sdk_agent._saved_auth_file", lambda: missing)
        _patch_host_spawn(monkeypatch, [{"type": "error", "message": "401 Unauthorized"}], tmp_path)
        agent = DelegateSdkAgent(_make_config())
        try:
            with pytest.raises(AgentConfigError, match="No saved login found"):
                await agent.start(str(tmp_path))
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_host_eof_during_init_raises_agent_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the host exits before acking init, ``_read_until`` raises AgentCrashError.

        Empty stdout → the drain posts the EOF sentinel → ``_read_until`` reads
        ``None`` and raises (the bare-exception path, since start() has no partial
        TurnRecord to stash). The crash message is built from the exit code.
        """
        _patch_host_spawn(monkeypatch, [], tmp_path)
        agent = DelegateSdkAgent(_make_config())
        try:
            with pytest.raises(AgentCrashError, match="host crashed"):
                await agent.start(str(tmp_path))
        finally:
            await agent.stop()


# ---- kill() / kill_sync() --------------------------------------------------


class TestKill:
    def test_kill_sync_terminates_running_process(self, tmp_path: Path) -> None:
        agent = DelegateSdkAgent(_make_config())
        proc = _FakeProcess([])
        agent._process = proc  # type: ignore[assignment]
        agent.kill_sync()
        assert proc.returncode == -9

    def test_kill_sync_noop_without_process(self) -> None:
        """No subprocess yet (start() never ran) — kill_sync must be a safe no-op."""
        agent = DelegateSdkAgent(_make_config())
        assert agent._process is None
        agent.kill_sync()  # must not raise

    def test_kill_sync_skips_already_exited_process(self, tmp_path: Path) -> None:
        """An already-exited process must not be killed again (returncode is set)."""
        agent = DelegateSdkAgent(_make_config())
        proc = _FakeProcess([])
        proc.returncode = 0  # already exited cleanly

        killed = False

        def _spy_kill() -> None:
            nonlocal killed
            killed = True

        proc.kill = _spy_kill  # type: ignore[method-assign]
        agent._process = proc  # type: ignore[assignment]
        agent.kill_sync()
        assert killed is False

    @pytest.mark.asyncio
    async def test_kill_delegates_to_kill_sync(self, tmp_path: Path) -> None:
        """The async ``kill()`` is a fire-and-forget wrapper over ``kill_sync()``."""
        agent = DelegateSdkAgent(_make_config())
        proc = _FakeProcess([])
        agent._process = proc  # type: ignore[assignment]
        await agent.kill()
        assert proc.returncode == -9


# ---- drain resilience ------------------------------------------------------


class TestDrainResilience:
    @pytest.mark.asyncio
    async def test_drain_stderr_drops_over_limit_line_and_returns(self, caplog: pytest.LogCaptureFixture) -> None:
        """An over-limit stderr line is drained + dropped with a warning; the task survives.

        A dead ``_stderr_task`` would strand the EOF await in ``_drain_stdout``
        (which joins it) and blank the crash-message stderr tail, so this branch
        must keep reading rather than letting ``LimitOverrunError`` kill it.
        """
        agent = DelegateSdkAgent(_make_config())
        reader = _LimitOverrunReader()
        with caplog.at_level(logging.WARNING, logger="coder_eval.agents.delegate_sdk_agent"):
            await agent._drain_stderr(reader)  # type: ignore[arg-type]
        assert any("exceeded" in rec.message and "dropped" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_drain_stdout_over_limit_treats_host_as_crashed(self, tmp_path: Path) -> None:
        """An over-limit stdout line can't be reassembled, so the host is treated as crashed.

        The drain drops the line, records a marker in ``_stderr_lines``, and routes
        through the EOF sentinel → ``communicate()`` sees ``None`` and raises
        AgentCrashError instead of blocking on the queue forever.
        """
        agent = DelegateSdkAgent(_make_config())
        proc = _FakeProcess([])
        proc.stdout = _LimitOverrunReader()  # type: ignore[assignment]
        agent._process = proc  # type: ignore[assignment]
        agent.working_directory = tmp_path
        agent._stdout_task = asyncio.create_task(agent._drain_stdout(proc.stdout))  # type: ignore[arg-type]
        agent._stderr_task = asyncio.create_task(agent._drain_stderr(proc.stderr))  # type: ignore[arg-type]

        with pytest.raises(AgentCrashError):
            await agent.communicate("go")
        assert any("exceeded" in line for line in agent._stderr_lines)


# ---- get_sdk_options -------------------------------------------------------


class TestGetSdkOptions:
    def test_none_before_start(self) -> None:
        agent = DelegateSdkAgent(_make_config())
        assert agent.get_sdk_options() is None


# ---- _find_pending_by_name -------------------------------------------------


class TestFindPendingByName:
    def test_returns_none_for_empty_name(self) -> None:
        assert DelegateSdkAgent._find_pending_by_name({}, "") is None

    def test_matches_by_name_when_id_missing(self) -> None:
        """Tool result without toolId must still update the pending command for that tool."""
        from coder_eval.models import CommandTelemetry

        pending_id = "synth-id"
        commands = {
            pending_id: {
                "telemetry": CommandTelemetry(
                    tool_name="Bash",
                    tool_id=pending_id,
                    timestamp=datetime.now(),
                    parameters={},
                    sequence_number=0,
                ),
                "start_time": 0.0,
            }
        }
        match = DelegateSdkAgent._find_pending_by_name(commands, "Bash")
        assert match == pending_id


# ---- auth-failure diagnostics ----------------------------------------------


class TestAuthDiagnostics:
    @pytest.mark.parametrize(
        "message",
        [
            "Auth required: set AUTH_TOKEN/TENANT_ID/ORG_ID env vars or run …",
            "401 Unauthorized",
            "HTTP 403 forbidden",
            "Authentication failed",
            "invalid credentials supplied",
            "token expired",
        ],
    )
    def test_auth_messages_detected(self, message: str) -> None:
        assert _is_auth_init_error(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            "backend 500 internal error",
            "Model 'foo' is not available",
            "connection reset by peer",
        ],
    )
    def test_non_auth_messages_not_detected(self, message: str) -> None:
        assert _is_auth_init_error(message) is False

    def test_describe_absent_saved_auth(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("coder_eval.agents.delegate_sdk_agent._saved_auth_file", lambda: tmp_path / "missing.json")
        assert "No saved login found" in _describe_saved_auth()

    def test_describe_expired_saved_auth(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        auth_file = tmp_path / "sdk-auth.json"
        auth_file.write_text(json.dumps({"expiresAt": int(time.time()) - 2 * 86_400}), encoding="utf-8")
        monkeypatch.setattr("coder_eval.agents.delegate_sdk_agent._saved_auth_file", lambda: auth_file)
        described = _describe_saved_auth()
        assert "expired" in described
        assert "2 day" in described

    def test_describe_unexpired_saved_auth(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        auth_file = tmp_path / "sdk-auth.json"
        auth_file.write_text(json.dumps({"expiresAt": int(time.time()) + 3600}), encoding="utf-8")
        monkeypatch.setattr("coder_eval.agents.delegate_sdk_agent._saved_auth_file", lambda: auth_file)
        assert "unexpired" in _describe_saved_auth()

    def test_describe_malformed_saved_auth_is_safe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        auth_file = tmp_path / "sdk-auth.json"
        auth_file.write_text("not json{", encoding="utf-8")
        monkeypatch.setattr("coder_eval.agents.delegate_sdk_agent._saved_auth_file", lambda: auth_file)
        # Must not raise; falls back to a read-error description.
        assert "could not be read" in _describe_saved_auth()


# ---- stop() timeout fallback -----------------------------------------------


class TestStopTimeout:
    @pytest.mark.asyncio
    async def test_stop_kills_process_if_wait_times_out(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When ``destroy`` is sent but wait() times out, stop() must kill the process."""

        class _HangingProcess(_FakeProcess):
            """Process whose wait() times out exactly once, then succeeds."""

            def __init__(self) -> None:
                super().__init__([{"type": "destroyed"}])
                self.kill_called = False
                self._first_wait = True

            async def wait(self) -> int:
                if self._first_wait:
                    self._first_wait = False
                    await asyncio.sleep(10)  # will be cancelled by wait_for timeout
                self.returncode = -9
                return -9

            def kill(self) -> None:
                self.kill_called = True

        agent = DelegateSdkAgent(_make_config())
        proc = _HangingProcess()
        agent._process = proc  # type: ignore[assignment]
        agent.working_directory = tmp_path

        # Patch the timeout constant to something tiny to keep the test fast.
        monkeypatch.setattr("coder_eval.agents.delegate_sdk_agent._STOP_TIMEOUT_SEC", 0.01)

        await agent.stop()

        assert proc.kill_called is True
        assert agent.get_state().value == "finished"


# ---- registry / orchestrator dispatch -------------------------------------


def test_delegate_sdk_agent_is_registered() -> None:
    """The @AgentRegistry.register decorator should have wired up the agent at import time."""
    from coder_eval.agents.registry import AgentRegistry

    registration = AgentRegistry.get("delegate-sdk")
    assert registration is not None
    assert registration.agent_class is DelegateSdkAgent
    assert registration.config_class is DelegateSdkAgentConfig


def test_create_agent_dispatches_to_delegate_sdk_agent() -> None:
    """The registry factory must construct a DelegateSdkAgent for "delegate-sdk"."""
    from coder_eval.agents.registry import create_agent

    config = _make_config()
    agent = create_agent("delegate-sdk", config, route=DirectRoute())
    assert isinstance(agent, DelegateSdkAgent)
    assert agent.config is config


class TestReviewRegressions:
    """Regression tests for code-review findings (2026-06-15)."""

    @pytest.mark.asyncio
    async def test_agent_output_uses_streamed_text_when_result_omits_response(self, tmp_path: Path) -> None:
        """If the host streams the final answer in deltas and the ``result`` message
        carries no ``response``, ``agent_output`` must be the full merged text — not
        the trailing fragment. Guards the delta-replace truncation bug."""
        stdout_script = [
            {"type": "event", "event": {"type": "message", "content": "All ", "isStepStart": True}},
            {"type": "event", "event": {"type": "message", "content": "done.", "isStepStart": False}},
            # result with NO `response` field (host streamed the answer as deltas).
            {"type": "result", "sessionId": "s", "durationMs": 5},
        ]
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, stdout_script, tmp_path)

        record = await agent.communicate("go")

        assert record.agent_output == "All done."  # not just "done."

    @pytest.mark.asyncio
    async def test_start_reclaims_orphaned_host_on_retry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The orchestrator re-invokes start() on a retryable init failure WITHOUT an
        intervening stop(); start() must tear down the prior subprocess + drain tasks
        instead of orphaning them. Guards the subprocess-leak-on-retry bug."""
        agent = DelegateSdkAgent(_make_config())
        # Simulate leftovers from a prior (failed) attempt: a live host + 2 drain tasks.
        stale_proc = _FakeProcess([])
        agent._process = stale_proc  # type: ignore[assignment]

        async def _idle() -> None:
            await asyncio.sleep(3600)

        stale_out = asyncio.create_task(_idle())
        stale_err = asyncio.create_task(_idle())
        agent._stdout_task = stale_out
        agent._stderr_task = stale_err
        assert stale_proc.returncode is None

        fresh = _patch_host_spawn(monkeypatch, [{"type": "init_ok"}], tmp_path)
        try:
            await agent.start(str(tmp_path))
            # Prior host was terminated (wait/kill set returncode), not orphaned.
            assert stale_proc.returncode is not None
            # Prior drain tasks were cancelled, not leaked.
            assert stale_out.done() and stale_err.done()
            # The agent now owns the fresh host.
            assert agent._process is fresh
        finally:
            await agent.stop()

    def test_get_environment_info_records_routing_host_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_environment_info surfaces env/model and the BACKEND_URL *host* only
        (never the full URL — no embedded-credential leak)."""
        monkeypatch.setenv("DELEGATE_SDK_ENV", "staging")
        monkeypatch.setenv("BACKEND_URL", "https://user:pw@backend.example.com:5002/x")
        monkeypatch.delenv("INTEROP_URL", raising=False)

        info = DelegateSdkAgent(_make_config(model="virtuoso-1-5")).get_environment_info()

        assert info["delegate_env"] == "staging"
        assert info["delegate_model"] == "virtuoso-1-5"
        assert info["delegate_backend_url_host"] == "backend.example.com"
        # No full URL / credentials anywhere in the recorded values.
        assert all("://" not in str(v) and "pw@" not in str(v) for v in info.values())
        assert "delegate_interop_url_host" not in info  # unset → omitted

    def test_get_environment_info_defaults_to_alpha(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With nothing configured, only the default env slug (+ the base run marker) is recorded."""
        for var in ("DELEGATE_SDK_ENV", "BACKEND_URL", "INTEROP_URL"):
            monkeypatch.delenv(var, raising=False)

        info = DelegateSdkAgent(_make_config()).get_environment_info()

        assert info == {"delegate_env": "alpha", "system_prompt_semantics": "unknown"}


class _ScriptThenBlockReader:
    """Reader that yields a scripted set of host lines, then blocks forever.

    Models a host that completes its init handshake (and optionally the start of
    a turn) but then goes silent on the backend round-trip — the first-response
    stall the recovery path is meant to catch."""

    def __init__(self, lines: list[dict[str, Any]]) -> None:
        self._queue = [(json.dumps(obj) + "\n").encode("utf-8") for obj in lines]
        self._blocked = asyncio.Event()

    async def readline(self) -> bytes:
        if self._queue:
            return self._queue.pop(0)
        await self._blocked.wait()  # pragma: no cover - cancelled at respawn/teardown
        return b""


def _patch_host_spawn_sequence(
    monkeypatch: pytest.MonkeyPatch, procs: list[_FakeProcess], tmp_path: Path
) -> list[_FakeProcess]:
    """Like :func:`_patch_host_spawn` but yields ``procs`` in order across spawns.

    Lets a single test exercise the real start() → stall → _respawn_host() →
    _spawn_and_init() path: the first spawn is the wedged host, the second is the
    recovered host — only the Node boundary is faked.
    """
    bundle = tmp_path / "delegate_stdio.mjs"
    bundle.write_text("// stub", encoding="utf-8")
    monkeypatch.setattr("coder_eval.agents.delegate_sdk_agent._resolve_stdio_bundle", lambda: bundle)
    spawned = iter(procs)

    async def _fake_exec(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        return next(spawned)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return procs


class TestResolveStallTimeout:
    def test_unset_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DELEGATE_STALL_TIMEOUT_S", raising=False)
        assert _resolve_stall_timeout() is None

    def test_positive_value_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DELEGATE_STALL_TIMEOUT_S", "45")
        assert _resolve_stall_timeout() == 45.0

    def test_non_numeric_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DELEGATE_STALL_TIMEOUT_S", "soon")
        assert _resolve_stall_timeout() is None

    def test_non_positive_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DELEGATE_STALL_TIMEOUT_S", "0")
        assert _resolve_stall_timeout() is None


# ---- DELEGATE_STDIO_VERBOSE -------------------------------------------------


@pytest.fixture
def app_log_level() -> Iterator[Callable[[int], None]]:
    """Set coder_eval's app-logger level for one test, then restore it.

    ``setLevel`` (not a ``monkeypatch.setattr`` on ``.level``) because
    ``isEnabledFor`` memoises its answer — only ``setLevel`` clears that cache.
    The agent module's logger is a child of the app logger, so this is what
    ``--verbose`` looks like to ``_resolve_stdio_verbose``.
    """
    app = logging.getLogger("coder_eval")
    app_level = app.level
    yield app.setLevel
    app.setLevel(app_level)


class TestResolveStdioVerbose:
    """The operator switch wins both ways; unset follows coder_eval's verbosity."""

    def test_explicit_truthy_enables_on_a_quiet_run(
        self, monkeypatch: pytest.MonkeyPatch, app_log_level: Callable[[int], None]
    ) -> None:
        app_log_level(logging.INFO)
        monkeypatch.setenv("DELEGATE_STDIO_VERBOSE", "1")
        assert _resolve_stdio_verbose() is True

    def test_explicit_truthy_accepts_true_case_insensitively(
        self, monkeypatch: pytest.MonkeyPatch, app_log_level: Callable[[int], None]
    ) -> None:
        app_log_level(logging.INFO)
        monkeypatch.setenv("DELEGATE_STDIO_VERBOSE", " TRUE ")
        assert _resolve_stdio_verbose() is True

    def test_explicit_falsy_overrides_verbose(
        self, monkeypatch: pytest.MonkeyPatch, app_log_level: Callable[[int], None]
    ) -> None:
        app_log_level(logging.DEBUG)
        monkeypatch.setenv("DELEGATE_STDIO_VERBOSE", "0")
        assert _resolve_stdio_verbose() is False

    def test_unset_follows_app_logger_verbosity(
        self, monkeypatch: pytest.MonkeyPatch, app_log_level: Callable[[int], None]
    ) -> None:
        monkeypatch.delenv("DELEGATE_STDIO_VERBOSE", raising=False)
        app_log_level(logging.DEBUG)
        assert _resolve_stdio_verbose() is True
        app_log_level(logging.INFO)
        assert _resolve_stdio_verbose() is False

    def test_unrecognised_value_warns_and_falls_back(
        self,
        monkeypatch: pytest.MonkeyPatch,
        app_log_level: Callable[[int], None],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        app_log_level(logging.INFO)
        monkeypatch.setenv("DELEGATE_STDIO_VERBOSE", "yes-please")
        with caplog.at_level(logging.WARNING, logger="coder_eval.agents.delegate_sdk_agent"):
            assert _resolve_stdio_verbose() is False
        assert "DELEGATE_STDIO_VERBOSE" in caplog.text


class TestStdioVerboseHostEnv:
    """What start() hands the host: the literal its own gate parses, or nothing."""

    @pytest.mark.asyncio
    async def test_enabled_forwards_normalised_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, app_log_level: Callable[[int], None]
    ) -> None:
        """A truthy-but-not-'1' operator value reaches the host as '1'."""
        app_log_level(logging.INFO)
        monkeypatch.setenv("DELEGATE_STDIO_VERBOSE", "true")
        _patch_host_spawn(monkeypatch, [{"type": "init_ok"}], tmp_path)
        agent = DelegateSdkAgent(_make_config())
        try:
            await agent.start(str(tmp_path))
            assert agent._host_env is not None
            assert agent._host_env["DELEGATE_STDIO_VERBOSE"] == "1"
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_explicit_opt_out_erases_inherited_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, app_log_level: Callable[[int], None]
    ) -> None:
        """'0' must not reach the host — its gate would read it as off anyway, but
        an inherited truthy value has to be erased for the opt-out to hold."""
        app_log_level(logging.DEBUG)
        monkeypatch.setenv("DELEGATE_STDIO_VERBOSE", "0")
        _patch_host_spawn(monkeypatch, [{"type": "init_ok"}], tmp_path)
        agent = DelegateSdkAgent(_make_config())
        try:
            await agent.start(str(tmp_path))
            assert agent._host_env is not None
            assert "DELEGATE_STDIO_VERBOSE" not in agent._host_env
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_enabling_on_a_quiet_run_forwards_host_stderr_at_info(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, app_log_level: Callable[[int], None]
    ) -> None:
        """The point of the switch: the host's stderr trace reaches the run log.

        On a quiet (INFO) run a DEBUG record is dropped by the app's handlers no
        matter what the host emits, so forcing the switch on must raise the
        level the forwarded lines are logged at.
        """
        app_log_level(logging.INFO)
        monkeypatch.setenv("DELEGATE_STDIO_VERBOSE", "1")
        _patch_host_spawn(monkeypatch, [{"type": "init_ok"}], tmp_path)
        agent = DelegateSdkAgent(_make_config())
        try:
            await agent.start(str(tmp_path))
            assert agent._host_stderr_log_level == logging.INFO
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_verbose_run_keeps_host_stderr_at_debug(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, app_log_level: Callable[[int], None]
    ) -> None:
        """Under --verbose DEBUG records already flow, so the trace stays at DEBUG."""
        app_log_level(logging.DEBUG)
        monkeypatch.setenv("DELEGATE_STDIO_VERBOSE", "1")
        _patch_host_spawn(monkeypatch, [{"type": "init_ok"}], tmp_path)
        agent = DelegateSdkAgent(_make_config())
        try:
            await agent.start(str(tmp_path))
            assert agent._host_stderr_log_level == logging.DEBUG
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_drain_stderr_logs_at_the_configured_level(self, caplog: pytest.LogCaptureFixture) -> None:
        """_drain_stderr honours the level start() picked, so INFO lines actually land."""
        agent = DelegateSdkAgent(_make_config())
        agent._host_stderr_log_level = logging.INFO
        reader = asyncio.StreamReader()
        reader.feed_data(b"host trace line\n")
        reader.feed_eof()
        with caplog.at_level(logging.INFO, logger="coder_eval.agents.delegate_sdk_agent"):
            await agent._drain_stderr(reader)
        forwarded = [r for r in caplog.records if "host trace line" in r.getMessage()]
        assert forwarded and forwarded[0].levelno == logging.INFO
        assert "host trace line" in agent._stderr_lines


class TestFirstActivityReadDeadline:
    """The stall-cap decision function that gates respawn+resend."""

    def _agent(self, stall: float | None) -> DelegateSdkAgent:
        agent = DelegateSdkAgent(_make_config())
        agent._stall_timeout = stall
        return agent

    def test_disabled_returns_turn_deadline_unchanged(self) -> None:
        deadline, capped = self._agent(None)._first_activity_read_deadline(
            123.0, first_activity_seen=False, resends_left=1
        )
        assert (deadline, capped) == (123.0, False)

    def test_caps_wait_while_awaiting_first_activity(self) -> None:
        deadline, capped = self._agent(30.0)._first_activity_read_deadline(
            None, first_activity_seen=False, resends_left=1
        )
        assert capped is True and deadline is not None

    def test_not_capped_once_activity_seen(self) -> None:
        _, capped = self._agent(30.0)._first_activity_read_deadline(None, first_activity_seen=True, resends_left=1)
        assert capped is False

    def test_not_capped_when_resend_budget_spent(self) -> None:
        _, capped = self._agent(30.0)._first_activity_read_deadline(None, first_activity_seen=False, resends_left=0)
        assert capped is False

    def test_tighter_turn_deadline_wins(self) -> None:
        # A turn deadline already sooner than now+stall owns the timeout (a real
        # turn timeout, not a stall) so stall_capped stays False.
        _, capped = self._agent(30.0)._first_activity_read_deadline(
            time.monotonic() + 1.0, first_activity_seen=False, resends_left=1
        )
        assert capped is False


class TestStallRecovery:
    @pytest.mark.asyncio
    async def test_first_response_stall_respawns_and_resends(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A silent first round-trip triggers respawn + resend, and the fresh host's
        turn completes — a transient stall recovers instead of timing out."""
        monkeypatch.setenv("DELEGATE_STALL_TIMEOUT_S", "0.05")
        # First host: init_ok, then silence on the send (stall).
        wedged = _FakeProcess([])
        wedged.stdout = _ScriptThenBlockReader([{"type": "init_ok"}])  # type: ignore[assignment]
        # Second host (respawn): init_ok, then a clean turn result.
        recovered = _FakeProcess(
            [
                {"type": "init_ok"},
                {"type": "event", "event": {"type": "message", "content": "recovered"}},
                {"type": "result", "response": "recovered", "sessionId": "sess-r"},
            ]
        )
        _patch_host_spawn_sequence(monkeypatch, [wedged, recovered], tmp_path)

        agent = DelegateSdkAgent(_make_config())
        try:
            await agent.start(str(tmp_path))
            record = await agent.communicate("do the thing", timeout=None)

            # The turn completed from the recovered host, not a crash/timeout.
            assert record.agent_output == "recovered"
            assert record.crashed is False
            # The wedged host was killed and the agent now owns the recovered one.
            assert wedged.returncode is not None
            assert agent._process is recovered
            # The prompt was resent to the fresh host (init + send on its stdin).
            resent = [json.loads(line) for line in recovered.stdin.lines]
            assert [m["cmd"] for m in resent] == ["init", "send"]
            assert resent[1]["prompt"] == "do the thing"
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_knob_unset_blocking_first_read_times_out_without_respawn(self, tmp_path: Path) -> None:
        """With the knob off (default), a silent host rides the turn timeout to a
        TurnTimeoutError — the exact pre-feature behavior the RPA eval relies on."""
        agent = DelegateSdkAgent(_make_config())
        # Manual wiring (not _install_fake_process): the drain must run on the
        # never-returning reader from the start, so no EOF sentinel is ever
        # queued — otherwise the first read would surface as a crash, not a stall.
        proc = _FakeProcess([])
        proc.stdout = _NeverStreamReader()  # type: ignore[assignment]
        agent._process = proc  # type: ignore[assignment]
        agent.working_directory = tmp_path
        agent._stdout_task = asyncio.create_task(agent._drain_stdout(proc.stdout))  # type: ignore[arg-type]
        agent._stderr_task = asyncio.create_task(agent._drain_stderr(proc.stderr))  # type: ignore[arg-type]

        assert agent._stall_timeout is None  # start() never ran; detection stays off
        try:
            with pytest.raises(TurnTimeoutError):
                await agent.communicate("hello", timeout=0.1)
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_respawn_preserves_session_id(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """_respawn_host keeps the delegate sessionId so the resent prompt can resume
        the conversation on a backend that persists sessions."""
        agent = DelegateSdkAgent(_make_config())
        agent._session_id = "sess-keep"
        proc = _install_fake_process(agent, [], tmp_path)

        async def _fake_spawn_and_init() -> None:
            agent._process = _FakeProcess([])

        monkeypatch.setattr(agent, "_spawn_and_init", _fake_spawn_and_init)
        await agent._respawn_host()

        assert agent._session_id == "sess-keep"
        assert proc.returncode is not None  # old host was killed


class TestSessionConflictRecovery:
    @pytest.mark.asyncio
    async def test_conflict_retry_respawns_fresh_host_and_conversation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The full build-12685191 recovery: a first-turn 409 session conflict kills
        the wedged host, and the executor's AGENT_CRASH retry respawns a fresh one
        whose send opens a fresh conversation (sessionId null) — instead of failing
        fast or resuming the wedged conversation."""
        wedged = _FakeProcess(
            [
                {"type": "init_ok"},
                {
                    "type": "error",
                    "message": (
                        "Delegate backend error: Agentic Loop failed with HTTP status 409, "
                        "Error message: <A reply is already being generated for this conversation.>"
                    ),
                },
            ]
        )
        recovered = _FakeProcess(
            [
                {"type": "init_ok"},
                {"type": "event", "event": {"type": "message", "content": "recovered"}},
                {"type": "result", "response": "recovered", "sessionId": "sess-fresh"},
            ]
        )
        _patch_host_spawn_sequence(monkeypatch, [wedged, recovered], tmp_path)

        agent = DelegateSdkAgent(_make_config())
        try:
            await agent.start(str(tmp_path))
            with pytest.raises(AgentCrashError, match="HTTP status 409"):
                await agent.communicate("do the thing")
            await agent.discard_pending_turn()  # the orchestrator's failure hook
            assert wedged.returncode is not None  # wedged host was killed at crash time

            # The retry (communicate only — start() is never re-run) must succeed
            # on a respawned host with a fresh conversation.
            record = await agent.communicate("do the thing")

            assert record.crashed is False
            assert record.agent_output == "recovered"
            assert agent._process is recovered
            assert agent._respawn_before_retry is False  # consumed by the respawn
            sent = [json.loads(line) for line in recovered.stdin.lines]
            assert [m["cmd"] for m in sent] == ["init", "send"]
            assert sent[1]["sessionId"] is None  # fresh conversation, not a resume
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_non_conflict_dead_host_retry_still_fails_fast(self, tmp_path: Path) -> None:
        """Without the conflict flag, a dead host keeps the fail-fast contract —
        the respawn path is reserved for the session-conflict fingerprint."""
        agent = DelegateSdkAgent(_make_config())
        _install_fake_process(agent, [], tmp_path)  # immediate EOF → mid-turn crash

        with pytest.raises(AgentCrashError, match="host crashed"):
            await agent.communicate("first attempt")
        await agent.discard_pending_turn()

        with pytest.raises(AgentCrashError, match="not running"):
            await agent.communicate("retry")
