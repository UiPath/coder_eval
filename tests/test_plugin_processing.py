"""Tests for Claude Code plugin processing."""

import logging
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


class TestPluginRootWarning:
    """A local plugin path must be a plugin ROOT, and claude-code says nothing when it isn't.

    `--plugin-dir <path>` resolves a skill at `<path>/skills/<name>/SKILL.md`. Aim one
    level deeper — at the bare directory of skill directories — and the SDK loads
    nothing at all, with no error. Every positive row of an activation suite then
    scores 0 and the suite reports recall 0.0, indistinguishable from a skill that
    never triggers. That shipped in six documentation surfaces before anyone noticed.

    Codex (`codex_agent._setup_skills`) and Antigravity
    (`antigravity_agent._resolve_skills_paths`) accept BOTH depths and already log
    when they link zero skills. claude-code — the one harness where the wrong depth is
    fatal — was the only one that stayed silent, and a repo-scoped lint rule cannot
    reach the user repos where `/coder-eval:check-skill` writes these suites.
    """

    def test_plugin_root_with_skills_dir_is_quiet(self, tmp_path, caplog):
        (tmp_path / "skills" / "demo").mkdir(parents=True)
        (tmp_path / "skills" / "demo" / "SKILL.md").write_text("---\n---\n", encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            process_plugins([{"type": "local", "path": str(tmp_path)}])

        assert "no skills/ subdirectory" not in caplog.text

    def test_bare_skills_dir_warns(self, tmp_path, caplog):
        # The exact shape six surfaces once prescribed: point at `.claude/skills` itself.
        skills_dir = tmp_path / "skills"
        (skills_dir / "demo").mkdir(parents=True)
        (skills_dir / "demo" / "SKILL.md").write_text("---\n---\n", encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            process_plugins([{"type": "local", "path": str(skills_dir)}])

        assert "no skills/ subdirectory" in caplog.text
        assert "PLUGIN ROOT" in caplog.text

    def test_missing_directory_does_not_warn_about_layout(self, tmp_path, caplog):
        # A path that does not exist is a different failure with its own signal; claiming
        # a layout problem about it would send the reader looking in the wrong place.
        with caplog.at_level(logging.WARNING):
            process_plugins([{"type": "local", "path": str(tmp_path / "nope")}])

        assert "no skills/ subdirectory" not in caplog.text

    def test_non_local_plugin_is_not_checked(self, tmp_path, caplog):
        # The plugin-root contract is about `type: local`; leave other source types alone.
        with caplog.at_level(logging.WARNING):
            process_plugins([{"type": "github", "path": str(tmp_path)}])

        assert "no skills/ subdirectory" not in caplog.text
