"""Unit tests for the user-telemetry side-channel (coder_eval.telemetry).

All tests mock the OTel/Azure SDK — no real exporter, network, or model traffic.
"""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest
import typer

import coder_eval.telemetry as tel


@pytest.fixture(autouse=True)
def _reset_telemetry_state(monkeypatch, tmp_path):
    """Reset telemetry module globals before and after each test."""
    # Isolate the persisted install-id config file from the real ~/.config so
    # no test writes to the developer's/CI's home directory.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    def _reset() -> None:
        tel._events_logger = None
        tel._provider = None
        tel._handler = None
        tel._default_props = {}
        tel._initialized = False
        # Drop any handlers a real/mock init attached to the dedicated logger.
        logging.getLogger("coder_eval.telemetry.events").handlers.clear()

    _reset()
    yield
    _reset()


@pytest.fixture
def enabled_settings(monkeypatch):
    """Configure settings so telemetry is enabled with a connection string."""
    from coder_eval.config import settings

    monkeypatch.setattr(settings, "telemetry_enabled", True)
    monkeypatch.setattr(settings, "telemetry_connection_string", "InstrumentationKey=test;IngestionEndpoint=https://x/")
    return settings


class _MockedOtel:
    """Context manager patching the function-local OTel/Azure imports in init_telemetry."""

    def __init__(self) -> None:
        self.provider = MagicMock(name="LoggerProvider")
        self.handler = logging.NullHandler()  # a real Handler so addHandler/propagate work

    def __enter__(self):
        self._patches = [
            patch("opentelemetry.sdk._logs.LoggerProvider", return_value=self.provider),
            patch("opentelemetry.sdk._logs.LoggingHandler", return_value=self.handler),
            patch("opentelemetry.sdk._logs.export.BatchLogRecordProcessor", MagicMock()),
            patch("opentelemetry.sdk.resources.Resource", MagicMock()),
            patch("azure.monitor.opentelemetry.exporter.AzureMonitorLogExporter", MagicMock()),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


# --- Disabled / no-op paths --------------------------------------------------


def test_init_noop_when_telemetry_disabled(monkeypatch):
    from coder_eval.config import settings

    monkeypatch.setattr(settings, "telemetry_enabled", False)
    monkeypatch.setattr(settings, "telemetry_connection_string", "InstrumentationKey=test")
    tel.init_telemetry(version="1.2.3")
    assert tel._events_logger is None
    assert tel._initialized is False


def test_init_noop_when_no_connection_string(monkeypatch):
    from coder_eval.config import settings

    monkeypatch.setattr(settings, "telemetry_enabled", True)
    monkeypatch.setattr(settings, "telemetry_connection_string", None)
    tel.init_telemetry(version="1.2.3")
    assert tel._events_logger is None


def test_track_event_before_init_is_noop():
    # Must not raise even though nothing is initialized.
    tel.track_event("CoderEval.Run.Start", {"TaskFileCount": 3})
    assert tel._events_logger is None


@pytest.mark.parametrize(
    "env_var",
    ["TELEMETRY_CONNECTION_STRING", "APPLICATIONINSIGHTS_CONNECTION_STRING", "UIPATH_AI_CONNECTION_STRING"],
)
def test_connection_string_resolves_from_each_documented_env_alias(monkeypatch, env_var):
    # The three documented env vars are the entire activation contract. Prove each
    # actually populates telemetry_connection_string through pydantic-settings'
    # real env resolution (fresh Settings, no .env), so dropping or renaming an
    # AliasChoices entry can no longer silently disable telemetry with no test
    # failure. (Other tests monkeypatch the already-resolved field, which can't
    # catch a broken alias.)
    from coder_eval.config import Settings

    for v in ("TELEMETRY_CONNECTION_STRING", "APPLICATIONINSIGHTS_CONNECTION_STRING", "UIPATH_AI_CONNECTION_STRING"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv(env_var, "InstrumentationKey=from-env")

    settings = Settings(_env_file=None)
    assert settings.telemetry_connection_string == "InstrumentationKey=from-env"


def test_baked_in_default_connection_string_used_when_unset(monkeypatch):
    # With no env override, the embedded default ships telemetry to the shared
    # coder-eval resource (telemetry is on by default). The baked-in value is a
    # well-formed ingestion connection string.
    from coder_eval.config import _DEFAULT_TELEMETRY_CONNECTION_STRING, Settings

    for v in ("TELEMETRY_CONNECTION_STRING", "APPLICATIONINSIGHTS_CONNECTION_STRING", "UIPATH_AI_CONNECTION_STRING"):
        monkeypatch.delenv(v, raising=False)

    settings = Settings(_env_file=None)
    assert settings.telemetry_connection_string == _DEFAULT_TELEMETRY_CONNECTION_STRING
    assert _DEFAULT_TELEMETRY_CONNECTION_STRING.startswith("InstrumentationKey=")
    assert "IngestionEndpoint=" in _DEFAULT_TELEMETRY_CONNECTION_STRING


# --- Mocked init / event emission --------------------------------------------


def test_init_builds_provider_and_events_logger(enabled_settings):
    with _MockedOtel() as mocked:
        tel.init_telemetry(version="9.9.9")
    assert tel._initialized is True
    assert tel._provider is mocked.provider
    events_logger = logging.getLogger("coder_eval.telemetry.events")
    assert events_logger.propagate is False
    assert mocked.handler in events_logger.handlers
    assert tel._default_props["Version"] == "9.9.9"
    assert tel._default_props["Source"] == "coder-eval"
    assert tel._default_props["SchemaVersion"] == tel._TELEMETRY_SCHEMA_VERSION
    assert tel._default_props["SessionId"]


def test_track_event_emits_record_with_custom_event_attr(enabled_settings):
    with _MockedOtel():
        tel.init_telemetry(version="1.0.0")
    mock_logger = MagicMock()
    tel._events_logger = mock_logger
    tel.track_event("CoderEval.Task.End", {"TaskId": "demo", "Score": 0.5})

    mock_logger.info.assert_called_once()
    args, kwargs = mock_logger.info.call_args
    assert args[0] == "CoderEval.Task.End"
    extra = kwargs["extra"]
    assert extra[tel._CUSTOM_EVENT_NAME_ATTR] == "CoderEval.Task.End"
    assert extra["TaskId"] == "demo"
    assert extra["Score"] == 0.5
    # Enrichment merged in.
    assert extra["Version"] == "1.0.0"
    assert "SessionId" in extra
    assert extra["Source"] == "coder-eval"


def test_track_event_swallows_exceptions(caplog):
    mock_logger = MagicMock()
    mock_logger.info.side_effect = RuntimeError("boom")
    tel._events_logger = mock_logger
    with caplog.at_level(logging.WARNING):
        tel.track_event("CoderEval.Run.Start")  # must not raise
    assert any("track_event failed" in r.message for r in caplog.records)


def test_session_id_stable_across_events(enabled_settings):
    with _MockedOtel():
        tel.init_telemetry(version="1.0.0")
    mock_logger = MagicMock()
    tel._events_logger = mock_logger
    tel.track_event("A")
    tel.track_event("B")
    first = mock_logger.info.call_args_list[0].kwargs["extra"]["SessionId"]
    second = mock_logger.info.call_args_list[1].kwargs["extra"]["SessionId"]
    assert first == second


# --- Property coercion -------------------------------------------------------


def test_coerce_props_drops_none_and_stringifies_nonscalar():
    out = tel._coerce_props({"a": None, "b": [1, 2], "c": 5, "d": "x", "e": True})
    assert "a" not in out
    assert out["b"] == "[1, 2]"
    assert out["c"] == 5
    assert out["d"] == "x"
    assert out["e"] is True


def test_coerce_props_drops_reserved_logrecord_keys():
    # "msg"/"args"/"name"/"levelname" collide with LogRecord attributes.
    out = tel._coerce_props({"msg": "x", "args": "y", "name": "z", "Keep": 1})
    assert out == {"Keep": 1}


def test_track_event_with_reserved_key_does_not_raise(enabled_settings):
    # A reserved key would make logging raise KeyError if not dropped; use the
    # real (NullHandler) logger so the full LogRecord machinery runs.
    with _MockedOtel():
        tel.init_telemetry(version="1.0.0")
    tel.track_event("CoderEval.Run.Start", {"msg": "should-drop", "Keep": 1})


# --- Install id / platform enrichment ----------------------------------------


def test_install_id_generated_persisted_and_reused(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    first = tel._get_or_create_install_id()
    assert first
    # Persisted to the documented location as `install_id`...
    config = tmp_path / "coder-eval" / "config.json"
    assert json.loads(config.read_text())["install_id"] == first
    # ...and reused on the next call (stable across runs).
    assert tel._get_or_create_install_id() == first


def test_install_id_reads_existing_value(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "coder-eval" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"install_id": "preexisting"}))
    assert tel._get_or_create_install_id() == "preexisting"


def test_install_id_none_when_unwritable(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # Put a file where the coder-eval config dir would go → mkdir fails → the id
    # can't be persisted, so it returns None (best-effort) rather than raising.
    (tmp_path / "coder-eval").write_text("not a dir")
    assert tel._get_or_create_install_id() is None


def test_install_id_none_when_config_path_raises(monkeypatch):
    # Any failure resolving the path (e.g. Path.home() RuntimeError when HOME is
    # unset in a container) must degrade to None, never propagate — otherwise it
    # would disable telemetry instead of just dropping InstallId.
    def _boom():
        raise RuntimeError("no home")

    monkeypatch.setattr(tel, "_config_path", _boom)
    assert tel._get_or_create_install_id() is None


def test_install_id_does_not_rewrite_when_already_present(tmp_path, monkeypatch):
    # If a valid id already exists we never need to write, so an unwritable dir
    # is fine — it's returned without a rewrite.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "coder-eval" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"install_id": "already-here"}))
    config.parent.chmod(0o555)  # read-only dir → no write possible
    try:
        assert tel._get_or_create_install_id() == "already-here"
    finally:
        config.parent.chmod(0o755)


def test_init_enrichment_includes_platform_and_install_id(enabled_settings):
    with _MockedOtel():
        tel.init_telemetry(version="1.0.0")
    props = tel._default_props
    assert props["OS"]  # platform.system() — non-empty on supported platforms
    assert "OSVersion" in props
    assert "Arch" in props
    assert "PythonVersion" in props
    assert props["InstallId"]  # persisted anonymous id attached to enrichment


def test_init_emits_without_install_id_when_unwritable(tmp_path, monkeypatch, enabled_settings):
    # Non-fatal degrade: an un-creatable config file must NOT disable telemetry
    # or crash — telemetry still initializes and emits, just without InstallId.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "coder-eval").write_text("not a dir")
    with _MockedOtel():
        tel.init_telemetry(version="1.0.0")  # must NOT raise
    assert tel._initialized is True
    assert tel._events_logger is not None
    assert "InstallId" not in tel._default_props
    # Other enrichment is still present.
    assert tel._default_props["Version"] == "1.0.0"
    assert tel._default_props["OS"]


# --- Source dimension / schema version ---------------------------------------


def test_source_is_caller_settable_via_setting(enabled_settings, monkeypatch):
    # `Source` defaults to "coder-eval" but a pipeline can stamp its own origin
    # via TELEMETRY_SOURCE (settings.telemetry_source) so runs are distinguishable.
    monkeypatch.setattr(enabled_settings, "telemetry_source", "nightly-vm")
    with _MockedOtel():
        tel.init_telemetry(version="1.0.0")
    assert tel._default_props["Source"] == "nightly-vm"
    mock_logger = MagicMock()
    tel._events_logger = mock_logger
    tel.track_event("CoderEval.Task.End")
    assert mock_logger.info.call_args.kwargs["extra"]["Source"] == "nightly-vm"


def test_schema_version_stamped_on_every_event(enabled_settings):
    with _MockedOtel():
        tel.init_telemetry(version="1.0.0")
    mock_logger = MagicMock()
    tel._events_logger = mock_logger
    tel.track_event("CoderEval.Run.Start")
    assert mock_logger.info.call_args.kwargs["extra"]["SchemaVersion"] == tel._TELEMETRY_SCHEMA_VERSION


# --- First-run disclosure notice ---------------------------------------------


def test_first_run_notice_prints_to_stderr_and_persists_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    tel._maybe_show_first_run_notice()
    err = capsys.readouterr().err
    assert "telemetry" in err.lower()
    assert "TELEMETRY_ENABLED=false" in err
    # The "shown" flag is persisted alongside the install id.
    config = tmp_path / "coder-eval" / "config.json"
    assert json.loads(config.read_text())["telemetry_notice_shown"] is True


def test_first_run_notice_shown_only_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    tel._maybe_show_first_run_notice()
    assert capsys.readouterr().err  # first run discloses
    tel._maybe_show_first_run_notice()
    assert capsys.readouterr().err == ""  # subsequent runs are silent


def test_first_run_notice_not_shown_when_flag_preexists(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "coder-eval" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"telemetry_notice_shown": True}))
    tel._maybe_show_first_run_notice()
    assert capsys.readouterr().err == ""


