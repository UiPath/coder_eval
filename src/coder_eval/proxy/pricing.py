"""Official Anthropic model pricing for cost calculation.

Prices are per million tokens (MTok). Source: https://www.anthropic.com/pricing
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    """Pricing for a single model (per million tokens)."""

    input_per_mtok: float
    output_per_mtok: float
    cache_write_per_mtok: float  # prompt caching write
    cache_read_per_mtok: float  # prompt caching read


# Official Anthropic pricing as of 2025
# Key: CLI model name (before gateway mapping)
_PRICING: dict[str, ModelPricing] = {
    # Claude 4.6 / 4.5 / 4 Opus
    "claude-opus-4-6": ModelPricing(15.0, 75.0, 18.75, 1.50),
    "claude-opus-4-6-20250514": ModelPricing(15.0, 75.0, 18.75, 1.50),
    "claude-opus-4-5-20251101": ModelPricing(15.0, 75.0, 18.75, 1.50),
    "claude-opus-4-20250514": ModelPricing(15.0, 75.0, 18.75, 1.50),
    # Claude 4.6 / 4.5 / 4 Sonnet
    "claude-sonnet-4-6": ModelPricing(3.0, 15.0, 3.75, 0.30),
    "claude-sonnet-4-6-20250514": ModelPricing(3.0, 15.0, 3.75, 0.30),
    "claude-sonnet-4-5-20250929": ModelPricing(3.0, 15.0, 3.75, 0.30),
    "claude-sonnet-4-20250514": ModelPricing(3.0, 15.0, 3.75, 0.30),
    # Claude 4.5 Haiku
    "claude-haiku-4-5-20251001": ModelPricing(0.80, 4.0, 1.0, 0.08),
    # Claude 3.7 Sonnet
    "claude-3-7-sonnet-20250219": ModelPricing(3.0, 15.0, 3.75, 0.30),
    # Claude 3.5 Sonnet
    "claude-3-5-sonnet-20241022": ModelPricing(3.0, 15.0, 3.75, 0.30),
    "claude-3-5-sonnet-20240620": ModelPricing(3.0, 15.0, 3.75, 0.30),
    # Claude 3 Opus
    "claude-3-opus-20240229": ModelPricing(15.0, 75.0, 18.75, 1.50),
    # Claude 3 Sonnet
    "claude-3-sonnet-20240229": ModelPricing(3.0, 15.0, 3.75, 0.30),
    # Claude 3 Haiku
    "claude-3-haiku-20240307": ModelPricing(0.25, 1.25, 0.30, 0.03),
}


def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float | None:
    """Calculate cost in USD for the given token usage.

    Returns None if the model is not in the pricing table.
    """
    pricing = _PRICING.get(model)
    if pricing is None:
        return None

    return (
        input_tokens * pricing.input_per_mtok
        + output_tokens * pricing.output_per_mtok
        + cache_creation_tokens * pricing.cache_write_per_mtok
        + cache_read_tokens * pricing.cache_read_per_mtok
    ) / 1_000_000
