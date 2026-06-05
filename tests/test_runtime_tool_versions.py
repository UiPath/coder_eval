"""Container-truth uip version capture (coder_eval#366 follow-up).

#366 recorded ``cli_version``/``tool_plugins`` from whichever environment the
collecting process ran in — the host for the run summary, and the pre-task
state for task.json. Tasks actually run inside docker containers that
auto-install the latest alpha tool plugins on first use, so:

- the orchestrator must re-capture the versions AFTER the task ran, through
  the sandbox's agent-aligned PATH (``_refresh_runtime_tool_versions``);
- the run summary must aggregate the per-task (in-container) captures instead
  of trusting the host (``_override_uip_versions_from_tasks``).
"""

import json
import os
import stat
from datetime import datetime
from pathlib import Path

from coder_eval.models import AgentKind, EvaluationResult, FinalStatus, TaskResult
from coder_eval.orchestration.batch import _override_uip_versions_from_tasks
from coder_eval.orchestrator import Orchestrator
from coder_eval.utils import runtime_uip_versions


# ---------- helpers ----------


def _make_result(env: dict | None) -> TaskResult:
    return TaskResult(
        task_id="t",
        variant_id="v",
        duration=1.0,
        result=EvaluationResult(
            task_id="t",
            task_description="d",
            variant_id="v",
            agent_type=AgentKind.CLAUDE_CODE,
            started_at=datetime.now(),
            final_status=FinalStatus.SUCCESS,
            iteration_count=0,
            environment_info=env or {},
        ),
    )


def _make_tool_plugin(tools_dir: Path, dir_name: str, *, name: str, version: str) -> None:
    pkg_dir = tools_dir / dir_name
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.json").write_text(json.dumps({"name": name, "version": version}), encoding="utf-8")


