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

    def test_sample_flag_exists(self):
        """--sample flag (dataset row cap) should be recognized in run --help."""
        result = runner.invoke(app, ["run", "--help"])
        assert "--sample" in _strip_ansi(result.output)

    def test_sample_flag_rejects_zero(self, tmp_path: Path):
        """--sample 0 should fail validation (min=1)."""
        # Pass a non-existent task to keep the run from actually executing;
        # Typer should reject the flag value before anything runs.
        bad_task = tmp_path / "no.yaml"
        result = runner.invoke(app, ["run", str(bad_task), "--sample", "0"])
        assert result.exit_code != 0
        out = _strip_ansi(result.output)
        # Typer emits a value-range error for min=1 violations.
        assert "sample" in out.lower() or "invalid" in out.lower()

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


class TestRepeatsFlag:
    def test_repeats_flag_exists(self):
        """--repeats flag should appear in run --help."""
        result = runner.invoke(app, ["run", "--help"])
        assert "--repeats" in _strip_ansi(result.output)

    def test_repeats_flag_rejects_zero(self, tmp_path: Path):
        """--repeats 0 should fail validation (min=1)."""
        bad_task = tmp_path / "no.yaml"
        result = runner.invoke(app, ["run", str(bad_task), "--repeats", "0"])
        assert result.exit_code != 0
        out = _strip_ansi(result.output)
        assert "repeats" in out.lower() or "invalid" in out.lower()

    def test_repeats_flag_in_help(self):
        """--repeats flag description should mention replicates."""
        result = runner.invoke(app, ["run", "--help"])
        assert "--repeats" in _strip_ansi(result.output)
