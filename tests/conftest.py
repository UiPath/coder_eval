"""Pytest configuration and shared fixtures."""

import logging

import pytest


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
