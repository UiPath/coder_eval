// Per-million-token prices and cost math. The rate table itself is GENERATED
// from src/coder_eval/pricing.py by `make pricing-mirror` — see ./pricing.generated.
// Source: Anthropic / OpenAI / Google public pricing.
//
// This is the frontend's single entry point for rates: the cascade-aware
// thinking-cost simulator (lib/thinkingSim.ts) and the per-message cost column
// (lib/runs.ts) both price through resolvePricing, so a model added or repriced
// in pricing.py reaches both at once once the mirror is regenerated.

import { PRICING } from "./pricing.generated";

export interface Pricing {
    inputPerMTok: number;
    outputPerMTok: number;
    cacheWritePerMTok: number;
    cacheReadPerMTok: number;
}

// The rate table itself is GENERATED from the authoritative Python table
// (src/coder_eval/pricing.py) by `make pricing-mirror`, and CE065 fails the
// build if the generated file drifts from it. Never hand-edit a rate here or in
// pricing.generated.ts — change pricing.py and regenerate.
//
// Models flagged `per_request_billing` in pricing.py are deliberately absent
// from the generated table: the provider routes per request, so a static
// headline rate is wrong. resolvePricing returns null for them and runs.ts
// apportions the captured ACTUAL per-call cost instead.
//
// Re-exported so the rate table has one import path for the whole frontend
// regardless of which file generates it. Not part of the consumer API; use
// resolvePricing() instead.
export { PRICING };

// Strip the LiteLLM/Bedrock routing + region/vendor prefixes back to the bare
// pricing key — mirror of src/coder_eval/pricing.py::_normalize_model, since the
// recorded model_used arrives qualified (e.g. "converse/zai.glm-5",
// "eu.anthropic.claude-sonnet-4-6"). Idempotent on already-bare ids.
// "openrouter/" is here for the same reason it is in _normalize_model: a harness
// that addresses OpenRouter natively (OpenCode) records the model WITH its
// provider prefix ("openrouter/deepseek/deepseek-v4-pro"), while the rate keys
// are the bare vendor/model ids the LiteLLM route records. Without the strip the
// same model normalizes differently depending on which harness produced the run.
const _ROUTING_PREFIXES = [
    "bedrock/converse/",
    "bedrock/",
    "converse/",
    "openrouter/",
];
const _REGION_PREFIXES = ["eu.", "us.", "apac.", "global."];
// Exported so the run header's model tally groups on the same key pricing looks
// up on (lib/runs.ts::tallyModels).
export function normalizeModel(model: string): string {
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
