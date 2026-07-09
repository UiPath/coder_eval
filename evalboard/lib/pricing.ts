// Per-million-token prices and cost math. Ported from
// src/coder_eval/proxy/pricing.py — keep in sync when that table changes.
// Source: Anthropic / OpenAI public pricing.
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
// authoritative Python table (src/coder_eval/proxy/pricing.py) and fail the
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

// Resolve pricing for a model id, tolerating undated aliases (the recorded
// model is usually the canonical id like "claude-sonnet-4-6", but be lenient
// about a trailing date suffix). Matches the Python source's exact-match
// semantics, plus a date-suffix strip — deliberately NO loose prefix match: a
// substring fallback would silently price `gpt-5-mini` at full `gpt-5` rates,
// presenting a multi-x overcharge as an authoritative-looking figure. Unknown
// ids return null (render "—") rather than a wrong number.
//
// Object.hasOwn (not `PRICING[model]` truthiness) guards against a degenerate
// id like "constructor"/"toString" resolving to an inherited prototype member.
export function resolvePricing(model: string | null): Pricing | null {
    if (!model) return null;
    if (Object.hasOwn(PRICING, model)) return PRICING[model];
    // Try stripping a trailing -YYYYMMDD date.
    const undated = model.replace(/-\d{8}$/, "");
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