def test_first_run_notice_best_effort_when_unpersistable(tmp_path, monkeypatch, capsys):
    # If the flag can't be persisted (config dir is a file), the notice still
    # prints and the call never raises — over-notifying beats silent collection.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "coder-eval").write_text("not a dir")
    tel._maybe_show_first_run_notice()  # must NOT raise
    assert capsys.readouterr().err


def test_init_shows_first_run_notice(enabled_settings, capsys):
    with _MockedOtel():
        tel.init_telemetry(version="1.0.0")
    assert "TELEMETRY_ENABLED=false" in capsys.readouterr().err


def test_init_does_not_touch_config_when_disabled(tmp_path, monkeypatch):
    # The default off path returns at the enable gate before the config file is
    # ever read/written.
    from coder_eval.config import settings

    monkeypatch.setattr(settings, "telemetry_enabled", True)
    monkeypatch.setattr(settings, "telemetry_connection_string", None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "coder-eval").write_text("not a dir")
    tel.init_telemetry(version="1.0.0")  # must not raise
    assert tel._initialized is False


# --- Identifier hashing ------------------------------------------------------


def test_hash_identifier_is_stable_and_not_verbatim():
    # Deterministic across calls (so dashboards can group) and never the raw value.
    h1 = tel.hash_identifier("my-secret-task-name")
    h2 = tel.hash_identifier("my-secret-task-name")
    assert h1 == h2
    assert h1 != "my-secret-task-name"
    assert "my-secret-task-name" not in h1


