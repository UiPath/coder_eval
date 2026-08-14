"""Unit tests for the plugin pricing-registration seam (register_pricing)."""

import pytest

from coder_eval import pricing
from coder_eval.pricing import ModelPricing, calculate_cost, is_priced, register_pricing


@pytest.fixture(autouse=True)
def _isolated_registered_pricing(monkeypatch):
    """Snapshot/restore the overlay so tests never leak global state."""
    monkeypatch.setattr(pricing, "_REGISTERED_PRICING", {})


def test_registered_rate_resolves_through_calculate_cost():
    register_pricing({"acme-1": ModelPricing(1.0, 2.0, 3.0, 4.0)})
    # 1M uncached input at $1/MTok, no output → $1.0.
    assert calculate_cost("acme-1", 1_000_000, 0) == 1.0


def test_registered_bare_key_resolves_through_prefixed_lookup():
    """register_pricing stores keys verbatim (bare), but calculate_cost resolves
    via _normalize_model — so a bare-registered key must still price a
    Bedrock/vendor-prefixed model id. Guards against the two normalization seams
    (registration vs lookup) drifting apart."""
    register_pricing({"acme-1": ModelPricing(1.0, 2.0, 3.0, 4.0)})
    # Same $1/MTok input rate must resolve through the qualified inference-profile id.
    assert calculate_cost("us.anthropic.acme-1", 1_000_000, 0) == 1.0
    assert calculate_cost("eu.acme-1", 1_000_000, 0) == 1.0


def test_openrouter_provider_prefix_normalizes_to_bare_key():
    """An agent that addresses OpenRouter natively (OpenCode) reports the model
    WITH its provider prefix, while the rate-card keys are the bare vendor/model
    ids the LiteLLM route uses. Both spellings must price identically, else the
    same model silently goes unpriced depending on which agent ran it."""
    assert is_priced("deepseek/deepseek-v4-flash-0731")
    assert is_priced("openrouter/deepseek/deepseek-v4-flash-0731")
    bare = calculate_cost("deepseek/deepseek-v4-flash-0731", 1_000_000, 1_000_000)
    prefixed = calculate_cost("openrouter/deepseek/deepseek-v4-flash-0731", 1_000_000, 1_000_000)
    assert bare == prefixed > 0


def test_lookup_rate_overlay_precedes_builtins():
    """_lookup_rate returns the registered overlay before the built-in table.
    The anti-shadow rule forbids a registered value *differing* from a built-in,
    so precedence can't be observed via a differing rate — pin it directly with a
    brand-new key absent from _PRICING and assert object identity."""
    assert "acme-1" not in pricing._PRICING
    rate = ModelPricing(1.0, 2.0, 3.0, 4.0)
    register_pricing({"acme-1": rate})
    assert pricing._lookup_rate("acme-1") is rate


def test_identical_reregistration_is_noop():
    rate = ModelPricing(1.0, 2.0, 3.0, 4.0)
    register_pricing({"acme-1": rate})
    register_pricing({"acme-1": ModelPricing(1.0, 2.0, 3.0, 4.0)})  # equal by value
    assert calculate_cost("acme-1", 1_000_000, 0) == 1.0


def test_conflicting_rate_for_registered_key_raises():
    register_pricing({"acme-1": ModelPricing(1.0, 2.0, 3.0, 4.0)})
    with pytest.raises(ValueError, match="already registered"):
        register_pricing({"acme-1": ModelPricing(9.0, 2.0, 3.0, 4.0)})


def test_conflicting_rate_against_builtin_raises():
    builtin = pricing._PRICING["claude-opus-4-8"]
    different = ModelPricing(
        builtin.input_per_mtok + 1.0,
        builtin.output_per_mtok,
        builtin.cache_write_per_mtok,
        builtin.cache_read_per_mtok,
    )
    with pytest.raises(ValueError, match="already registered"):
        register_pricing({"claude-opus-4-8": different})


def test_identical_rate_against_builtin_is_noop():
    builtin = pricing._PRICING["claude-opus-4-8"]
    register_pricing(
        {
            "claude-opus-4-8": ModelPricing(
                builtin.input_per_mtok,
                builtin.output_per_mtok,
                builtin.cache_write_per_mtok,
                builtin.cache_read_per_mtok,
            )
        }
    )
    # Built-in lookup still works and is unchanged.
    assert calculate_cost("claude-opus-4-8", 1_000_000, 0) == builtin.input_per_mtok


def test_unknown_model_still_returns_none():
    assert calculate_cost("not-a-real-model", 1_000_000, 0) is None


def test_batch_with_conflict_registers_nothing():
    """All-or-nothing: a conflict on a later key must not half-register the
    earlier keys in the same batch."""
    register_pricing({"acme-1": ModelPricing(1.0, 2.0, 3.0, 4.0)})
    with pytest.raises(ValueError, match="already registered"):
        register_pricing(
            {
                "acme-2": ModelPricing(5.0, 6.0, 7.0, 8.0),  # new, would-be-good
                "acme-1": ModelPricing(9.0, 2.0, 3.0, 4.0),  # conflicts → whole batch aborts
            }
        )
    # acme-2 must NOT have been committed by the aborted batch.
    assert calculate_cost("acme-2", 1_000_000, 0) is None


def test_all_zero_rate_resolves_and_blocks_shadowing():
    """A free (all-zero) rate is a real entry: it resolves to $0 and still
    participates in the anti-shadow check (guards the is-not-None lookup)."""
    register_pricing({"free-1": ModelPricing(0.0, 0.0, 0.0, 0.0)})
    assert calculate_cost("free-1", 1_000_000, 1_000_000) == 0.0
    with pytest.raises(ValueError, match="already registered"):
        register_pricing({"free-1": ModelPricing(1.0, 0.0, 0.0, 0.0)})
