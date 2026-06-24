"""Telemetry wiring at the CLI seam: init on callback + CoderEval.Run.Start + flush.

All telemetry calls are patched — no real exporter, network, or model traffic.
"""

import contextlib
import re
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from typer.testing import CliRunner

from coder_eval.cli import app
from coder_eval.cli.run_command import _run_all_tasks


# Click/Rich wraps tokens in ANSI escapes (e.g., `\x1b[1;36m--max-parallel\x1b[0m`)
# which breaks naive substring matching against the rendered help. Strip them
# before asserting so tests are robust across CI terminal-width and color settings.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def test_callback_inits_telemetry_once():
    with patch("coder_eval.telemetry.init_telemetry") as mock_init:
        # No subcommand → callback runs then exits 0 after printing help.
        result = CliRunner().invoke(app, [])
    assert result.exit_code == 0
    mock_init.assert_called_once()
    # Called with the package version as a keyword.
    assert "version" in mock_init.call_args.kwargs


def test_wrapped_run_command_still_parses_its_options():
    # track_command wraps run_command via functools.wraps; Typer must still
    # introspect the real signature and expose its flags.
    result = CliRunner().invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    output = _strip_ansi(result.output)
    assert "--max-parallel" in output
    assert "--type" in output
    assert "--experiment" in output


def test_wrapped_proxy_command_still_parses_its_options():
    result = CliRunner().invoke(app, ["proxy", "--help"])
    assert result.exit_code == 0
    # proxy has its own options; --help must succeed (signature preserved).
    assert "Usage" in _strip_ansi(result.output)


def test_help_never_crashes_when_telemetry_enabled_and_config_unwritable(tmp_path, monkeypatch):
    # Regression: --help and `run --help` must never crash when telemetry is
    # enabled but the install-id config can't be written. init_telemetry runs in
    # the global callback for these; it must degrade (emit without InstallId or
    # off), never raise. Uses an invalid connection string so no real SDK exporter
    # thread is started — we only care that the callback path doesn't crash.
    import coder_eval.telemetry as tel
    from coder_eval.config import settings

    monkeypatch.setattr(settings, "telemetry_enabled", True)
    monkeypatch.setattr(settings, "telemetry_connection_string", "invalid-connection-string")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "coder-eval").write_text("not a dir")  # make the config dir uncreatable

    for argv in (["--help"], ["run", "--help"]):
        monkeypatch.setattr(tel, "_initialized", False)  # force real init on each invocation
        monkeypatch.setattr(tel, "_events_logger", None)
        monkeypatch.setattr(tel, "_provider", None)
        result = CliRunner().invoke(app, argv)
        assert result.exit_code == 0, f"{argv} exited {result.exit_code}: {result.output}"


async def test_run_emits_run_start_and_flushes(tmp_path):
    summary = Mock(tasks_failed=0, tasks_error=0)

    with (
        patch("coder_eval.cli.run_command.prepare_run_directory", return_value=tmp_path),
        patch("coder_eval.cli.run_command.expand_task_files", return_value=[Path("a.yaml"), Path("b.yaml")]),
        patch("coder_eval.cli.run_command._run_with_experiment", new=AsyncMock(return_value=(summary, 0))),
        patch("coder_eval.logging_config.aggregate_task_logs"),
        patch("coder_eval.cli.run_command.print_execution_summary"),
        patch("coder_eval.telemetry.track_event") as mock_track,
        patch("coder_eval.telemetry.flush_telemetry") as mock_flush,
    ):
        await _run_all_tasks(
            task_files=[Path("a.yaml"), Path("b.yaml")],
            preservation_mode=None,
            run_dir=tmp_path,
            max_parallel=2,
            agent_type="codex",
            stream_mode="full",
            resume=False,
        )

    mock_track.assert_called_once()
    name, props = mock_track.call_args.args
    assert name == "CoderEval.Run.Start"
    assert props["TaskFileCount"] == 2
    assert props["MaxParallel"] == 2
    assert props["AgentType"] == "codex"
    assert props["StreamMode"] == "full"
    assert props["Resume"] is False
    assert props["ExperimentProvided"] is False
    mock_flush.assert_called_once()


async def test_run_start_uses_default_fallbacks_for_none_inputs(tmp_path):
    # agent_type=None / stream_mode=None must surface as the "default"/"none"
    # fallback property values, not as null.
    summary = Mock(tasks_failed=0, tasks_error=0)

    with (
        patch("coder_eval.cli.run_command.prepare_run_directory", return_value=tmp_path),
        patch("coder_eval.cli.run_command.expand_task_files", return_value=[Path("a.yaml")]),
        patch("coder_eval.cli.run_command._run_with_experiment", new=AsyncMock(return_value=(summary, 0))),
        patch("coder_eval.logging_config.aggregate_task_logs"),
        patch("coder_eval.cli.run_command.print_execution_summary"),
        patch("coder_eval.telemetry.track_event") as mock_track,
        patch("coder_eval.telemetry.flush_telemetry"),
    ):
        await _run_all_tasks(
            task_files=[Path("a.yaml")],
            preservation_mode=None,
            run_dir=tmp_path,
            max_parallel=1,
            agent_type=None,
            stream_mode=None,
            resume=False,
        )

    _, props = mock_track.call_args.args
    assert props["AgentType"] == "default"
    assert props["StreamMode"] == "none"


async def test_run_flushes_even_when_experiment_raises(tmp_path):
    with (
        patch("coder_eval.cli.run_command.prepare_run_directory", return_value=tmp_path),
        patch("coder_eval.cli.run_command.expand_task_files", return_value=[]),
        patch("coder_eval.cli.run_command._run_with_experiment", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch("coder_eval.telemetry.track_event"),
        patch("coder_eval.telemetry.flush_telemetry") as mock_flush,
        contextlib.suppress(RuntimeError),
    ):
        await _run_all_tasks(
            task_files=[],
            preservation_mode=None,
            run_dir=tmp_path,
            max_parallel=1,
        )
    # flush still runs in the finally on the exception path.
    mock_flush.assert_called_once()
