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
    # Claude 4.8 / 4.7 / 4.6 / 4.5 / 4 Opus
    "claude-opus-4-8": ModelPricing(15.0, 75.0, 18.75, 1.50),
    "claude-opus-4-7": ModelPricing(15.0, 75.0, 18.75, 1.50),
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
    # OpenAI GPT-5 / Codex (direct or via Azure OpenAI). Released 2025-09-23.
    # Source: https://openai.com/api/pricing (gpt-5-codex: $1.25/M in,
    # $0.125/M cached in, $10/M out). OpenAI does not bill cache writes
    # separately, so cache_write == input rate.
    "gpt-5-codex": ModelPricing(1.25, 10.0, 1.25, 0.125),
    "gpt-5": ModelPricing(1.25, 10.0, 1.25, 0.125),
    # gpt-5.3-codex (2026-02-24): $1.75/M input, $0.175/M cached, $14/M output.
    "gpt-5.3-codex": ModelPricing(1.75, 14.0, 1.75, 0.175),
    # gpt-5.4 (2026-03-05): $2.50/M input, $0.25/M cached, $15/M output.
    "gpt-5.4": ModelPricing(2.5, 15.0, 2.5, 0.25),
    # gpt-5.5: $5/M input, $0.50/M cached, $30/M output.
    # CAVEAT: this flat rate does NOT model gpt-5.5's long-context surcharge
    # (2x input / 1.5x output once a session exceeds 272K input tokens), so cost
    # for very-large-context runs reads low. Fine for typical eval tasks; revisit
    # if benchmarking large-context Codex runs.
    "gpt-5.5": ModelPricing(5.0, 30.0, 5.0, 0.50),
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
