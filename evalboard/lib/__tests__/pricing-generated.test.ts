import { describe, expect, test } from "vitest";
import { PRICING, resolvePricing } from "../pricing";

// Consumption-side guard on the GENERATED rate table (lib/pricing.generated.ts).
// CE065 on the Python side proves the file matches src/coder_eval/pricing.py;
// this proves the evalboard actually consumes it, so a truncated, empty or
// wrongly-shaped generated file fails `pnpm verify` rather than silently
// rendering "—" for every cost. That silent-narrowing failure mode is exactly
// what the deleted hand-copy parity test shipped.

describe("generated pricing table", () => {
    test("is not silently narrow", () => {
        // 59 built-in Python rows minus the 3 per_request_billing ones.
        expect(Object.keys(PRICING).length).toBeGreaterThanOrEqual(56);
    });

    test("each field carries its own rate", () => {
        // A FROZEN legacy key with four distinct rates: it pins the
        // Python-field -> TS-field mapping without re-creating the two-file
        // edit that generating the table exists to remove. Never pin a live,
        // actively-repriced model here.
        expect(PRICING["claude-3-haiku-20240307"]).toEqual({
            inputPerMTok: 0.25,
            outputPerMTok: 1.25,
            cacheWritePerMTok: 0.3,
            cacheReadPerMTok: 0.03,
        });
    });

    test("prefix stripping still resolves through the generated table", () => {
        expect(resolvePricing("eu.anthropic.claude-opus-4-8")).not.toBeNull();
    });

    test("per_request_billing models stay unpriced so runs.ts apportions the real bill", () => {
        expect(resolvePricing("moonshotai/kimi-k3")).toBeNull();
        expect(resolvePricing("z-ai/glm-5.2")).toBeNull();
        expect(resolvePricing("deepseek/deepseek-v4-pro")).toBeNull();
    });

    test("previously-unmirrored drift entries now resolve", () => {
        // gpt-5.5-pro sat in the old hand-copy's exemption set as
        // bookkeeping, not a product rule — so it rendered "—" for cost.
        // Generating the table is what fixes that; pin the fix, not the
        // status quo it replaced.
        // Resolving at all IS the fix; the rates themselves are CE065's job,
        // so pinning them here would be the same two-file coupling again.
        for (const id of ["gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.4-pro", "gpt-5.5-pro"]) {
            expect(resolvePricing(id), `${id} should resolve`).not.toBeNull();
        }
    });
});
