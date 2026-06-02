"""Tests for ``agent.sdk_options.*`` overrides via the generic ``-D`` flag.

``--sdk-option`` was removed in the surface-reduction refactor; SDK pass-through
options are now expressed as ``-D agent.sdk_options.<key>=<value>``. These tests
assert the same observable value coercion (YAML 1.2 canonicals through, YAML 1.1
truthy aliases kept as strings) holds end-to-end through ``_build_overrides``.
"""

from __future__ import annotations

import pytest

from coder_eval.cli.run_command import _build_overrides


def _sdk_opts(pairs: list[str]) -> dict[str, object]:
    """Build overrides from ``key=value`` SDK pairs (via ``-D``) and return the sdk sub-map."""
    prefix = "agent.sdk_options."
    overrides = _build_overrides(
        model=None,
        driver=None,
        set_overrides=[f"{prefix}{p}" for p in pairs],
    )
    return {k[len(prefix) :]: v for k, v in overrides.items() if k.startswith(prefix)}


class TestSdkOptionCoercion:
    """Value coercion: YAML 1.2 canonicals through, YAML 1.1 truthy aliases as strings."""

    def test_canonical_true_false_become_bool(self):
        assert _sdk_opts(["some_flag=true", "verbose=false"]) == {"some_flag": True, "verbose": False}

    def test_int_and_float_coerce(self):
        assert _sdk_opts(["max_thinking_tokens=2048", "ratio=0.5"]) == {"max_thinking_tokens": 2048, "ratio": 0.5}

    def test_null_coerces_to_none(self):
        assert _sdk_opts(["fallback_model=null"]) == {"fallback_model": None}

    def test_plain_string_passes_through(self):
        assert _sdk_opts(["effort=high"]) == {"effort": "high"}

    @pytest.mark.parametrize(
        "raw",
        ["on", "off", "yes", "no", "y", "n", "ON", "Off", "Yes", "NO", "Y", "N"],
    )
    def test_yaml_1_1_truthy_aliases_stay_strings(self, raw):
        """Foot-gun: yaml.safe_load("on") returns True in YAML 1.1. Keep as string."""
        assert _sdk_opts([f"some_field={raw}"]) == {"some_field": raw}, f"Expected {raw!r} to stay a string"

    def test_string_containing_truthy_alias_not_affected(self):
        """Only the bare YAML 1.1 keywords are stringified — not substrings."""
        assert _sdk_opts(["mode=yesterday"]) == {"mode": "yesterday"}

    def test_empty_list_is_empty_dict(self):
        assert _sdk_opts([]) == {}
