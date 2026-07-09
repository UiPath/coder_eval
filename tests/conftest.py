"""Pytest configuration and shared fixtures."""

import logging

import pytest


@pytest.fixture(autouse=True)
def disable_telemetry_by_default():
    """Hard-disable usage telemetry for every test (prevents prod pollution).

    A developer's local ``.env`` carries a real ``APPLICATIONINSIGHTS_CONNECTION_STRING``,
    and several CLI tests invoke the real Typer app, whose callback calls
    ``init_telemetry()``. Without this guard, ``make test`` would emit real
    ``CoderEval.*`` customEvents to the production App Insights resource. We flip
    the canonical disable gate AND clear the connection string so
    ``init_telemetry()`` no-ops on the ``not enabled or not connection_string``
    early return.

    Tests that specifically exercise telemetry re-enable it in-body via their own
    ``monkeypatch`` (e.g. ``test_telemetry.enabled_settings``), which is applied
    after this autouse fixture and therefore wins for the duration of that test.
    """
    from coder_eval.config import settings

    original_enabled = settings.telemetry_enabled
    original_conn = settings.telemetry_connection_string
    settings.telemetry_enabled = False
    settings.telemetry_connection_string = None
    try:
        yield
    finally:
        settings.telemetry_enabled = original_enabled
        settings.telemetry_connection_string = original_conn


@pytest.fixture(autouse=True)
def reset_logging_after_test():
    """Reset logging configuration after each test to prevent test pollution.

    This ensures that tests calling setup_logging() don't affect subsequent tests
    by restoring the logger's propagate flag and clearing handlers.
    """
    yield

    # Cleanup after test
    app_logger = logging.getLogger("coder_eval")

    # Restore propagate flag for pytest's caplog compatibility
    app_logger.propagate = True

    # Clear all handlers
    app_logger.handlers.clear()

    # Reset to default level
    app_logger.setLevel(logging.NOTSET)
