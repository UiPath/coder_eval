"""Tests for CLI --experiment flag integration."""

import os
import re
from pathlib import Path

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

    def test_resolve_experiment_path_from_subdirectory(self, tmp_path: Path):
        """Experiment resolution should work regardless of CWD."""
        from coder_eval.cli.run_command import _resolve_experiment_path

        subdir = tmp_path / "some" / "subdir"
        subdir.mkdir(parents=True)

        original_cwd = os.getcwd()
        try:
            os.chdir(subdir)
            result = _resolve_experiment_path(Path("model-comparison"))
            assert result is not None
            assert result.exists()
            assert result.name == "model-comparison.yaml"
        finally:
            os.chdir(original_cwd)
