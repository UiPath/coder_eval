"""Tests for judge model-name translation helpers."""

from __future__ import annotations

import re

import pytest

from coder_eval.evaluation.judge_models import to_anthropic_alias, to_bedrock_model
from coder_eval.proxy.config import DEFAULT_MODEL_MAP


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


@pytest.mark.parametrize("gateway_value", sorted(set(DEFAULT_MODEL_MAP.values())))
def test_to_bedrock_model_table_drives_proxy_config_entries(gateway_value: str) -> None:
    """Every gateway model name in DEFAULT_MODEL_MAP translates to a clean Bedrock id."""
    result = to_bedrock_model(gateway_value, "eu-north-1")
    assert re.fullmatch(r"eu\.anthropic\.[a-z0-9.-]+", result), result
    assert not re.search(r"-v\d+(?::\d+)?$", result), result


@pytest.mark.parametrize("gateway_value", sorted(set(DEFAULT_MODEL_MAP.values())))
def test_to_anthropic_alias_table_drives_proxy_config_entries(gateway_value: str) -> None:
    """Every gateway model name in DEFAULT_MODEL_MAP translates to a clean bare Anthropic alias."""
    result = to_anthropic_alias(gateway_value)
    assert not result.startswith("anthropic."), result
    assert not re.search(r"-v\d+(?::\d+)?$", result), result
    assert result, result
