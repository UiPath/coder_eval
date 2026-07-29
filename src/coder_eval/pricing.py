"""Model pricing for cost calculation.

Anthropic/OpenAI built-in rates; plugins contribute additional rates via
``register_pricing()``. Prices are per million tokens (MTok).
Source: https://www.anthropic.com/pricing
"""

from collections.abc import Iterable
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
    # Claude 5 Sonnet (2026-06-30 on Bedrock): standard $3/$15 (promo $2/$10 thru 2026-08-31).
    "claude-sonnet-5": ModelPricing(3.0, 15.0, 3.75, 0.30),
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
    # gpt-5.5-pro / gpt-5.4-pro: $30/M in, $180/M out; pro tiers offer NO prompt
    # caching (cache_read nominal, never billed since pro doesn't cache).
    "gpt-5.5-pro": ModelPricing(30.0, 180.0, 30.0, 3.0),
    "gpt-5.4-pro": ModelPricing(30.0, 180.0, 30.0, 3.0),
    # gpt-5.4 economy tiers: mini $0.75/$4.50, nano $0.20/$1.25.
    "gpt-5.4-mini": ModelPricing(0.75, 4.5, 0.75, 0.075),
    "gpt-5.4-nano": ModelPricing(0.20, 1.25, 0.20, 0.02),
    # GPT-5.6 family (2026-07-09): sol flagship / terra balanced (Codex default) / luna
    # economy. Terra matches gpt-5.4's rate; sol matches gpt-5.5.
    "gpt-5.6-sol": ModelPricing(5.0, 30.0, 6.25, 0.50),
    "gpt-5.6-terra": ModelPricing(2.5, 15.0, 3.125, 0.25),
    "gpt-5.6-luna": ModelPricing(1.0, 6.0, 1.25, 0.10),
    # Google Gemini (AntigravityAgent, via the Gemini Developer API). Per-MTok
    # rates from ai.google.dev/gemini-api/docs/pricing (2026). Gemini bills no
    # separate cache-WRITE fee, so cache_write == input (the agent maps
    # cache_creation_tokens to 0, so this value is effectively unused); cache_read
    # is the cached-input rate (~10% of input). CAVEAT: Pro's >200K-token tier is
    # higher ($4/$18); this flat rate reads low for very-large-context runs — fine
    # for typical eval tasks. Keyed on the bare model id in agent.model.
    "gemini-3-pro-preview": ModelPricing(2.0, 12.0, 2.0, 0.20),
    "gemini-3.1-pro-preview": ModelPricing(2.0, 12.0, 2.0, 0.20),
    "gemini-3.1-pro-preview-customtools": ModelPricing(2.0, 12.0, 2.0, 0.20),
    "gemini-3.5-flash": ModelPricing(1.5, 9.0, 1.5, 0.15),
    "gemini-3-flash-preview": ModelPricing(1.5, 9.0, 1.5, 0.15),
    # Open-weight models on Bedrock (eu-north-1), driven via the LiteLLM backend.
    # Bedrock lists no prompt-cache read/write rate for these, so cache-creation
    # is priced at the input rate and cache-read at 0 (conservative — see the
    # per-provider cost-accounting caveat; revisit against the AWS model cards).
    "deepseek.v3.2": ModelPricing(0.74, 2.22, 0.74, 0.0),
    "zai.glm-5": ModelPricing(1.2, 3.84, 1.2, 0.0),
    "moonshotai.kimi-k2.5": ModelPricing(0.72, 3.6, 0.72, 0.0),
    # OpenRouter models (cost-optimization path). These providers cache prompt
    # prefixes IMPLICITLY (no cache_control, no cache-write fee), so cache-creation
    # is priced at input (unused — cache_creation_tokens is always 0) and cache-read
    # at OpenRouter's published input_cache_read rate. Rates per OpenRouter's
    # /models endpoint (per-token x 1e6).
    "moonshotai/kimi-k3": ModelPricing(3.0, 15.0, 3.0, 0.30),
    "z-ai/glm-5.2": ModelPricing(0.826, 2.596, 0.826, 0.1534),
    "deepseek/deepseek-v4-pro": ModelPricing(0.435, 0.87, 0.435, 0.003625),
}


# Plugin-contributed rates (e.g. coder_eval_uipath registers UiPath models).
# Merged over the built-in table at lookup time.
_REGISTERED_PRICING: dict[str, ModelPricing] = {}


