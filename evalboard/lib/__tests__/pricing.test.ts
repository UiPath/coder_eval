import { describe, expect, test } from "vitest";
import { messageCostUsd, resolvePricing, tokenBucketUsd } from "../pricing";

describe("resolvePricing", () => {
    test("resolves a canonical model id", () => {
        expect(resolvePricing("claude-sonnet-4-6")?.outputPerMTok).toBe(15);
    });

    test("tolerates a trailing date suffix", () => {
        expect(resolvePricing("claude-sonnet-4-6-20991231")?.outputPerMTok).toBe(
            15,
        );
    });

    test("returns null for unknown / missing models", () => {
        expect(resolvePricing("definitely-not-a-model")).toBeNull();
        expect(resolvePricing(null)).toBeNull();
    });

    test("does NOT loosely prefix-match a cheaper variant to a pricier family", () => {
        // gpt-5-mini / gpt-5-nano are not in the table and must NOT inherit
        // full gpt-5 rates via a substring match — that would silently
        // overcharge by a multiple. Unknown → null (renders "—").
        expect(resolvePricing("gpt-5-mini")).toBeNull();
        expect(resolvePricing("gpt-5-nano")).toBeNull();
        // And a partial of a known id is not "rounded up" to it either.
        expect(resolvePricing("claude-opus")).toBeNull();
    });

    test("ignores object-prototype keys (no NaN from degenerate ids)", () => {
        // Object.hasOwn guards against "constructor"/"toString" resolving to an
        // inherited member instead of returning null.
        expect(resolvePricing("constructor")).toBeNull();
        expect(resolvePricing("toString")).toBeNull();
        expect(resolvePricing("__proto__")).toBeNull();
    });

    test("knows the current default opus id, at the repriced tier", () => {
        // Opus 4.5 REPRICED the family from $15/$75 to $5/$25 per Mtok. The two
        // generations differ 3x, so pin the boundary: an id on the wrong side of
        // it triples (or thirds) every Opus cost the board renders, which reads
        // as a plausible number rather than an obvious error.
        // pricing-parity.test.ts is the authority on the rates themselves; this
        // asserts the split survives an edit to the table.
        expect(resolvePricing("claude-opus-4-8")?.outputPerMTok).toBe(25);
        expect(resolvePricing("claude-opus-5")?.outputPerMTok).toBe(25);
        expect(resolvePricing("claude-opus-4-1")?.outputPerMTok).toBe(75);
    });

    test("strips LiteLLM/Bedrock routing + region prefixes (recorded model_used is qualified)", () => {
        // The recorded model arrives prefixed on litellm/Bedrock runs; without the
        // strip these rendered "—" for the whole cost column.
        expect(resolvePricing("converse/zai.glm-5")?.outputPerMTok).toBe(3.84);
        expect(resolvePricing("bedrock/converse/deepseek.v3.2")?.inputPerMTok).toBe(0.74);
        expect(resolvePricing("eu.anthropic.claude-sonnet-4-6")?.outputPerMTok).toBe(15);
    });
});

describe("tokenBucketUsd", () => {
    test("prices each bucket at its own rate", () => {
        // claude-sonnet-4-6: input 3, output 15, cacheWrite 3.75, cacheRead 0.3.
        expect(tokenBucketUsd("claude-sonnet-4-6", 2000, "output")).toBeCloseTo(
            0.03,
            9,
        );
        expect(
            tokenBucketUsd("claude-sonnet-4-6", 80_000, "cacheRead"),
        ).toBeCloseTo(0.024, 9);
        expect(
            tokenBucketUsd("claude-sonnet-4-6", 1000, "cacheWrite"),
        ).toBeCloseTo(0.00375, 9);
        expect(tokenBucketUsd("claude-sonnet-4-6", 1000, "input")).toBeCloseTo(
            0.003,
            9,
        );
    });

    test("returns null for an unpriced model or missing token count", () => {
        expect(tokenBucketUsd(null, 1000, "output")).toBeNull();
        expect(tokenBucketUsd("nope", 1000, "output")).toBeNull();
        expect(tokenBucketUsd("claude-sonnet-4-6", null, "output")).toBeNull();
    });
});

describe("messageCostUsd", () => {
    test("prices the four token buckets against the model's rates", () => {
        // claude-sonnet-4-6: input 3, output 15, cacheWrite 3.75, cacheRead 0.3 /MTok.
        // (1000·3 + 2000·15 + 500·3.75 + 10000·0.3) / 1e6 = 0.037875
        const cost = messageCostUsd({
            model: "claude-sonnet-4-6",
            inputTokens: 1000,
            outputTokens: 2000,
            cacheWriteTokens: 500,
            cacheReadTokens: 10000,
        });
        expect(cost).toBeCloseTo(0.037875, 9);
    });

    test("treats null token buckets as zero when at least one is present", () => {
        // Only output recorded: 2000 · 15 / 1e6 = 0.03
        const cost = messageCostUsd({
            model: "claude-sonnet-4-6",
            inputTokens: null,
            outputTokens: 2000,
            cacheWriteTokens: null,
            cacheReadTokens: null,
        });
        expect(cost).toBeCloseTo(0.03, 9);
    });

    test("returns null for an unpriced model (no misleading $0.00)", () => {
        expect(
            messageCostUsd({
                model: "some-future-model",
                inputTokens: 1000,
                outputTokens: 1000,
                cacheWriteTokens: 0,
                cacheReadTokens: 0,
            }),
        ).toBeNull();
    });

    test("returns null when no token figure was recorded (old runs)", () => {
        expect(
            messageCostUsd({
                model: "claude-sonnet-4-6",
                inputTokens: null,
                outputTokens: null,
                cacheWriteTokens: null,
                cacheReadTokens: null,
            }),
        ).toBeNull();
    });

    test("returns null when the model is missing (old runs)", () => {
        expect(
            messageCostUsd({
                model: null,
                inputTokens: 100,
                outputTokens: 100,
                cacheWriteTokens: 0,
                cacheReadTokens: 0,
            }),
        ).toBeNull();
    });
});
