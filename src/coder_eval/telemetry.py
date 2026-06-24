"""User telemetry via OpenTelemetry → Azure Application Insights ``customEvents``.

A self-contained, opt-out usage-telemetry side-channel. It emits discrete
lifecycle events (run-start, task-end, per-command) to the App Insights
``customEvents`` table and is **never** part of the eval data path.

How customEvents routing works
------------------------------
The Azure Monitor exporter routes an OpenTelemetry *log record* to the
``customEvents`` table (instead of the default ``traces`` table) **iff** the
record carries the attribute ``microsoft.custom_event.name``. The event name is
that attribute's value; every other record attribute becomes a
``customDimensions`` entry. We reach that attribute through plain stdlib
logging: an OTel ``LoggingHandler`` is attached to a dedicated logger, and
``track_event`` calls ``logger.info(name, extra={...})``. So the only OTel/Azure
imports live inside ``init_telemetry`` — ``track_event`` is pure stdlib and a
cheap no-op when telemetry is off.

Posture
-------
Telemetry is **off unless** a connection string is configured (no embedded
default — this repo ships none). It activates only when
``APPLICATIONINSIGHTS_CONNECTION_STRING`` / ``UIPATH_AI_CONNECTION_STRING`` is
set and ``TELEMETRY_ENABLED`` is not false (the single canonical disable gate).
No prompts, file contents, or repo paths are ever captured — only enums, counts,
durations, an anonymous per-install id (a random UUID persisted in the user
config file — identifies an install, not a person), and non-PII platform
identity (OS / arch / Python version).

Non-fatal contract
-------------------
Every public function wraps its body in ``try/except Exception`` and logs a
warning rather than raising — telemetry must never break a run. This invariant
is enforced by the CE019 custom lint rule. Persisting the anonymous install id
is best-effort too: if its config file can't be written, telemetry still emits
events, just without the ``InstallId`` dimension.
"""

import atexit
import hashlib
import json
import logging
import os
import platform
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import typer


logger = logging.getLogger(__name__)

# Dedicated stdlib logger that fans events into the OTel handler. Stays None
# until init_telemetry wires up a provider; track_event no-ops while None.
_events_logger: logging.Logger | None = None
# The OTel LoggerProvider (typed Any: OTel/Azure are largely untyped and we keep
# their symbols confined to init_telemetry to avoid leaking Unknown elsewhere).
_provider: Any = None
# The OTel handler attached to the dedicated events logger; tracked so
# shutdown_telemetry can detach it (the logger is a process-wide singleton that
# outlives a shutdown, so leaving it attached would double-emit on a re-init).
_handler: logging.Handler | None = None
# Enrichment merged into every event — all scalar, no user content. The
# per-process session id lives here under "SessionId" (no separate global).
_default_props: dict[str, str] = {}
_initialized: bool = False

# stdlib LogRecord reserved attribute names. A property key colliding with one
# of these makes logging raise KeyError on emit, so the coercer drops them.
_RESERVED_LOGRECORD_ATTRS: frozenset[str] = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}

# The attribute the Azure Monitor exporter looks for to route a log record to
# the customEvents table. Hard-coded (matches the exporter's internal
# _MICROSOFT_CUSTOM_EVENT_NAME constant) so track_event needs no OTel import.
_CUSTOM_EVENT_NAME_ATTR = "microsoft.custom_event.name"

# The scalar contract for event properties. Public so producers (e.g.
# orchestrator.build_task_event) can annotate their event dicts with it and have
# pyright reject a non-scalar at the producing call site, not just at runtime.
Scalar = str | int | float | bool

F = TypeVar("F", bound=Callable[..., Any])


def hash_identifier(value: str) -> str:
    """Stable, one-way hash of a free-text identifier for telemetry.

    ``TaskId`` / ``VariantId`` are author-defined free-text that could encode
    sensitive data, so they are emitted as a truncated SHA-256 hex digest rather
    than verbatim — the raw string never reaches the telemetry store. The digest
    is deterministic across runs/processes/platforms (unlike the salted builtin
    ``hash()``), so dashboards can still group by a stable key, and is the same
    across installs so a task can be sliced fleet-wide. Empty input maps to empty
    output so a missing variant stays distinguishable from a present one.
    """
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _coerce_props(properties: dict[str, Any] | None) -> dict[str, Scalar]:
    """Coerce a property map into logging-safe scalars.

    Drops ``None`` values and keys that collide with reserved ``LogRecord``
    attribute names; ``str()``-ifies any non-scalar value so no nested
    structure reaches the exporter.
    """
    coerced: dict[str, Scalar] = {}
    for key, value in (properties or {}).items():
        if value is None or key in _RESERVED_LOGRECORD_ATTRS:
            continue
        coerced[key] = value if isinstance(value, str | int | float | bool) else str(value)
    return coerced


