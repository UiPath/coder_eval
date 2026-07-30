// Per-million-token prices and cost math. Ported from
// src/coder_eval/pricing.py — keep in sync when that table changes.
// Source: Anthropic / OpenAI / Google public pricing.
//
// This is the single source of truth for rates on the frontend: the
// cascade-aware thinking-cost simulator (lib/thinkingSim.ts) and the
// per-message cost column (lib/runs.ts) both price against this table, so a
// model added or repriced here updates both at once.

export interface Pricing {
    inputPerMTok: number;
    outputPerMTok: number;
    cacheWritePerMTok: number;
    cacheReadPerMTok: number;
}

// Exported so a unit test can assert key-and-rate parity against the
// authoritative Python table (src/coder_eval/pricing.py) and fail the
// build on drift — this hand-copied mirror is otherwise guarded only by a
// comment. Not part of the consumer API; use resolvePricing() instead.
export const PRICING: Record<string, Pricing> = {
    "claude-opus-4-8": p(15, 75, 18.75, 1.5),
    "claude-opus-4-7": p(15, 75, 18.75, 1.5),
    "claude-opus-4-6": p(15, 75, 18.75, 1.5),
    "claude-opus-4-6-20250514": p(15, 75, 18.75, 1.5),
    "claude-opus-4-5-20251101": p(15, 75, 18.75, 1.5),
    "claude-opus-4-20250514": p(15, 75, 18.75, 1.5),
    "claude-sonnet-4-6": p(3, 15, 3.75, 0.3),
    "claude-sonnet-4-6-20250514": p(3, 15, 3.75, 0.3),
    "claude-sonnet-4-5-20250929": p(3, 15, 3.75, 0.3),
    "claude-sonnet-4-20250514": p(3, 15, 3.75, 0.3),
    "claude-haiku-4-5-20251001": p(0.8, 4, 1, 0.08),
    "claude-3-7-sonnet-20250219": p(3, 15, 3.75, 0.3),
    "claude-3-5-sonnet-20241022": p(3, 15, 3.75, 0.3),
    "claude-3-5-sonnet-20240620": p(3, 15, 3.75, 0.3),
    "claude-3-opus-20240229": p(15, 75, 18.75, 1.5),
    "claude-3-sonnet-20240229": p(3, 15, 3.75, 0.3),
    "claude-3-haiku-20240307": p(0.25, 1.25, 0.3, 0.03),
    "gpt-5-codex": p(1.25, 10, 1.25, 0.125),
    "gpt-5": p(1.25, 10, 1.25, 0.125),
    "gpt-5.3-codex": p(1.75, 14, 1.75, 0.175),
    "gpt-5.4": p(2.5, 15, 2.5, 0.25),
    "gpt-5.5": p(5, 30, 5, 0.5),
    // Google Gemini (AntigravityAgent). Gemini bills no separate cache-write
    // fee (cache_write == input, effectively unused); cache_read is the cached-
    // input rate. Pro's >200K-token tier is higher — this flat rate reads low
    // for very-large-context runs, fine for typical eval tasks.
    "gemini-3-pro-preview": p(2, 12, 2, 0.2),
    "gemini-3.1-pro-preview": p(2, 12, 2, 0.2),
    "gemini-3.1-pro-preview-customtools": p(2, 12, 2, 0.2),
    "gemini-3.5-flash": p(1.5, 9, 1.5, 0.15),
    "gemini-3-flash-preview": p(1.5, 9, 1.5, 0.15),
    // OpenRouter open-weight models (litellm backend). Mirror of pricing.py;
    // these providers cache implicitly (cache_write == input, unused) so only
    // cache_read carries a discounted rate. NOTE: OpenRouter routes per-request,
    // so these rates are only accurate when the litellm config pins the provider
    // (sort: price) — otherwise the billed rate can differ from the headline.
    "moonshotai/kimi-k3": p(3, 15, 3, 0.3),
    "z-ai/glm-5.2": p(0.826, 2.596, 0.826, 0.1534),
    "deepseek/deepseek-v4-pro": p(0.435, 0.87, 0.435, 0.003625),
    // Bedrock open-weight models (litellm backend, eu-north-1). Mirror of pricing.py.
    // The recorded model_used arrives prefixed (e.g. "converse/zai.glm-5"), so
    // resolvePricing strips the routing/region prefixes before lookup.
    "deepseek.v3.2": p(0.74, 2.22, 0.74, 0),
    "zai.glm-5": p(1.2, 3.84, 1.2, 0),
    "moonshotai.kimi-k2.5": p(0.72, 3.6, 0.72, 0),
};

