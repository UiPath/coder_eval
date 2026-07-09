"""Tests for judge model-name translation helpers."""

from __future__ import annotations

import re

import pytest

from coder_eval.evaluation.judge_models import to_anthropic_alias, to_bedrock_model


# Representative vendor-prefixed model names (the Bedrock AWS-id dialect) the judge
# model-name translators must normalize. Formerly sourced from the proxy's
# DEFAULT_MODEL_MAP; kept inline here now that the proxy has been removed.
_VENDOR_MODEL_NAMES = [
    "anthropic.claude-sonnet-4-6",
    "anthropic.claude-opus-4-6-v1",
    "anthropic.claude-sonnet-4-20250514-v1:0",
    "anthropic.claude-opus-4-5-20251101-v1:0",
    "anthropic.claude-sonnet-4-5-20250929-v1:0",
    "anthropic.claude-haiku-4-5-20251001-v1:0",
    "anthropic.claude-3-7-sonnet-20250219-v1:0",
    "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "anthropic.claude-3-opus-20240229-v1:0",
    "anthropic.claude-3-haiku-20240307-v1:0",
]


# --- to_anthropic_alias ---


def test_to_anthropic_alias_strips_vendor_prefix() -> None:
    assert to_anthropic_alias("anthropic.claude-sonnet-4-6") == "claude-sonnet-4-6"


def test_to_anthropic_alias_strips_v1_suffix() -> None:
    assert to_anthropic_alias("anthropic.claude-opus-4-6-v1") == "claude-opus-4-6"


def test_to_anthropic_alias_strips_v2_0_suffix() -> None:
    assert to_anthropic_alias("anthropic.claude-3-5-sonnet-20241022-v2:0") == "claude-3-5-sonnet-20241022"


def test_to_anthropic_alias_idempotent_on_bare_alias() -> None:
    assert to_anthropic_alias("claude-sonnet-4-6") == "claude-sonnet-4-6"


def test_to_anthropic_alias_preserves_latest_suffix() -> None:
    assert to_anthropic_alias("claude-sonnet-4-5-latest") == "claude-sonnet-4-5-latest"


@pytest.mark.parametrize("value", ["", "   ", "anthropic."])
def test_to_anthropic_alias_raises_on_empty(value: str) -> None:
    with pytest.raises(ValueError):
        to_anthropic_alias(value)


# --- to_bedrock_model ---


def test_to_bedrock_model_qualifies_bare_alias() -> None:
    assert to_bedrock_model("claude-sonnet-4-6", "eu-north-1") == "eu.anthropic.claude-sonnet-4-6"


def test_to_bedrock_model_strips_v1_suffix_then_qualifies() -> None:
    assert to_bedrock_model("anthropic.claude-opus-4-6-v1", "eu-north-1") == "eu.anthropic.claude-opus-4-6"


def test_to_bedrock_model_idempotent_on_qualified_id() -> None:
    assert to_bedrock_model("eu.anthropic.claude-sonnet-4-6", "eu-north-1") == "eu.anthropic.claude-sonnet-4-6"


def test_to_bedrock_model_us_region_prefix() -> None:
    assert to_bedrock_model("anthropic.claude-sonnet-4-6", "us-east-1") == "us.anthropic.claude-sonnet-4-6"


def test_to_bedrock_model_raises_on_empty() -> None:
    with pytest.raises(ValueError):
        to_bedrock_model("", "eu-north-1")


@pytest.mark.parametrize("vendor_value", _VENDOR_MODEL_NAMES)
def test_to_bedrock_model_table_drives_vendor_entries(vendor_value: str) -> None:
    """Every representative vendor-prefixed model name translates to a clean Bedrock id."""
    result = to_bedrock_model(vendor_value, "eu-north-1")
    assert re.fullmatch(r"eu\.anthropic\.[a-z0-9.-]+", result), result
    assert not re.search(r"-v\d+(?::\d+)?$", result), result


@pytest.mark.parametrize("vendor_value", _VENDOR_MODEL_NAMES)
def test_to_anthropic_alias_table_drives_vendor_entries(vendor_value: str) -> None:
    """Every representative vendor-prefixed model name translates to a clean bare Anthropic alias."""
    result = to_anthropic_alias(vendor_value)
    assert not result.startswith("anthropic."), result
    assert not re.search(r"-v\d+(?::\d+)?$", result), result
    assert result, result
