"""Tests for experiment YAML loading."""

from pathlib import Path

import pytest
import yaml

from coder_eval.orchestration.experiment import load_experiment


class TestLoadExperiment:
    def test_load_default_experiment(self):
        """Load the built-in default experiment."""
        exp = load_experiment(Path("experiments/default.yaml"))
        assert exp.experiment_id == "default"
        assert len(exp.variants) == 1
        assert exp.variants[0].variant_id == "default"

    def test_load_custom_experiment(self, tmp_path):
        """Load a custom experiment from YAML."""
        exp_data = {
            "experiment_id": "model-comparison",
            "description": "Compare models",
            "base": {
                "agent": {"permission_mode": "bypassPermissions"},
            },
            "variants": [
                {"variant_id": "sonnet", "agent": {"model": "claude-sonnet-4-20250514"}},
                {"variant_id": "opus", "agent": {"model": "claude-opus-4-20250514"}},
            ],
        }
        exp_file = tmp_path / "test-experiment.yaml"
        exp_file.write_text(yaml.dump(exp_data))

        exp = load_experiment(exp_file)
        assert exp.experiment_id == "model-comparison"
        assert len(exp.variants) == 2

    def test_load_nonexistent_file(self):
        with pytest.raises(FileNotFoundError):
            load_experiment(Path("nonexistent.yaml"))

    def test_load_invalid_yaml(self, tmp_path):
        exp_file = tmp_path / "bad.yaml"
        exp_file.write_text("experiment_id: 123\nvariants: not-a-list")
        with pytest.raises(ValueError, match="Invalid experiment"):
            load_experiment(exp_file)