def test_hash_identifier_distinguishes_values():
    assert tel.hash_identifier("task-a") != tel.hash_identifier("task-b")


def test_hash_identifier_empty_maps_to_empty():
    # A missing variant stays distinguishable from a present one.
    assert tel.hash_identifier("") == ""


# --- Flush / shutdown --------------------------------------------------------


def test_flush_telemetry_calls_force_flush(enabled_settings):
    with _MockedOtel() as mocked:
        tel.init_telemetry(version="1.0.0")
    tel.flush_telemetry(1234)
    mocked.provider.force_flush.assert_called_with(1234)


def test_flush_telemetry_noop_when_uninitialized():
    tel.flush_telemetry()  # _provider is None → no raise


def test_flush_swallows_exceptions(caplog, enabled_settings):
    with _MockedOtel() as mocked:
        tel.init_telemetry(version="1.0.0")
    mocked.provider.force_flush.side_effect = RuntimeError("boom")
    with caplog.at_level(logging.WARNING):
        tel.flush_telemetry()
    assert any("flush failed" in r.message for r in caplog.records)


def test_shutdown_resets_state_allowing_reinit(enabled_settings):
    with _MockedOtel():
        tel.init_telemetry(version="1.0.0")
    assert tel._initialized is True
    tel.shutdown_telemetry()
    assert tel._provider is None
    assert tel._events_logger is None
    assert tel._initialized is False
    assert tel._default_props == {}
    # A fresh init re-initializes cleanly.
    with _MockedOtel():
        tel.init_telemetry(version="2.0.0")
    assert tel._initialized is True
    assert tel._default_props["Version"] == "2.0.0"


