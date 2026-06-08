"""Tests for Claude Code plugin processing."""

from pathlib import Path

from coder_eval.utils import process_plugins


class TestProcessPlugins:
    """Test suite for process_plugins."""

    def test_empty_plugins_list(self):
        """Empty plugins list should return empty list."""
        result = process_plugins([])
        assert result == []

    def test_plugin_without_path(self):
        """Plugin without 'path' key should pass through unchanged."""
        plugins = [{"type": "local"}]
        result = process_plugins(plugins)
        assert result == plugins
        assert result[0].get("type") == "local"

    def test_simple_path_no_env_vars(self):
        """Path without env vars should pass through unchanged."""
        plugins = [{"type": "local", "path": "/absolute/path/to/plugin"}]
        result = process_plugins(plugins)
        assert result[0]["path"] == str(Path("/absolute/path/to/plugin").resolve())

    def test_expandvars_single_dollar_syntax(self, monkeypatch):
        """Expand $VAR syntax in plugin paths."""
        monkeypatch.setenv("UIPATH_PLUGIN_MARKETPLACE_DIR", "/home/user/plugins")
        plugins = [{"type": "local", "path": "$UIPATH_PLUGIN_MARKETPLACE_DIR/mcp"}]
        result = process_plugins(plugins)
        assert result[0]["path"] == str(Path("/home/user/plugins/mcp").resolve())

    def test_expandvars_braced_syntax(self, monkeypatch):
        """Expand ${VAR} syntax in plugin paths."""
        monkeypatch.setenv("UIPATH_PLUGIN_MARKETPLACE_DIR", "/home/user/plugins")
        plugins = [{"type": "local", "path": "${UIPATH_PLUGIN_MARKETPLACE_DIR}/mcp"}]
        result = process_plugins(plugins)
        assert result[0]["path"] == str(Path("/home/user/plugins/mcp").resolve())

    def test_unset_env_var_unchanged(self, monkeypatch):
        """Path unchanged when env var is not set (logged as warning)."""
        monkeypatch.delenv("UNDEFINED_VAR", raising=False)
        plugins = [{"type": "local", "path": "$UNDEFINED_VAR/plugin"}]
        result = process_plugins(plugins)
        # Path remains unchanged with the env var literal (os.path.expandvars behavior)
        assert "$UNDEFINED_VAR" in result[0]["path"]

    def test_multiple_plugins_with_different_vars(self, monkeypatch):
        """Process multiple plugins with different env vars."""
        monkeypatch.setenv("MARKETPLACE_DIR", "/marketplace")
        monkeypatch.setenv("PLUGIN_HOME", "/plugins")
        plugins = [
            {"type": "local", "path": "$MARKETPLACE_DIR/plugin1"},
            {"type": "local", "path": "$PLUGIN_HOME/plugin2"},
        ]
        result = process_plugins(plugins)
        assert result[0]["path"] == str(Path("/marketplace/plugin1").resolve())
        assert result[1]["path"] == str(Path("/plugins/plugin2").resolve())

    def test_original_dict_not_modified(self, monkeypatch):
        """Original plugin dict should not be modified."""
        monkeypatch.setenv("PLUGIN_DIR", "/plugins")
        original_plugins = [{"type": "local", "path": "$PLUGIN_DIR/mcp"}]
        original_path = original_plugins[0]["path"]
        process_plugins(original_plugins)
        # Original should be unchanged
        assert original_plugins[0]["path"] == original_path

    def test_multiple_env_vars_in_single_path(self, monkeypatch):
        """Path with multiple env vars should expand all."""
        monkeypatch.setenv("BASE", "/base")
        monkeypatch.setenv("SUBDIR", "plugins")
        plugins = [{"type": "local", "path": "$BASE/$SUBDIR/mcp"}]
        result = process_plugins(plugins)
        assert result[0]["path"] == str(Path("/base/plugins/mcp").resolve())

    def test_preserves_other_dict_fields(self, monkeypatch):
        """Process plugins should preserve all dict fields."""
        monkeypatch.setenv("DIR", "/dir")
        plugins = [
            {
                "type": "local",
                "path": "$DIR/plugin",
                "enabled": True,
                "custom_field": "value",
            }
        ]
        result = process_plugins(plugins)
        assert result[0]["type"] == "local"
        assert result[0]["enabled"] is True
        assert result[0]["custom_field"] == "value"
        assert result[0]["path"] == str(Path("/dir/plugin").resolve())

    def test_braced_syntax_not_set(self, monkeypatch):
        """Braced syntax ${VAR} also warns when not set."""
        monkeypatch.delenv("UNDEFINED_VAR", raising=False)
        plugins = [{"type": "local", "path": "${UNDEFINED_VAR}/plugin"}]
        result = process_plugins(plugins)
        # Path unchanged (braced var not expanded when not set)
        assert "${UNDEFINED_VAR}" in result[0]["path"]
