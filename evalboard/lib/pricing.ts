// Per-million-token prices and cost math.
//
// Rates are NOT written here: they are generated from the authoritative Python
// table (src/coder_eval/pricing.py) into lib/pricing.generated.ts. To change a
// rate, edit that file and run `pnpm gen:pricing` — never hand-edit a rate on
// this side. lib/__tests__/pricing-parity.test.ts fails the build if the
// generated artifact drifts from pricing.py.
//
// This module is the single source of truth for rates *on the frontend*: the
// cascade-aware thinking-cost simulator (lib/thinkingSim.ts) and the
// per-message cost column (lib/runs.ts) both price against this table, so a
// model added or repriced in pricing.py updates both at once.

import { GENERATED_PRICING } from "./pricing.generated";
import type { Pricing } from "./pricing-types";

export type { Pricing } from "./pricing-types";

// Exported so the parity test can assert the generated artifact is current
// against pricing.py. Not part of the consumer API; use resolvePricing() instead.
export const PRICING: Record<string, Pricing> = GENERATED_PRICING;

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