def _make_fake_uip(bin_dir: Path, version: str) -> None:
    """Drop an executable ``uip`` stub that prints ``version``.

    Platform-aware: ``shutil.which`` on Windows resolves only PATHEXT
    extensions and CreateProcess can't exec a shebang script, so emit a
    ``uip.bat`` there and a ``#!/bin/sh`` script elsewhere.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        (bin_dir / "uip.bat").write_text(f"@echo off\necho {version}\n", encoding="utf-8")
        return
    uip = bin_dir / "uip"
    uip.write_text(f"#!/bin/sh\necho '{version}'\n", encoding="utf-8")
    uip.chmod(uip.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class _FakeSandbox:
    """Just the surface _refresh_runtime_tool_versions touches."""

    def __init__(self, plugin_tools_dir: str | None, uip_search_path: str) -> None:
        self.plugin_tools_dir = plugin_tools_dir
        self.uip_search_path = uip_search_path
        self.refreshed = False

    def refresh_plugin_tools_dir(self) -> None:
        self.refreshed = True


def _bare_orchestrator(env: dict, sandbox: _FakeSandbox | None) -> Orchestrator:
    """Orchestrator shell with only the attributes the refresh method reads."""
    orch = Orchestrator.__new__(Orchestrator)
    orch.result = _make_result(env).result
    orch.sandbox = sandbox
    return orch


# ---------- runtime_uip_versions (utils) ----------


def test_runtime_uip_versions_resolves_via_explicit_dir_and_path(tmp_path: Path):
    """Versions come from the given plugin dir + search PATH, not the process env."""
    tools_dir = tmp_path / "node_modules" / "@uipath"
    _make_tool_plugin(tools_dir, "maestro-tool", name="@uipath/maestro-tool", version="1.2.0-alpha.20260605")
    _make_fake_uip(tmp_path / "bin", "1.2.0-alpha.20260605.9999")

    out = runtime_uip_versions(tools_dir, str(tmp_path / "bin"))

    assert out == {
        "cli_version": "1.2.0-alpha.20260605.9999",
        "tool_plugins": {"maestro-tool": "1.2.0-alpha.20260605"},
    }


def test_runtime_uip_versions_missing_uip_is_unknown(tmp_path: Path):
    """An empty search PATH yields 'unknown' without falling back to the process PATH."""
    out = runtime_uip_versions(None, str(tmp_path / "empty"))

    assert out == {"cli_version": "unknown", "tool_plugins": {}}


def test_runtime_uip_versions_none_dir_does_not_discover_host_plugins(tmp_path: Path, monkeypatch):
    """A None plugin dir yields {} even when host discovery would find plugins.

    Falling back to process-env discovery here would reach into the host's
    installs on in-process runs — the exact skew this helper exists to remove.
    """
    host_tools = tmp_path / "host" / "node_modules" / "@uipath"
    _make_tool_plugin(host_tools, "maestro-tool", name="@uipath/maestro-tool", version="9.9.9-host")
    monkeypatch.setenv("PLUGIN_TOOLS_DIR", str(host_tools))

    out = runtime_uip_versions(None, str(tmp_path / "empty"))

    assert out["tool_plugins"] == {}


# ---------- Orchestrator._refresh_runtime_tool_versions ----------


def test_refresh_runtime_tool_versions_updates_env_from_sandbox(tmp_path: Path):
    """Post-task capture re-derives the plugin dir and overwrites setup-time values."""
    tools_dir = tmp_path / "node_modules" / "@uipath"
    _make_tool_plugin(tools_dir, "maestro-tool", name="@uipath/maestro-tool", version="2.0.0")
    _make_fake_uip(tmp_path / "bin", "2.0.0-shell")
    sandbox = _FakeSandbox(str(tools_dir), str(tmp_path / "bin"))
    orch = _bare_orchestrator({"cli_version": "1.0.0-baked", "tool_plugins": {"maestro-tool": "1.0.0-baked"}}, sandbox)

    orch._refresh_runtime_tool_versions()

    assert sandbox.refreshed, "must re-derive the plugin dir: auto-install can move/create it mid-task"
    assert orch.result.environment_info["cli_version"] == "2.0.0-shell"
    assert orch.result.environment_info["tool_plugins"] == {"maestro-tool": "2.0.0"}


def test_refresh_runtime_tool_versions_keeps_setup_values_on_empty_resolution(tmp_path: Path):
    """When post-task resolution finds nothing, setup-time values survive."""
    sandbox = _FakeSandbox(None, str(tmp_path / "empty"))
    orch = _bare_orchestrator({"cli_version": "1.0.0-baked", "tool_plugins": {"maestro-tool": "1.0.0-baked"}}, sandbox)

    orch._refresh_runtime_tool_versions()

    assert orch.result.environment_info["cli_version"] == "1.0.0-baked"
    assert orch.result.environment_info["tool_plugins"] == {"maestro-tool": "1.0.0-baked"}


def test_refresh_runtime_tool_versions_none_plugin_dir_keeps_setup_plugins(tmp_path: Path, monkeypatch):
    """cli updates while plugins keep setup values when the plugin dir is gone.

    The keys are independent: a resolvable shell with a lost plugin dir must
    not pull the HOST's plugins in via process-env discovery (the docstring's
    partial-update case).
    """
    _make_fake_uip(tmp_path / "bin", "2.0.0-shell")
    host_tools = tmp_path / "host" / "node_modules" / "@uipath"
    _make_tool_plugin(host_tools, "maestro-tool", name="@uipath/maestro-tool", version="9.9.9-host")
    monkeypatch.setenv("PLUGIN_TOOLS_DIR", str(host_tools))
    sandbox = _FakeSandbox(None, str(tmp_path / "bin"))
    orch = _bare_orchestrator({"cli_version": "1.0.0-baked", "tool_plugins": {"maestro-tool": "1.0.0-baked"}}, sandbox)

    orch._refresh_runtime_tool_versions()

    assert orch.result.environment_info["cli_version"] == "2.0.0-shell"
    assert orch.result.environment_info["tool_plugins"] == {"maestro-tool": "1.0.0-baked"}


def test_refresh_runtime_tool_versions_no_sandbox_is_noop():
    orch = _bare_orchestrator({"cli_version": "1.0.0-baked"}, None)

    orch._refresh_runtime_tool_versions()

    assert orch.result.environment_info["cli_version"] == "1.0.0-baked"


# ---------- batch._override_uip_versions_from_tasks ----------


def test_override_uses_container_consensus_over_host():
    version_info = {"cli_version": "host-1", "tool_plugins": {"maestro-tool": "host-1"}}
    tasks = [
        _make_result({"cli_version": "c-1", "tool_plugins": {"maestro-tool": "m-1"}}),
        _make_result({"cli_version": "c-1", "tool_plugins": {"maestro-tool": "m-1"}}),
    ]

    _override_uip_versions_from_tasks(version_info, tasks)

    assert version_info["cli_version"] == "c-1"
    assert version_info["tool_plugins"] == {"maestro-tool": "m-1"}


def test_override_records_drift_as_joined_versions():
    """A mid-run alpha publish shows up as a joined, sorted version set."""
    version_info = {"cli_version": "host-1", "tool_plugins": {}}
    tasks = [
        _make_result({"cli_version": "c-1", "tool_plugins": {"maestro-tool": "m-2"}}),
        _make_result({"cli_version": "c-2", "tool_plugins": {"maestro-tool": "m-1"}}),
    ]

    _override_uip_versions_from_tasks(version_info, tasks)

    assert version_info["cli_version"] == "c-1 | c-2"
    assert version_info["tool_plugins"] == {"maestro-tool": "m-1 | m-2"}


def test_override_keeps_host_values_when_no_task_reported():
    """Legacy/errored tasks without env info leave the host fallback in place."""
    version_info = {"cli_version": "host-1", "tool_plugins": {"maestro-tool": "host-1"}}
    tasks = [_make_result(None), _make_result({"cli_version": "unknown"})]

    _override_uip_versions_from_tasks(version_info, tasks)

    assert version_info["cli_version"] == "host-1"
    assert version_info["tool_plugins"] == {"maestro-tool": "host-1"}


def test_override_unions_plugins_across_tasks():
    """Tasks that exercised different tools each contribute their plugin."""
    version_info = {}
    tasks = [
        _make_result({"tool_plugins": {"maestro-tool": "m-1"}}),
        _make_result({"tool_plugins": {"orchestrator-tool": "o-1"}}),
    ]

    _override_uip_versions_from_tasks(version_info, tasks)

    assert version_info["tool_plugins"] == {"maestro-tool": "m-1", "orchestrator-tool": "o-1"}


def test_override_all_empty_plugin_dicts_keep_host_fallback():
    """Tasks that all report tool_plugins == {} must not stomp the host value."""
    version_info = {"cli_version": "host-1", "tool_plugins": {"maestro-tool": "host-1"}}
    tasks = [_make_result({"tool_plugins": {}}), _make_result({"tool_plugins": {}})]

    _override_uip_versions_from_tasks(version_info, tasks)

    assert version_info["tool_plugins"] == {"maestro-tool": "host-1"}


def test_override_filters_unknown_cli_among_valid():
    """'unknown' from one task neither blocks nor joins a valid consensus."""
    version_info = {"cli_version": "host-1"}
    tasks = [_make_result({"cli_version": "unknown"}), _make_result({"cli_version": "c-1"})]

    _override_uip_versions_from_tasks(version_info, tasks)

    assert version_info["cli_version"] == "c-1"


# ---------- Sandbox plumbing (real object, not the fake) ----------


def test_sandbox_refresh_plugin_tools_dir_real_plumbing(tmp_path: Path):
    """uip_search_path + refresh_plugin_tools_dir re-derive from the agent-aligned PATH."""
    from coder_eval.models import SandboxConfig
    from coder_eval.sandbox import Sandbox

    dist = tmp_path / "node_modules" / "@uipath" / "cli" / "dist"
    _make_fake_uip(dist, "9.9.9")
    sandbox = Sandbox(config=SandboxConfig(), task_id="t")

    sandbox.set_command_base_path(str(dist))

    assert sandbox.uip_search_path.startswith(f"{dist}{os.pathsep}")
    assert sandbox.plugin_tools_dir == str(tmp_path / "node_modules" / "@uipath")
