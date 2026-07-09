"""Tests for resolve_variant_initial_prompt_file in task_loader.py."""

import pytest

from coder_eval.models import ExperimentVariant
from coder_eval.orchestration.task_loader import resolve_variant_initial_prompt_file


class TestResolveVariantInitialPromptFile:
    def test_resolves_relative_path(self, tmp_path):
        prompt_file = tmp_path / "prompts" / "custom.md"
        prompt_file.parent.mkdir(parents=True)
        prompt_file.write_text("Custom prompt content", encoding="utf-8")

        variant = ExperimentVariant(variant_id="test", initial_prompt_file="prompts/custom.md")
        resolve_variant_initial_prompt_file(variant, tmp_path)

        assert variant.initial_prompt == "Custom prompt content"
        assert variant.initial_prompt_file is None

    def test_resolves_absolute_path(self, tmp_path):
        prompt_file = tmp_path / "absolute.md"
        prompt_file.write_text("Absolute prompt", encoding="utf-8")

        variant = ExperimentVariant(variant_id="test", initial_prompt_file=str(prompt_file))
        resolve_variant_initial_prompt_file(variant, tmp_path)

        assert variant.initial_prompt == "Absolute prompt"
        assert variant.initial_prompt_file is None

    def test_missing_file_raises(self, tmp_path):
        variant = ExperimentVariant(variant_id="test", initial_prompt_file="nonexistent.md")

        with pytest.raises(FileNotFoundError, match="variant initial_prompt_file not found"):
            resolve_variant_initial_prompt_file(variant, tmp_path)

    def test_strips_whitespace(self, tmp_path):
        prompt_file = tmp_path / "padded.md"
        prompt_file.write_text("  content with whitespace  \n\n", encoding="utf-8")

        variant = ExperimentVariant(variant_id="test", initial_prompt_file="padded.md")
        resolve_variant_initial_prompt_file(variant, tmp_path)

        assert variant.initial_prompt == "content with whitespace"

    def test_noop_when_no_file(self, tmp_path):
        variant = ExperimentVariant(variant_id="test")
        resolve_variant_initial_prompt_file(variant, tmp_path)

        assert variant.initial_prompt is None
        assert variant.initial_prompt_file is None

    def test_clears_file_before_setting_prompt(self, tmp_path):
        """Verify field-clearing order avoids mutual exclusivity validator."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("resolved content", encoding="utf-8")

        variant = ExperimentVariant(variant_id="test", initial_prompt_file="prompt.md")

        # This should not raise the mutual-exclusivity validator
        resolve_variant_initial_prompt_file(variant, tmp_path)

        assert variant.initial_prompt == "resolved content"
        assert variant.initial_prompt_file is None
