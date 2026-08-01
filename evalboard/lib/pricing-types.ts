// The per-MTok rate shape, shared by the generated table
// (lib/pricing.generated.ts) and the hand-written lookup/cost helpers in
// lib/pricing.ts. It lives in its own leaf module so the generated file can
// import it without a cycle (pricing.ts → generated → pricing.ts).
// Consumers should keep importing `Pricing` from lib/pricing.ts.

export interface Pricing {
    inputPerMTok: number;
    outputPerMTok: number;
    cacheWritePerMTok: number;
    cacheReadPerMTok: number;
}
