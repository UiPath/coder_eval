"""The ``--backend`` CLI flag must sync into ``os.environ``, not just mutate Settings.

The docker driver forwards the run's backend into the container via the standard env
passthrough (name-only ``--env API_BACKEND``, read from ``os.environ``). A flag that
only mutated in-process Settings would be dropped at the container boundary and the
in-container Settings would silently default to DIRECT — downgrading the judge (and
agent) route. This pins the flag → ``os.environ`` sync that closes that gap and keeps
the backend on the same wiring as every other forwarded env var.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from coder_eval.cli import app
from coder_eval.config import settings


runner = CliRunner()


async def _noop_run_all_tasks(*args: object, **kwargs: object) -> None:
    """Stand in for the real async batch run so the command returns right after
    the backend-override block (which is what we're pinning)."""
    return None


@pytest.mark.parametrize("backend", ["bedrock", "direct"])
def test_backend_flag_syncs_api_backend_env(monkeypatch, backend) -> None:
    # delenv snapshots the key so monkeypatch's teardown restores it even though
    # the command mutates os.environ directly (no test pollution across params).
    monkeypatch.delenv("API_BACKEND", raising=False)
    # `--backend` also mutates the process-wide ``settings`` singleton in place.
    # Snapshot + restore it here (via monkeypatch teardown) so this test is
    # self-contained and can't leak ``api_backend`` into later tests under a
    # single-process run.
    monkeypatch.setattr(settings, "api_backend", settings.api_backend)
    with patch("coder_eval.cli.run_command._run_all_tasks", _noop_run_all_tasks):
        result = runner.invoke(app, ["run", "tasks/hello_date.yaml", "--backend", backend])
    assert result.exit_code == 0, result.output
    assert os.environ.get("API_BACKEND") == backend


def test_no_backend_flag_leaves_api_backend_env_untouched(monkeypatch) -> None:
    """Without ``--backend``, the command must not invent an API_BACKEND value."""
    monkeypatch.delenv("API_BACKEND", raising=False)
    with patch("coder_eval.cli.run_command._run_all_tasks", _noop_run_all_tasks):
        result = runner.invoke(app, ["run", "tasks/hello_date.yaml"])
    assert result.exit_code == 0, result.output
    assert "API_BACKEND" not in os.environ
