"""Model pricing for cost calculation.

Anthropic/OpenAI/Google built-in rates; plugins contribute additional rates via
``register_pricing()``. Prices are per million tokens (MTok).
Sources: https://platform.claude.com/docs/en/about-claude/pricing,
https://developers.openai.com/api/docs/pricing,
https://ai.google.dev/gemini-api/docs/pricing, and OpenRouter's live
``/api/v1/models`` (every row re-verified 2026-09-03, except the Bedrock
open-weight block: AWS publishes no eu-north-1 figures for those three).

SSOT for rates on both halves of the repo: ``evalboard/lib/pricing.generated.ts`` is
generated from it by ``make pricing-mirror`` and guarded by CE065. Never hand-edit the
generated file.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class ModelPricing:
    """Pricing for a single model (per million tokens).

    ``per_request_billing`` marks a provider that bills per request, so this
    static rate is a last-resort estimate retained for the ``max_usd`` pre-flight
    only. A frontend that can show the actual captured per-call cost must not
    price such a model statically — an estimate would replace a real figure.
    """

    input_per_mtok: float
    output_per_mtok: float
    cache_write_per_mtok: float  # prompt caching write
    cache_read_per_mtok: float  # prompt caching read
    per_request_billing: bool = False


# Models the Delegate agent serves, under the SDK's own hyphenated ids -- a
# dict of its own (not inlined into _PRICING) so DELEGATE_MODEL_IDS below can
# be derived rather than hand-copied. gpt-5-4/5-5/5-6-sol's rates are NOT typos
# -- see .claude/notes/agents.md § Delegate agent pricing before "fixing" them.
_DELEGATE_PRICING: dict[str, ModelPricing] = {
    "virtuoso-1-5": ModelPricing(0.95, 4.0, 0.0, 0.16),
    "virtuoso-2-0": ModelPricing(0.95, 4.0, 0.0, 0.19),
    "gemini-3-5-flash": ModelPricing(1.50, 9.0, 0.0, 0.15),
    "gemini-3-6-flash": ModelPricing(1.50, 7.50, 0.0, 0.15),
    "gemini-3-1-pro-preview": ModelPricing(2.0, 12.0, 0.0, 0.20),
    "gpt-5-4": ModelPricing(2.50, 15.0, 15.0, 0.25),
    "gpt-5-5": ModelPricing(5.0, 30.0, 30.0, 0.50),
    "gpt-5-6-sol": ModelPricing(5.0, 30.0, 6.25, 0.50),
    "gpt-5-6-terra": ModelPricing(2.0, 12.0, 2.50, 0.20),
    "gpt-5-6-luna": ModelPricing(0.20, 1.20, 0.25, 0.02),
    "kimi-k2-7-code": ModelPricing(0.95, 4.0, 0.0, 0.19),
    "glm-5-3-flash": ModelPricing(0.15, 0.50, 0.0, 0.03),
}

DELEGATE_MODEL_IDS: frozenset[str] = frozenset(_DELEGATE_PRICING)
"""Model ids the Delegate agent serves, for a test/doc that needs the real set
rather than a second hand-copied literal that could drift from ``_DELEGATE_PRICING``."""


_PRICING: dict[str, ModelPricing] = {
    # Fable 5.1 (and Mythos 5.1) price cache hits at 0.025x input, not the 0.1x
    # every other Claude model uses. Fable 5 pays $1 on the identical $10 base.
    "claude-fable-5-1": ModelPricing(10.0, 50.0, 12.50, 0.25),
    "claude-fable-5": ModelPricing(10.0, 50.0, 12.50, 1.0),
    # Opus 4.5 and later dropped to $5/$25; 4.1 and 4 keep the old $15/$75. The
    # version boundary is the price boundary: a newer Opus is not the dearer one.
    "claude-opus-5": ModelPricing(5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-8": ModelPricing(5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-7": ModelPricing(5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-6": ModelPricing(5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-5": ModelPricing(5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-5-20251101": ModelPricing(5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-1": ModelPricing(15.0, 75.0, 18.75, 1.50),
    "claude-opus-4": ModelPricing(15.0, 75.0, 18.75, 1.50),
    "claude-opus-4-20250514": ModelPricing(15.0, 75.0, 18.75, 1.50),
    # $2/$10, NOT the $3/$15 that Sonnet 4.6 and earlier pay. Do not copy the 4.x row onto it.
    "claude-sonnet-5": ModelPricing(2.0, 10.0, 2.50, 0.20),
    "claude-sonnet-4-6": ModelPricing(3.0, 15.0, 3.75, 0.30),
    "claude-sonnet-4-5": ModelPricing(3.0, 15.0, 3.75, 0.30),
    "claude-sonnet-4-5-20250929": ModelPricing(3.0, 15.0, 3.75, 0.30),
    "claude-sonnet-4-20250514": ModelPricing(3.0, 15.0, 3.75, 0.30),
    # $1/$5. Not $0.80/$4 — those are Haiku 3.5's rates.
    "claude-haiku-4-5": ModelPricing(1.0, 5.0, 1.25, 0.10),
    "claude-haiku-4-5-20251001": ModelPricing(1.0, 5.0, 1.25, 0.10),
    "claude-haiku-3-5": ModelPricing(0.80, 4.0, 1.0, 0.08),
    "claude-3-7-sonnet-20250219": ModelPricing(3.0, 15.0, 3.75, 0.30),
    "claude-3-5-sonnet-20241022": ModelPricing(3.0, 15.0, 3.75, 0.30),
    "claude-3-5-sonnet-20240620": ModelPricing(3.0, 15.0, 3.75, 0.30),
    "claude-3-opus-20240229": ModelPricing(15.0, 75.0, 18.75, 1.50),
    "claude-3-sonnet-20240229": ModelPricing(3.0, 15.0, 3.75, 0.30),
    "claude-3-haiku-20240307": ModelPricing(0.25, 1.25, 0.30, 0.03),
    # OpenAI GPT-5 / Codex (direct or Azure). No cache-write fee: cache_write == input below.
    "gpt-5-codex": ModelPricing(1.25, 10.0, 1.25, 0.125),
    "gpt-5": ModelPricing(1.25, 10.0, 1.25, 0.125),
    "gpt-5.1-codex-max": ModelPricing(1.25, 10.0, 1.25, 0.125),
    "gpt-5.1-codex": ModelPricing(1.25, 10.0, 1.25, 0.125),
    "gpt-5.1-codex-mini": ModelPricing(0.25, 2.0, 0.25, 0.025),
    # The one OpenAI entry whose cached rate is 25% of input, not 10%.
    "codex-mini-latest": ModelPricing(1.50, 6.0, 1.50, 0.375),
    "gpt-5.3-codex": ModelPricing(1.75, 14.0, 1.75, 0.175),
    "gpt-5.2-codex": ModelPricing(1.75, 14.0, 1.75, 0.175),
    "gpt-5.4": ModelPricing(2.5, 15.0, 2.5, 0.25),
    # CAVEAT: flat rate, so gpt-5.5's long-context surcharge (2x input / 1.5x
    # output past 272K input tokens) is not modelled and reads low.
    "gpt-5.5": ModelPricing(5.0, 30.0, 5.0, 0.50),
    # Pro tiers offer no prompt caching, so cache_read is nominal.
    "gpt-5.5-pro": ModelPricing(30.0, 180.0, 30.0, 3.0),
    "gpt-5.4-pro": ModelPricing(30.0, 180.0, 30.0, 3.0),
    "gpt-5.4-mini": ModelPricing(0.75, 4.5, 0.75, 0.075),
    "gpt-5.4-nano": ModelPricing(0.20, 1.25, 0.20, 0.02),
    # HAZARD: a single CURRENT-rate card with no effective date, so a repriced
    # model makes historical runs re-price at today's rate. Sol's rate is
    # promotional through at least 2026-11-21; re-check then.
    "gpt-5.6-sol": ModelPricing(4.0, 20.0, 4.0, 0.40),
    "gpt-5.6-terra": ModelPricing(2.0, 12.0, 2.0, 0.20),
    "gpt-5.6-luna": ModelPricing(0.20, 1.20, 0.20, 0.02),
    # Keyed on the literal ids ListModels returns. No cache-write fee, so
    # cache_write == input (unused). CAVEAT: Pro's >200K-token tier costs more, so
    # a very-large-context run reads LOW. List rates; discounted by half through
    # 2026-12-31.
    "gemini-3.8-flash": ModelPricing(1.5, 7.5, 1.5, 0.15),
    "gemini-3.7-flash": ModelPricing(1.5, 7.5, 1.5, 0.15),
    "gemini-3.6-flash": ModelPricing(1.5, 7.5, 1.5, 0.15),
    "gemini-3.5-flash": ModelPricing(1.5, 9.0, 1.5, 0.15),
    "gemini-3.5-flash-lite": ModelPricing(0.30, 2.5, 0.30, 0.03),
    "gemini-3.1-pro-preview": ModelPricing(2.0, 12.0, 2.0, 0.20),
    "gemini-3.1-pro-preview-customtools": ModelPricing(2.0, 12.0, 2.0, 0.20),
    "gemini-3.1-flash-lite": ModelPricing(0.25, 1.5, 0.25, 0.025),
    "gemini-3.1-flash-lite-preview": ModelPricing(0.25, 1.5, 0.25, 0.025),
    "gemini-3-flash-preview": ModelPricing(0.50, 3.0, 0.50, 0.05),
    # Off the public card; the last published rate keeps historical runs priced.
    "gemini-3-pro-preview": ModelPricing(2.0, 12.0, 2.0, 0.20),
    # HAZARD: eu-north-1 rates, a ~20% premium over us-east-1 -- do NOT "correct"
    # them against the US column. No published prompt-cache rate, so cache-creation
    # is priced at input and cache-read at 0.
    "deepseek.v3.2": ModelPricing(0.74, 2.22, 0.74, 0.0),
    "zai.glm-5": ModelPricing(1.2, 3.84, 1.2, 0.0),
    "moonshotai.kimi-k2.5": ModelPricing(0.72, 3.6, 0.72, 0.0),
    # These providers cache prefixes implicitly, so cache-creation is priced at
    # input (unused). HEADLINE rates only: OpenRouter routes per request, so the
    # real bill depends on the provider a call lands on -- which is why the litellm
    # path captures actual per-call cost and overrides these. Static fallback, and
    # why these three carry per_request_billing (the mirror omits them).
    # Rationale: .claude/notes/reporting.md § Cost joining
    "moonshotai/kimi-k3": ModelPricing(3.0, 15.0, 3.0, 0.30, per_request_billing=True),
    "z-ai/glm-5.2": ModelPricing(0.966, 3.036, 0.966, 0.1932, per_request_billing=True),
    "deepseek/deepseek-v4-pro": ModelPricing(1.030776, 2.061552, 1.030776, 0.085898, per_request_billing=True),
    **_DELEGATE_PRICING,
}


# Plugin-contributed rates, merged over the built-in table at lookup time.
_REGISTERED_PRICING: dict[str, ModelPricing] = {}


def builtin_rates() -> Mapping[str, ModelPricing]:
    """The built-in rate card, read-only.

    Excludes plugin rates (``register_pricing``), which are not resolvable
    outside a running process and are out-of-tree by design.
    """
    return MappingProxyType(_PRICING)


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


# Mirrors models.routing._BEDROCK_KNOWN_PREFIXES. A Bedrock route qualifies a bare
# alias; the table is keyed on the bare alias, so strip these before the lookup.
_BEDROCK_REGION_PREFIXES: tuple[str, ...] = ("eu.", "us.", "apac.", "global.")


def _normalize_model(model: str) -> str:
    """Strip Bedrock region + vendor qualifiers back to the bare pricing key.

    ``eu.anthropic.claude-opus-4-8`` → ``claude-opus-4-8``. Idempotent on
    already-bare aliases (``claude-opus-4-8`` / ``gpt-5-codex`` pass through),
    so it is safe to apply unconditionally for every route.
    """
    model = model.strip()
    # Routing prefixes -> bare model id. ``openrouter/`` is here because an agent
    # that addresses OpenRouter natively reports the model WITH its provider
    # prefix, while the rate-card keys are bare -- without the strip the same model
    # prices under LiteLLM and silently goes unpriced under OpenCode.
    for routing_prefix in ("bedrock/converse/", "bedrock/", "converse/", "openrouter/"):
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
    """Whether the rate card can price this model (after prefix normalization)."""
    return _lookup_rate(_normalize_model(model)) is not None


def unpriced_models(models: Iterable[str | None]) -> list[str]:
    """Sorted, de-duplicated models from ``models`` that the rate card can't price.

    Falsy entries are dropped: an unpinned model resolves at the route level, which
    a pre-flight check cannot see.
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
