"""Tests for coder_eval.utils version-info helpers."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coder_eval.utils import _git_short_sha, _tool_plugin_versions, get_version_info


def test_git_short_sha_returns_unknown_when_path_missing(tmp_path: Path):
    missing = tmp_path / "does-not-exist"
    assert _git_short_sha(missing) == "unknown"


def test_git_short_sha_returns_unknown_when_git_missing(tmp_path: Path):
    """FileNotFoundError on git binary is swallowed and returns 'unknown'."""
    with patch("coder_eval.utils.subprocess.run", side_effect=FileNotFoundError):
        assert _git_short_sha(tmp_path) == "unknown"


def test_git_short_sha_passes_utf8_encoding_kwargs(tmp_path: Path):
    """_git_short_sha must call subprocess.run with encoding='utf-8' / errors='replace'.

    Mocking subprocess.run bypasses the decoder, so we assert on the kwargs
    instead of on the decoded string — that's the actual contract that
    keeps Windows / corrupted-git stdout from crashing the run.
    """
    fake_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="abc1234\n", stderr="")
    with patch("coder_eval.utils.subprocess.run", return_value=fake_result) as mock_run:
        sha = _git_short_sha(tmp_path)
        assert sha == "abc1234"
        kwargs = mock_run.call_args.kwargs
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        assert kwargs["text"] is True


def _make_tool_plugin(tools_dir: Path, dir_name: str, *, name: str, version: str) -> None:
    """Create a fake ``<tools_dir>/<dir_name>/package.json`` plugin manifest."""
    pkg_dir = tools_dir / dir_name
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.json").write_text(json.dumps({"name": name, "version": version}), encoding="utf-8")


def test_tool_plugin_versions_reads_maestro_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A ``@uipath/maestro-tool`` plugin under PLUGIN_TOOLS_DIR is captured by name + version."""
    tools_dir = tmp_path / "node_modules" / "@uipath"
    _make_tool_plugin(tools_dir, "maestro-tool", name="@uipath/maestro-tool", version="1.2.0-alpha.20260604")
    # A non-tool plugin (the CLI shell itself) must be ignored.
    _make_tool_plugin(tools_dir, "cli", name="@uipath/cli", version="1.2.0-alpha.20260604")
    monkeypatch.setenv("PLUGIN_TOOLS_DIR", str(tools_dir))

    plugins = _tool_plugin_versions()

    assert plugins == {"maestro-tool": "1.2.0-alpha.20260604"}


def test_tool_plugin_versions_captures_multiple_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """All ``*-tool`` plugins are enumerated, not just maestro-tool."""
    tools_dir = tmp_path / "node_modules" / "@uipath"
    _make_tool_plugin(tools_dir, "maestro-tool", name="@uipath/maestro-tool", version="1.2.0")
    _make_tool_plugin(tools_dir, "studio-tool", name="@uipath/studio-tool", version="3.4.5")
    monkeypatch.setenv("PLUGIN_TOOLS_DIR", str(tools_dir))

    plugins = _tool_plugin_versions()

    assert plugins == {"maestro-tool": "1.2.0", "studio-tool": "3.4.5"}


def test_tool_plugin_versions_absent_dir_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A missing PLUGIN_TOOLS_DIR yields an empty mapping rather than raising."""
    monkeypatch.setenv("PLUGIN_TOOLS_DIR", str(tmp_path / "does-not-exist" / "@uipath"))

    assert _tool_plugin_versions() == {}


def test_tool_plugin_versions_unreadable_manifest_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A malformed package.json is skipped, not fatal; valid siblings still land."""
    tools_dir = tmp_path / "node_modules" / "@uipath"
    _make_tool_plugin(tools_dir, "maestro-tool", name="@uipath/maestro-tool", version="1.2.0")
    broken = tools_dir / "broken-tool"
    broken.mkdir()
    (broken / "package.json").write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("PLUGIN_TOOLS_DIR", str(tools_dir))

    assert _tool_plugin_versions() == {"maestro-tool": "1.2.0"}


def test_get_version_info_includes_tool_plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """environment_info carries a ``tool_plugins`` block with the captured versions."""
    tools_dir = tmp_path / "node_modules" / "@uipath"
    _make_tool_plugin(tools_dir, "maestro-tool", name="@uipath/maestro-tool", version="9.9.9")
    # get_version_info() resolves the plugin-tools dir via resolve_uipath_plugin_dir()
    # (PATH-based), which ignores PLUGIN_TOOLS_DIR — so pin the resolver itself at the
    # fixture. A bare setenv leaks the host's real @uipath CLI in when one is installed.
    monkeypatch.setattr("coder_eval.utils.resolve_uipath_plugin_dir", lambda search_path=None: tools_dir)

    info = get_version_info()

    assert info["tool_plugins"] == {"maestro-tool": "9.9.9"}


def test_run_summary_round_trips_nested_tool_plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """``get_version_info()`` is wired into ``RunSummary(environment_info=...)`` at
    batch.py:439, so the nested ``tool_plugins`` dict must survive construction and
    JSON serialization intact. This fails if ``RunSummary.environment_info`` is typed
    ``dict[str, str]`` (pydantic rejects the nested dict with a ``string_type`` error).
    """
    from datetime import datetime

    from coder_eval.models.results import RunSummary

    tools_dir = tmp_path / "node_modules" / "@uipath"
    _make_tool_plugin(tools_dir, "maestro-tool", name="@uipath/maestro-tool", version="1.2.0-alpha.20260604")
    _make_tool_plugin(tools_dir, "studio-tool", name="@uipath/studio-tool", version="3.4.5")
    # get_version_info() resolves via resolve_uipath_plugin_dir() (ignores PLUGIN_TOOLS_DIR);
    # pin the resolver so the host's real @uipath CLI, if installed, can't leak in.
    monkeypatch.setattr("coder_eval.utils.resolve_uipath_plugin_dir", lambda search_path=None: tools_dir)

    info = get_version_info()
    expected_plugins = {"maestro-tool": "1.2.0-alpha.20260604", "studio-tool": "3.4.5"}
    assert info["tool_plugins"] == expected_plugins

    summary = RunSummary(
        run_id="2026-06-04_00-00-00",
        start_time=datetime(2026, 6, 4),
        end_time=datetime(2026, 6, 4),
        total_duration_seconds=0.0,
        tasks_run=0,
        tasks_succeeded=0,
        tasks_failed=0,
        tasks_error=0,
        task_results=[],
        framework_version=info.get("coder_eval", "unknown"),
        environment_info=info,
    )

    # Nested dict is preserved on the model instance...
    assert summary.environment_info["tool_plugins"] == expected_plugins
    # ...and through the run.json serialization path (model_dump_json -> parse).
    round_tripped = json.loads(summary.model_dump_json())
    assert round_tripped["environment_info"]["tool_plugins"] == expected_plugins
