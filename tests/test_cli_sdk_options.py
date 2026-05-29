"""Tests for the ``--sdk-option KEY=VALUE`` CLI parser."""

from __future__ import annotations

import pytest
import typer

from coder_eval.cli.run_command import _parse_sdk_options


class TestParseSdkOptionsCoercion:
    """Value coercion: YAML 1.2 canonicals through, YAML 1.1 truthy aliases as strings."""

    def test_canonical_true_false_become_bool(self):
        out = _parse_sdk_options(["some_flag=true", "verbose=false"])
        assert out == {"some_flag": True, "verbose": False}

    def test_int_and_float_coerce(self):
        out = _parse_sdk_options(["max_thinking_tokens=2048", "ratio=0.5"])
        assert out == {"max_thinking_tokens": 2048, "ratio": 0.5}

    def test_null_coerces_to_none(self):
        out = _parse_sdk_options(["fallback_model=null"])
        assert out == {"fallback_model": None}

    def test_plain_string_passes_through(self):
        out = _parse_sdk_options(["effort=high"])
        assert out == {"effort": "high"}

    @pytest.mark.parametrize(
        "raw",
        ["on", "off", "yes", "no", "y", "n", "ON", "Off", "Yes", "NO", "Y", "N"],
    )
    def test_yaml_1_1_truthy_aliases_stay_strings(self, raw):
        """Foot-gun: yaml.safe_load("on") returns True in YAML 1.1. Keep as string."""
        out = _parse_sdk_options([f"some_field={raw}"])
        assert out == {"some_field": raw}, f"Expected {raw!r} to stay a string"

    def test_string_containing_truthy_alias_not_affected(self):
        """Only the bare YAML 1.1 keywords are stringified — not substrings."""
        out = _parse_sdk_options(["mode=yesterday"])
        assert out == {"mode": "yesterday"}


class TestParseSdkOptionsValidation:
    def test_missing_equals_raises(self):
        with pytest.raises(typer.BadParameter, match="must be key=value"):
            _parse_sdk_options(["bareword"])

    def test_empty_key_raises(self):
        with pytest.raises(typer.BadParameter, match="key cannot be empty"):
            _parse_sdk_options(["=value"])

    def test_duplicate_key_last_wins(self):
        out = _parse_sdk_options(["effort=low", "effort=high"])
        assert out == {"effort": "high"}

    def test_empty_list_is_empty_dict(self):
        assert _parse_sdk_options([]) == {}