def _config_path() -> Path:
    """Path to the user config file holding the stable anonymous install id.

    ``$XDG_CONFIG_HOME/coder-eval/config.json`` (``~/.config/coder-eval/config.json``
    by default), matching the XDG base-directory convention on every platform.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "coder-eval" / "config.json"


def _get_or_create_install_id() -> str | None:
    """Return a stable, anonymous per-install id (no PII), or None if unpersistable.

    Persisted as ``install_id`` in the user config file, generated once on first
    run and reused thereafter so the same machine/install reads as one "user".
    The value is a random UUID — it identifies an install, not a person.

    Best-effort: if the id can't be resolved or persisted for ANY reason (read-only
    home, no HOME so ``Path.home()`` raises, corrupt state, …) this returns None and
    telemetry simply emits events without the ``InstallId`` dimension — it never
    raises, because telemetry must never break a run.
    """
    try:
        path = _config_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            # File missing/unreadable/corrupt → start fresh and try to write below.
            data = {}

        existing = data.get("install_id")
        if isinstance(existing, str) and existing:
            return existing

        install_id = str(uuid.uuid4())
        data["install_id"] = install_id
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return install_id
    except Exception as exc:
        # Never propagate: a missing HOME (Path.home() → RuntimeError), an
        # unwritable dir (OSError), etc. must degrade to no InstallId, NOT disable
        # telemetry. Keeping telemetry live without InstallId is the agreed behavior.
        logger.debug("could not persist install id (%s); telemetry will omit InstallId", exc)
        return None


def init_telemetry(version: str) -> None:
    """Initialize telemetry once per process (no-op if disabled or already done).

    Fully non-fatal: if the install-id config file can't be written, telemetry
    still initializes and emits events — just without the ``InstallId`` dimension
    — and never crashes the process.

    Args:
        version: The coder-eval version, recorded as the ``Version`` dimension.
    """
    global _events_logger, _provider, _handler, _default_props, _initialized
    try:
        if _initialized:
            return

        # Single-init contract: settings is read once per process here. A
        # shutdown → re-init cycle re-reads the same module-global settings
        # singleton (only tests, which monkeypatch settings, exercise re-init).
        from coder_eval.config import settings

        if not settings.telemetry_enabled or not settings.telemetry_connection_string:
            return

        try:
            import warnings

            from azure.monitor.opentelemetry.exporter import AzureMonitorLogExporter
            from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
            from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
            from opentelemetry.sdk.resources import Resource
        except ImportError:
            logger.debug("telemetry SDK unavailable; disabled")
            return

        # Construct the exporter in its own guard: it parses the connection
        # string (a credential — InstrumentationKey + IngestionEndpoint) and a
        # parse error can echo it back, so on failure log a generic message
        # WITHOUT interpolating the exception, never leaking the credential.
        try:
            exporter = AzureMonitorLogExporter(connection_string=settings.telemetry_connection_string)
        except Exception:
            logger.warning("telemetry connection string is invalid; telemetry disabled")
            return

        provider = LoggerProvider(resource=Resource.create({"service.name": "coder-eval"}))
        provider.add_log_record_processor(BatchLogRecordProcessor(exporter))

        # The SDK's LoggingHandler is deprecated in favor of a separate
        # instrumentation package we don't depend on; the documented attribute
        # bridge still works. Suppress the one-time warning so enabling
        # telemetry doesn't print noise to a user's stderr.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            handler = LoggingHandler(level=logging.INFO, logger_provider=provider)

        events_logger = logging.getLogger("coder_eval.telemetry.events")
        events_logger.setLevel(logging.INFO)
        events_logger.propagate = False  # never reach console/file handlers
        # The logger is a process-wide singleton; drop any stale handler from a
        # prior init/shutdown cycle before attaching this one.
        for stale in list(events_logger.handlers):
            events_logger.removeHandler(stale)
        events_logger.addHandler(handler)

        _default_props = {
            "Version": version,
            "SessionId": str(uuid.uuid4()),
            "Source": "coder-eval",
            "IsCI": str(bool(os.getenv("CI"))),
            # Generic product-telemetry dimensions: non-PII platform identity.
            "OS": platform.system(),
            "OSVersion": platform.release(),
            "Arch": platform.machine(),
            "PythonVersion": platform.python_version(),
        }
        # Best-effort: omit InstallId (rather than fail) if it can't be persisted.
        install_id = _get_or_create_install_id()
        if install_id:
            _default_props["InstallId"] = install_id

        _provider = provider
        _handler = handler
        _events_logger = events_logger
        _initialized = True
        atexit.register(shutdown_telemetry)
    except Exception as exc:
        # Fully non-fatal — telemetry must never break a run. (An unpersistable
        # install id is handled earlier as best-effort: telemetry stays on and
        # just omits InstallId, so it doesn't reach here.)
        logger.warning("telemetry init failed: %s", exc)


def track_event(name: str, properties: dict[str, Scalar] | None = None) -> None:
    """Emit a single custom event (no-op if telemetry is uninitialized).

    Merges the process-level enrichment with the caller's ``properties`` and
    routes the record to App Insights ``customEvents`` via the reserved
    ``microsoft.custom_event.name`` attribute.
    """
    try:
        if _events_logger is None:
            return
        merged = {**_default_props, **_coerce_props(properties)}
        _events_logger.info(name, extra={_CUSTOM_EVENT_NAME_ATTR: name, **merged})
    except Exception as exc:
        logger.warning("telemetry track_event failed: %s", exc)


def track_command(name: str) -> Callable[[F], F]:
    """Decorator factory wrapping a CLI command to emit a ``CoderEval.Cli.<name>`` event.

    Captures ``Status`` / ``DurationMs`` / ``ErrorType`` on every exit path
    (return, ``typer.Exit``, exception) and always re-raises the original
    exception/exit. ``functools.wraps`` preserves ``__wrapped__`` so Typer/click
    still introspect the real command signature.
    """
    import functools

    def deco(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.monotonic()
            status, error_type = "Succeeded", ""
            try:
                return func(*args, **kwargs)
            except typer.Exit as exc:
                if exc.exit_code:  # non-zero exit code → failure
                    status, error_type = "Failed", "Exit"
                raise
            except (KeyboardInterrupt, SystemExit) as exc:
                # These derive from BaseException, not Exception, so without this
                # branch a Ctrl-C / sys.exit would skip both handlers and the
                # finally would mis-record the aborted command as "Succeeded".
                status, error_type = "Failed", type(exc).__name__
                raise
            except Exception as exc:
                status, error_type = "Failed", type(exc).__name__
                raise
            finally:
                track_event(
                    f"CoderEval.Cli.{name}",
                    {"Status": status, "DurationMs": int((time.monotonic() - start) * 1000), "ErrorType": error_type},
                )

        # functools.wraps copies func's signature onto wrapper for Typer/click,
        # but the inferred type is Callable[..., Any], not the TypeVar F — the
        # cast restores F so callers see the original command's type.
        return wrapper  # type: ignore[return-value]

    return deco


def flush_telemetry(timeout_millis: int = 5000) -> None:
    """Force-flush buffered events (no-op if telemetry is uninitialized)."""
    try:
        if _provider is None:
            return
        _provider.force_flush(timeout_millis)
    except Exception as exc:
        logger.warning("telemetry flush failed: %s", exc)


def shutdown_telemetry() -> None:
    """Flush and tear down telemetry, resetting module state for a clean re-init."""
    global _events_logger, _provider, _handler, _default_props, _initialized
    try:
        if _provider is None:
            return
        provider = _provider
        if _events_logger is not None and _handler is not None:
            _events_logger.removeHandler(_handler)
        _events_logger = None
        _provider = None
        _handler = None
        _default_props = {}
        _initialized = False
        provider.force_flush()
        provider.shutdown()
    except Exception as exc:
        logger.warning("telemetry shutdown failed: %s", exc)