def test_reinit_after_shutdown_does_not_leak_handlers(enabled_settings):
    # init → shutdown → init must leave exactly one live handler on the dedicated
    # logger (shutdown must detach; init must not double-attach).
    events_logger = logging.getLogger("coder_eval.telemetry.events")
    with _MockedOtel():
        tel.init_telemetry(version="1.0.0")
    assert len(events_logger.handlers) == 1
    tel.shutdown_telemetry()
    assert len(events_logger.handlers) == 0
    with _MockedOtel():
        tel.init_telemetry(version="2.0.0")
    assert len(events_logger.handlers) == 1


def test_init_drops_stale_handler_left_by_a_prior_cycle(enabled_settings):
    # Simulate a shutdown that failed to detach: pre-attach a stray handler to
    # the dedicated logger. init_telemetry must drop it and leave exactly one
    # live handler (its own), so events are never double-emitted.
    events_logger = logging.getLogger("coder_eval.telemetry.events")
    stray = logging.NullHandler()
    events_logger.addHandler(stray)
    with _MockedOtel():
        tel.init_telemetry(version="1.0.0")
    assert len(events_logger.handlers) == 1
    assert stray not in events_logger.handlers


def test_shutdown_swallows_exceptions(caplog, enabled_settings):
    with _MockedOtel() as mocked:
        tel.init_telemetry(version="1.0.0")
    mocked.provider.shutdown.side_effect = RuntimeError("boom")
    with caplog.at_level(logging.WARNING):
        tel.shutdown_telemetry()
    assert any("shutdown failed" in r.message for r in caplog.records)


