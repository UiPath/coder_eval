"""Unit tests for the standalone proxy CLI command."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer

from coder_eval.cli.proxy_command import _run_proxy


@pytest.fixture
def _mock_env(monkeypatch):
    """Set up minimal valid env vars for proxy command."""
    monkeypatch.setenv("LLMGW_URL", "https://gw.example.com")
    monkeypatch.setenv("LLMGW_CLIENT_ID", "test-id")
    monkeypatch.setenv("LLMGW_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("LLMGW_SEMANTIC_ORG_ID", "org-1")
    monkeypatch.setenv("LLMGW_SEMANTIC_TENANT_ID", "tenant-1")


def _noop_load_dotenv(*args, **kwargs):
    """No-op replacement for load_dotenv to avoid touching the real .env file."""


def _make_mock_proxy(port: int = 9999) -> MagicMock:
    """Create a mock LLMGatewayProxy."""
    mock = MagicMock()
    mock.start = AsyncMock(return_value=port)
    mock.stop = AsyncMock()
    mock.usage = MagicMock(requests=0)
    return mock


class TestProxyCommandValidation:
    """Tests for env validation and error paths in _run_proxy."""

    async def test_missing_all_env_vars_exits(self, monkeypatch):
        """Missing required env vars should raise typer.Exit(1)."""
        for key in [
            "LLMGW_URL",
            "LLMGW_CLIENT_ID",
            "LLMGW_CLIENT_SECRET",
            "LLMGW_SEMANTIC_ORG_ID",
            "LLMGW_SEMANTIC_TENANT_ID",
        ]:
            monkeypatch.delenv(key, raising=False)

        with patch("dotenv.load_dotenv", _noop_load_dotenv):
            with pytest.raises(typer.Exit) as exc_info:
                await _run_proxy(port=0, env_file=".env", vendor="awsbedrock", api_flavor="invoke", quiet=False)
            assert exc_info.value.exit_code == 1

    async def test_partial_missing_env_vars_reports_missing(self, monkeypatch, capsys):
        """When some vars are set, error should list only the missing ones."""
        monkeypatch.setenv("LLMGW_URL", "https://gw.example.com")
        monkeypatch.setenv("LLMGW_CLIENT_ID", "test-id")
        for key in ["LLMGW_CLIENT_SECRET", "LLMGW_SEMANTIC_ORG_ID", "LLMGW_SEMANTIC_TENANT_ID"]:
            monkeypatch.delenv(key, raising=False)

        with patch("dotenv.load_dotenv", _noop_load_dotenv), pytest.raises(typer.Exit):
            await _run_proxy(port=0, env_file=".env", vendor="awsbedrock", api_flavor="invoke", quiet=False)

        captured = capsys.readouterr()
        assert "LLMGW_CLIENT_SECRET" in captured.err
        assert "LLMGW_SEMANTIC_ORG_ID" in captured.err
        assert "LLMGW_SEMANTIC_TENANT_ID" in captured.err

    async def test_invalid_timeout_exits(self, monkeypatch, _mock_env):
        """Non-integer LLMGW_TIMEOUT_SECONDS should raise typer.Exit(1)."""
        monkeypatch.setenv("LLMGW_TIMEOUT_SECONDS", "not-a-number")

        with patch("dotenv.load_dotenv", _noop_load_dotenv):
            with pytest.raises(typer.Exit) as exc_info:
                await _run_proxy(port=0, env_file=".env", vendor="awsbedrock", api_flavor="invoke", quiet=False)
            assert exc_info.value.exit_code == 1

    async def test_float_timeout_exits(self, monkeypatch, _mock_env):
        """Float LLMGW_TIMEOUT_SECONDS should raise typer.Exit(1) — int() rejects it."""
        monkeypatch.setenv("LLMGW_TIMEOUT_SECONDS", "30.5")

        with patch("dotenv.load_dotenv", _noop_load_dotenv):
            with pytest.raises(typer.Exit) as exc_info:
                await _run_proxy(port=0, env_file=".env", vendor="awsbedrock", api_flavor="invoke", quiet=False)
            assert exc_info.value.exit_code == 1


class TestProxyCommandOutput:
    """Tests for quiet vs non-quiet output behavior."""

    async def test_quiet_mode_exports_to_stdout(self, monkeypatch, _mock_env, capsys):
        """Quiet mode should print only export commands to stdout."""
        mock_proxy = _make_mock_proxy(port=8080)

        with (
            patch("dotenv.load_dotenv", _noop_load_dotenv),
            patch("coder_eval.proxy.server.LLMGatewayProxy", return_value=mock_proxy),
            patch("coder_eval.proxy.config.ProxyConfig", return_value=MagicMock()),
            patch.object(asyncio.Event, "wait", new_callable=AsyncMock),
        ):
            await _run_proxy(port=0, env_file=".env", vendor="awsbedrock", api_flavor="invoke", quiet=True)

        captured = capsys.readouterr()
        assert "export ANTHROPIC_BASE_URL=http://127.0.0.1:8080" in captured.out
        assert "export ANTHROPIC_API_KEY=llmgw-proxy" in captured.out
        # No human messages on stdout
        assert "LLM Gateway Proxy running" not in captured.out

    async def test_non_quiet_mode_banner_to_stderr(self, monkeypatch, _mock_env, capsys):
        """Non-quiet mode should print banner to stderr, not stdout."""
        mock_proxy = _make_mock_proxy(port=8080)

        with (
            patch("dotenv.load_dotenv", _noop_load_dotenv),
            patch("coder_eval.proxy.server.LLMGatewayProxy", return_value=mock_proxy),
            patch("coder_eval.proxy.config.ProxyConfig", return_value=MagicMock()),
            patch.object(asyncio.Event, "wait", new_callable=AsyncMock),
        ):
            await _run_proxy(port=0, env_file=".env", vendor="awsbedrock", api_flavor="invoke", quiet=False)

        captured = capsys.readouterr()
        assert "LLM Gateway Proxy running" in captured.err
        assert "http://127.0.0.1:8080" in captured.err
        # No banner on stdout
        assert "LLM Gateway Proxy running" not in captured.out
