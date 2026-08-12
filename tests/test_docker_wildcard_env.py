"""Tests for Docker env_passthrough_extra support.

Tests the feature that allows extending the default env_passthrough allowlist
via the env_passthrough_extra field without having to copy the entire default.
"""

from __future__ import annotations

import sys

import pytest

from coder_eval.models import DockerDriverConfig


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="docker driver is POSIX-only")


class TestEnvPassthroughExtra:
    """Tests for env_passthrough_extra field."""

    def test_env_passthrough_extra_empty_by_default(self):
        """Verify that env_passthrough_extra defaults to empty list."""
        cfg = DockerDriverConfig()
        assert cfg.env_passthrough_extra == []

    def test_env_passthrough_extra_merges_with_defaults(self):
        """Verify that env_passthrough_extra can extend the default allowlist."""
        cfg = DockerDriverConfig(env_passthrough_extra=["MY_CUSTOM_TOKEN", "DEBUG_MODE"])
        # Defaults should still be there
        assert "ANTHROPIC_API_KEY" in cfg.env_passthrough
        # Extras are stored for merging at runtime
        assert cfg.env_passthrough_extra == ["MY_CUSTOM_TOKEN", "DEBUG_MODE"]

    def test_env_passthrough_extra_with_custom_allowlist(self):
        """Verify that env_passthrough_extra works with custom allowlist."""
        cfg = DockerDriverConfig(env_passthrough=["VAR1", "VAR2"], env_passthrough_extra=["VAR3", "VAR4"])
        assert "VAR1" in cfg.env_passthrough
        assert "VAR2" in cfg.env_passthrough
        assert cfg.env_passthrough_extra == ["VAR3", "VAR4"]

    def test_env_passthrough_extra_list_persists(self):
        """Verify that env_passthrough_extra list is preserved as-is."""
        extras = ["TOKEN_A", "TOKEN_B", "TOKEN_C"]
        cfg = DockerDriverConfig(env_passthrough_extra=extras)
        assert cfg.env_passthrough_extra == extras

    def test_default_allowlist_forwards_codex_auth_and_routing(self):
        """All CODEX_* routing vars forward by default - the Azure-routed endpoint
        needs CODEX_API_VERSION or the in-container codex binary fails routing."""
        cfg = DockerDriverConfig()
        for var in ("CODEX_API_KEY", "CODEX_BASE_URL", "CODEX_MODEL", "CODEX_API_VERSION"):
            assert var in cfg.env_passthrough