def test_double_init_is_idempotent(enabled_settings):
    with _MockedOtel() as mocked:
        tel.init_telemetry(version="1.0.0")
        first_provider = tel._provider
        tel.init_telemetry(version="1.0.0")
    assert tel._provider is first_provider
    # LoggerProvider constructed exactly once despite two init calls.
    assert mocked.provider.add_log_record_processor.call_count == 1


# --- track_command decorator -------------------------------------------------


def test_track_command_success_emits_succeeded():
    with patch.object(tel, "track_event") as mock_track:

        @tel.track_command("run")
        def cmd():
            return 42

        assert cmd() == 42
    name, props = mock_track.call_args.args
    assert name == "CoderEval.Cli.run"
    assert props["Status"] == "Succeeded"
    assert props["ErrorType"] == ""
    assert props["DurationMs"] >= 0


def test_track_command_exception_emits_failed_and_propagates():
    with patch.object(tel, "track_event") as mock_track:

        @tel.track_command("plan")
        def cmd():
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            cmd()
    _, props = mock_track.call_args.args
    assert props["Status"] == "Failed"
    assert props["ErrorType"] == "ValueError"


def test_track_command_nonzero_exit_is_failed():
    with patch.object(tel, "track_event") as mock_track:

        @tel.track_command("run")
        def cmd():
            raise typer.Exit(1)

        with pytest.raises(typer.Exit):
            cmd()
    _, props = mock_track.call_args.args
    assert props["Status"] == "Failed"
    assert props["ErrorType"] == "Exit"


def test_track_command_zero_exit_is_succeeded():
    with patch.object(tel, "track_event") as mock_track:

        @tel.track_command("report")
        def cmd():
            raise typer.Exit(0)

        with pytest.raises(typer.Exit):
            cmd()
    _, props = mock_track.call_args.args
    assert props["Status"] == "Succeeded"
    assert props["ErrorType"] == ""


@pytest.mark.parametrize("exc_cls", [KeyboardInterrupt, SystemExit])
def test_track_command_base_exception_is_failed_and_propagates(exc_cls):
    # KeyboardInterrupt / SystemExit derive from BaseException, not Exception;
    # without an explicit handler the finally would mis-record them as Succeeded.
    with patch.object(tel, "track_event") as mock_track:

        @tel.track_command("plan")
        def cmd():
            raise exc_cls()

        with pytest.raises(exc_cls):
            cmd()
    _, props = mock_track.call_args.args
    assert props["Status"] == "Failed"
    assert props["ErrorType"] == exc_cls.__name__


def test_track_command_preserves_name_and_doc():
    @tel.track_command("run")
    def my_command():
        """The original docstring."""

    assert my_command.__name__ == "my_command"
    assert my_command.__doc__ == "The original docstring."
    assert hasattr(my_command, "__wrapped__")  # functools.wraps → Typer can introspect


# --- Defensive import-failure path -------------------------------------------


def test_init_disabled_on_import_error(enabled_settings):
    import sys

    # Setting a sys.modules entry to None makes `import` of it raise ImportError,
    # exercising the inner defensive degrade-to-disabled branch.
    with patch.dict(sys.modules, {"azure.monitor.opentelemetry.exporter": None}):
        tel.init_telemetry(version="1.0.0")  # must not raise
    assert tel._events_logger is None
    assert tel._initialized is False


def test_init_does_not_log_connection_string_on_exporter_failure(caplog, enabled_settings):
    # The exporter parses the connection string (a credential) and a parse error
    # can echo it back. init_telemetry must degrade to disabled and log a generic
    # message WITHOUT interpolating the credential.
    secret = enabled_settings.telemetry_connection_string
    with (
        _MockedOtel(),
        patch(
            "azure.monitor.opentelemetry.exporter.AzureMonitorLogExporter",
            side_effect=ValueError(f"invalid connection string: {secret}"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        tel.init_telemetry(version="1.0.0")  # must not raise
    assert tel._initialized is False
    assert tel._events_logger is None
    # The credential must not appear anywhere in the captured logs.
    assert all(secret not in r.getMessage() for r in caplog.records)
    assert any("connection string is invalid" in r.message for r in caplog.records)
