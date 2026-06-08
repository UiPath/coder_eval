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

import pytest

from coder_eval.models import AgentKind, EvaluationResult, FinalStatus, TaskResult
from coder_eval.orchestration.batch import _override_uip_versions_from_tasks
from coder_eval.orchestrator import Orchestrator
from coder_eval.utils import (
    _cli_version_from_manifest,
    _uip_version,
    looks_like_version,
    runtime_uip_versions,
)


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


def _make_fake_uip_lines(bin_dir: Path, lines: list[str]) -> None:
    """Drop a ``uip`` stub whose ``--version`` prints ``lines`` verbatim, in order.

    Models newer CLI builds that emit an auto-update/sync envelope (or any
    non-version chatter) around the version line.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        body = "@echo off\n" + "".join(f"echo {ln}\n" for ln in lines)
        (bin_dir / "uip.bat").write_text(body, encoding="utf-8")
        return
    uip = bin_dir / "uip"
    body = "#!/bin/sh\n" + "".join(f"echo '{ln}'\n" for ln in lines)
    uip.write_text(body, encoding="utf-8")
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
    version_info = {"cli_version": "1.2.0-alpha.host", "tool_plugins": {"maestro-tool": "host-1"}}
    tasks = [
        _make_result({"cli_version": "1.2.0-alpha.1", "tool_plugins": {"maestro-tool": "m-1"}}),
        _make_result({"cli_version": "1.2.0-alpha.1", "tool_plugins": {"maestro-tool": "m-1"}}),
    ]

    _override_uip_versions_from_tasks(version_info, tasks)

    assert version_info["cli_version"] == "1.2.0-alpha.1"
    assert version_info["tool_plugins"] == {"maestro-tool": "m-1"}


def test_override_records_drift_as_joined_versions():
    """A mid-run alpha publish shows up as a joined, sorted version set."""
    version_info = {"cli_version": "1.2.0-alpha.host", "tool_plugins": {}}
    tasks = [
        _make_result({"cli_version": "1.2.0-alpha.1", "tool_plugins": {"maestro-tool": "m-2"}}),
        _make_result({"cli_version": "1.196.0-alpha.2", "tool_plugins": {"maestro-tool": "m-1"}}),
    ]

    _override_uip_versions_from_tasks(version_info, tasks)

    # sorted() is lexicographic: "1.196..." sorts before "1.2..." (char '1' < '2').
    assert version_info["cli_version"] == "1.196.0-alpha.2 | 1.2.0-alpha.1"
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
    version_info = {"cli_version": "1.2.0-alpha.host"}
    tasks = [_make_result({"cli_version": "unknown"}), _make_result({"cli_version": "1.2.0-alpha.1"})]

    _override_uip_versions_from_tasks(version_info, tasks)

    assert version_info["cli_version"] == "1.2.0-alpha.1"


def test_override_filters_nonversion_junk_cli():
    """A non-version cli_version (newer CLI's JSON envelope) is filtered from the join.

    Belt to the _uip_version chokepoint: even if junk is already on disk
    (older runs, re-summarised on --resume), it must not reach the chip.
    """
    version_info = {"cli_version": "1.2.0-alpha.host"}
    tasks = [
        _make_result({"cli_version": '{"Result": "Success"}'}),
        _make_result({"cli_version": "[]"}),
        _make_result({"cli_version": "1.2.0-alpha.1"}),
    ]

    _override_uip_versions_from_tasks(version_info, tasks)

    assert version_info["cli_version"] == "1.2.0-alpha.1"


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


# ---------- version-string validation (junk-envelope guard) ----------


@pytest.mark.parametrize(
    "value",
    [
        "1.196.0-alpha.20260605.7426",
        "1.2.0-alpha.20260604.7394",
        "1.1.0-alpha.20260519.7220",
        "1.196.0",
        "v1.2.0",
        "  1.2.0  ",  # surrounding whitespace tolerated
    ],
)
def test_looks_like_version_accepts_real_versions(value):
    assert looks_like_version(value)


@pytest.mark.parametrize(
    "value",
    [
        '{"Result": "Success"}',  # newer CLI's JSON envelope
        "[]",
        "unknown",
        "",
        "1.2",  # missing patch
        "not a version",
        None,
        [],
        {"Result": "Success"},
    ],
)
def test_looks_like_version_rejects_junk(value):
    assert not looks_like_version(value)


def test_uip_version_returns_unknown_for_json_envelope(tmp_path: Path):
    """A CLI that prints a JSON envelope instead of a version yields 'unknown', not the envelope."""
    _make_fake_uip_lines(tmp_path / "bin", ['{"Result": "Success"}'])

    assert _uip_version(str(tmp_path / "bin")) == "unknown"


def test_uip_version_extracts_version_after_sync_envelope(tmp_path: Path):
    """When the CLI prints sync chatter before the version, the version line still wins."""
    _make_fake_uip_lines(
        tmp_path / "bin",
        ['{"Result": "Success", "Code": "VersionSync"}', "1.196.0-alpha.20260605.7426"],
    )

    assert _uip_version(str(tmp_path / "bin")) == "1.196.0-alpha.20260605.7426"


# ---------- cli_version read from @uipath/cli/package.json (manifest-first) ----------


def test_cli_version_from_manifest_reads_cli_package_json(tmp_path: Path):
    """The shell version is read from @uipath/cli/package.json, like tool_plugins."""
    tools_dir = tmp_path / "node_modules" / "@uipath"
    _make_tool_plugin(tools_dir, "cli", name="@uipath/cli", version="1.2.0-alpha.20260604.7394")

    assert _cli_version_from_manifest(tools_dir) == "1.2.0-alpha.20260604.7394"


def test_cli_version_from_manifest_none_when_absent_or_junk(tmp_path: Path):
    tools_dir = tmp_path / "node_modules" / "@uipath"
    assert _cli_version_from_manifest(None) is None
    assert _cli_version_from_manifest(tools_dir) is None  # dir has no cli/package.json
    _make_tool_plugin(tools_dir, "cli", name="@uipath/cli", version="not-a-version")
    assert _cli_version_from_manifest(tools_dir) is None  # non-version => fall back


def test_runtime_uip_versions_prefers_manifest_over_stdout(tmp_path: Path):
    """When the manifest is present, it wins over `uip --version` stdout."""
    tools_dir = tmp_path / "node_modules" / "@uipath"
    _make_tool_plugin(tools_dir, "cli", name="@uipath/cli", version="1.196.0-alpha.20260605.7426")
    _make_tool_plugin(tools_dir, "maestro-tool", name="@uipath/maestro-tool", version="1.196.0-alpha.20260605.7426")
    # A divergent stdout version proves the manifest path is taken, not stdout.
    _make_fake_uip(tmp_path / "bin", "9.9.9-stale-stdout")

    out = runtime_uip_versions(tools_dir, str(tmp_path / "bin"))

    assert out["cli_version"] == "1.196.0-alpha.20260605.7426"
    assert out["tool_plugins"] == {"maestro-tool": "1.196.0-alpha.20260605.7426"}


def test_runtime_uip_versions_falls_back_to_stdout_without_manifest(tmp_path: Path):
    """No @uipath/cli manifest under the dir => validated `uip --version` stdout is used."""
    tools_dir = tmp_path / "node_modules" / "@uipath"
    tools_dir.mkdir(parents=True)  # exists, but no cli/package.json
    _make_fake_uip(tmp_path / "bin", "1.2.0-alpha.20260604.7394")

    out = runtime_uip_versions(tools_dir, str(tmp_path / "bin"))

    assert out["cli_version"] == "1.2.0-alpha.20260604.7394"
