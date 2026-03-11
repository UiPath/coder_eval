"""Tests for CLI --experiment flag integration."""

import re

from typer.testing import CliRunner

from coder_eval.cli import app


runner = CliRunner()


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


class TestExperimentCLI:
    def test_experiment_flag_exists(self):
        """--experiment flag should be recognized."""
        result = runner.invoke(app, ["run", "--help"])
        assert "--experiment" in _strip_ansi(result.output)

    def test_experiment_short_flag_exists(self):
        """Short -e flag should be recognized."""
        result = runner.invoke(app, ["run", "--help"])
        assert "-e" in _strip_ansi(result.output)