def _lookup_rate(key: str) -> ModelPricing | None:
    """Resolve a (normalized) pricing key: plugin overlay first, then built-ins.

    Uses ``is not None`` rather than truthiness so a later ``__bool__`` on
    ``ModelPricing`` (or a type change) can't make a falsy-but-present rate fall
    through to the built-in table.
    """
    registered = _REGISTERED_PRICING.get(key)
    return registered if registered is not None else _PRICING.get(key)


def register_pricing(rates: dict[str, ModelPricing]) -> None:
    """Merge plugin model rates into the pricing table.

    Re-registering an identical rate for an existing key is idempotent; a
    *conflicting* rate raises (reproducibility — mirrors AgentRegistry's
    anti-shadow rule, so load order can't change a model's price).

    All-or-nothing: every key is validated against the existing table before
    any is committed, so a conflict on a later key in a multi-model batch does
    not leave earlier keys half-registered.
    """
    for key, rate in rates.items():
        existing = _lookup_rate(key)
        if existing is not None and existing != rate:
            raise ValueError(
                f"pricing for {key!r} already registered as {existing}; refusing to shadow with {rate}. "
                + "A built-in or another plugin already prices this model — check plugin load order "
                + "(two plugins must not register conflicting rates for the same model id)."
            )
    _REGISTERED_PRICING.update(rates)


# Bedrock cross-region inference-profile prefixes (mirrors
# models.routing._BEDROCK_KNOWN_PREFIXES). A Bedrock route qualifies a bare
# alias into e.g. ``eu.anthropic.claude-opus-4-8``; the pricing table is keyed
# on the bare alias, so we strip these back off before the lookup.
_BEDROCK_REGION_PREFIXES: tuple[str, ...] = ("eu.", "us.", "apac.", "global.")


def _normalize_model(model: str) -> str:
    """Strip Bedrock region + vendor qualifiers back to the bare pricing key.

    ``eu.anthropic.claude-opus-4-8`` → ``claude-opus-4-8``. Idempotent on
    already-bare aliases (``claude-opus-4-8`` / ``gpt-5-codex`` pass through),
    so it is safe to apply unconditionally for every route.
    """
    model = model.strip()
    # LiteLLM/Bedrock routing prefixes (e.g. "converse/zai.glm-5",
    # "bedrock/converse/deepseek.v3.2") → bare model id.
    for routing_prefix in ("bedrock/converse/", "bedrock/", "converse/"):
        if model.startswith(routing_prefix):
            model = model[len(routing_prefix) :]
            break
    for prefix in _BEDROCK_REGION_PREFIXES:
        if model.startswith(prefix):
            model = model[len(prefix) :]
            break
    if model.startswith("anthropic."):
        model = model[len("anthropic.") :]
    return model


def is_priced(model: str) -> bool:
    """Whether the rate card can price this model (after prefix normalization).

    The rate card is a static table, so a model that shipped after the installed
    framework version has no rate. It is the fallback for turns the agent's own
    backend never priced (timed-out and killed partials), so an unpriced model
    means those turns book their tokens against no money. Call this at run start
    to make that a warning instead of a total that reads low with nothing on the
    report to say so.
    """
    return _lookup_rate(_normalize_model(model)) is not None


def unpriced_models(models: Iterable[str | None]) -> list[str]:
    """Sorted, de-duplicated models from ``models`` that the rate card can't price.

    Falsy entries are dropped: a task with no pinned model resolves its model at
    the route level, which this pre-flight check cannot see.
    """
    return sorted({m for m in models if m and not is_priced(m)})


def calculate_cost(
    model: str,
    uncached_input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float | None:
    """Calculate cost in USD for the given token usage.

    ``uncached_input_tokens`` is the fresh slice billed at the input rate — pass
    ``TokenUsage.uncached_input_tokens``, NOT ``input_tokens`` (the derived total,
    which already includes the cache buckets and would double-count them).

    The model id is normalized (Bedrock region/vendor prefixes stripped) so a
    qualified inference-profile id like ``eu.anthropic.claude-opus-4-8`` prices
    the same as the bare ``claude-opus-4-8``. Returns None if the (normalized)
    model is not in the pricing table.
    """
    pricing = _lookup_rate(_normalize_model(model))
    if pricing is None:
        return None

    return (
        uncached_input_tokens * pricing.input_per_mtok
        + output_tokens * pricing.output_per_mtok
        + cache_creation_tokens * pricing.cache_write_per_mtok
        + cache_read_tokens * pricing.cache_read_per_mtok
    ) / 1_000_000