function p(
    input: number,
    output: number,
    cacheWrite: number,
    cacheRead: number,
): Pricing {
    return {
        inputPerMTok: input,
        outputPerMTok: output,
        cacheWritePerMTok: cacheWrite,
        cacheReadPerMTok: cacheRead,
    };
}

// Strip the LiteLLM/Bedrock routing + region/vendor prefixes back to the bare
// pricing key — mirror of src/coder_eval/pricing.py::_normalize_model, since the
// recorded model_used arrives qualified (e.g. "converse/zai.glm-5",
// "eu.anthropic.claude-sonnet-4-6"). Idempotent on already-bare ids.
const _ROUTING_PREFIXES = ["bedrock/converse/", "bedrock/", "converse/"];
const _REGION_PREFIXES = ["eu.", "us.", "apac.", "global."];
function normalizeModel(model: string): string {
    let m = model.trim();
    for (const pre of _ROUTING_PREFIXES) {
        if (m.startsWith(pre)) {
            m = m.slice(pre.length);
            break;
        }
    }
    for (const pre of _REGION_PREFIXES) {
        if (m.startsWith(pre)) {
            m = m.slice(pre.length);
            break;
        }
    }
    if (m.startsWith("anthropic.")) m = m.slice("anthropic.".length);
    return m;
}

// Resolve pricing for a model id, tolerating routing/region prefixes and undated
// aliases (the recorded model is usually the canonical id like "claude-sonnet-4-6",
// but LiteLLM/Bedrock runs record it prefixed, and some carry a trailing date).
// Deliberately NO loose *substring* match: that would silently price `gpt-5-mini`
// at full `gpt-5` rates, presenting a multi-x overcharge as an authoritative-looking
// figure. Unknown ids return null (render "—") rather than a wrong number.
//
// Object.hasOwn (not `PRICING[model]` truthiness) guards against a degenerate
// id like "constructor"/"toString" resolving to an inherited prototype member.
export function resolvePricing(model: string | null): Pricing | null {
    if (!model) return null;
    const norm = normalizeModel(model);
    if (Object.hasOwn(PRICING, norm)) return PRICING[norm];
    // Try stripping a trailing -YYYYMMDD date.
    const undated = norm.replace(/-\d{8}$/, "");
    if (Object.hasOwn(PRICING, undated)) return PRICING[undated];
    return null;
}

// Estimated USD value of a single token bucket (one column), priced against the
// model's list rates. Powers the Tokens↔USD column toggle. Returns null when
// the model is unpriced or the token count is missing — null renders as "—".
export type TokenKind = "input" | "output" | "cacheWrite" | "cacheRead";

// Each token kind maps to exactly one rate field. Declared as a
// Record<TokenKind, …> so adding a TokenKind without a rate is a compile error
// (the old nested ternary fell a new kind through to the input rate silently).
const RATE_FIELD: Record<TokenKind, keyof Pricing> = {
    input: "inputPerMTok",
    output: "outputPerMTok",
    cacheWrite: "cacheWritePerMTok",
    cacheRead: "cacheReadPerMTok",
};

export function tokenBucketUsd(
    model: string | null,
    tokens: number | null,
    kind: TokenKind,
): number | null {
    const pricing = resolvePricing(model);
    if (!pricing || tokens == null) return null;
    return (tokens * pricing[RATE_FIELD[kind]]) / 1_000_000;
}

// True per-message cost in USD from the recorded per-message token buckets and
// model, priced against the same rate table the Python backend uses. This is
// the rate-accurate cost of the single API call the message represents — the
// SDK only reports a cumulative per-turn `total_cost_usd`, never per message.
//
// Returns null when the model isn't in the rate table (can't price it) or when
// no token figure was recorded for the message (older runs predating
// per-message tokens) — null renders as "—" rather than a misleading $0.00.
export function messageCostUsd(usage: {
    model: string | null;
    inputTokens: number | null;
    outputTokens: number | null;
    cacheWriteTokens: number | null;
    cacheReadTokens: number | null;
}): number | null {
    const pricing = resolvePricing(usage.model);
    if (!pricing) return null;
    const { inputTokens, outputTokens, cacheWriteTokens, cacheReadTokens } =
        usage;
    if (
        inputTokens == null &&
        outputTokens == null &&
        cacheWriteTokens == null &&
        cacheReadTokens == null
    ) {
        return null;
    }
    return (
        ((inputTokens ?? 0) * pricing.inputPerMTok +
            (outputTokens ?? 0) * pricing.outputPerMTok +
            (cacheWriteTokens ?? 0) * pricing.cacheWritePerMTok +
            (cacheReadTokens ?? 0) * pricing.cacheReadPerMTok) /
        1_000_000
    );
}
