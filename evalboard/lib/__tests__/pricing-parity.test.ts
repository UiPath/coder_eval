import { describe, expect, test } from "vitest";
import { excludedModels, mirroredTable, readTable } from "../../scripts/gen-pricing.mjs";
import { PRICING, resolvePricing } from "../pricing";

// Drift guard: lib/pricing.generated.ts is generated from the authoritative
// Python table (src/coder_eval/pricing.py) by scripts/gen-pricing.mjs. Nobody
// types a rate on this side, so the only failure mode left is a STALE artifact —
// pricing.py changed and `pnpm gen:pricing` wasn't run.
//
// Semantics are EXACT-MATCH against pricing.py minus one small, explicit
// exclusion set (see EXCLUDED_MODELS in the generator). The old hand-copied
// mirror couldn't sustain exact-match — it needed a not-mirrored allowlist that
// 17 models drifted past, two of whose entries were models real runs use,
// silently rendering "—" for their cost. Generation makes exact-match free: one
// assertion subsumes orphans, missing entries, and rate drift, and the only
// models left off are the ones a static rate would be actively WRONG for.
//
// This imports readTable, NOT parsePythonTable, and the distinction is the whole
// guard: parsePythonTable on both sides would compare two products of the same
// regex, so a row that regex cannot match is missing from the artifact AND from
// the expectation — deep-equal passes while the board renders "—" for that model
// forever. readTable cross-checks the matched-row count against the number of
// `ModelPricing(` constructions in the file and throws if they disagree.

describe("pricing.generated.ts ↔ pricing.py parity", () => {
    // Throws if the parser skipped a row, so this doubles as the coverage check.
    const py = readTable();

    test("parses a non-trivial Python table", () => {
        // Guard against a regex/path regression silently passing the test.
        expect(Object.keys(py).length).toBeGreaterThan(10);
    });

    test("the generated table is current with pricing.py", () => {
        const generated = Object.fromEntries(
            Object.entries(PRICING).map(([model, p]) => [
                model,
                [
                    p.inputPerMTok,
                    p.outputPerMTok,
                    p.cacheWritePerMTok,
                    p.cacheReadPerMTok,
                ],
            ]),
        );
        expect(
            generated,
            "lib/pricing.generated.ts is stale — run `pnpm gen:pricing`",
        ).toEqual(mirroredTable(py));
    });
});

// The one deliberate gap between the two tables. OpenRouter routes each request
// to whichever provider wins its sort, so no single headline rate is correct;
// the harness captures each call's ACTUAL cost proxy-side and the detail view
// renders it per call (provider_call_costs → ProviderCallTableSection). These
// tests pin that the estimate stays absent, because the failure mode is silent:
// a regenerated table that quietly reintroduces them puts a confidently wrong
// number beside the measured one.
describe("per-request-routed models are deliberately unpriced", () => {
    const excluded = excludedModels();

    test("the exclusion set is non-empty and still live", () => {
        // mirroredTable throws on an id pricing.py no longer prices, so simply
        // calling it here fails the suite on a stale exclusion.
        expect(excluded.length).toBeGreaterThan(0);
        expect(() => mirroredTable(readTable())).not.toThrow();
    });

    test("none of them carry a static rate on the frontend", () => {
        for (const model of excluded) {
            expect(
                Object.hasOwn(PRICING, model),
                `${model} must not be statically priced — it is shown at captured per-call cost`,
            ).toBe(false);
            expect(resolvePricing(model)).toBeNull();
        }
    });

    test("Bedrock open-weight models ARE priced (they are not routed per request)", () => {
        // The nearby-but-opposite case, pinned so a future widening of the
        // exclusion set can't quietly take these with it: same open-weight
        // families, but running at fixed Bedrock rates with no per-call capture,
        // so a static rate is correct and required.
        for (const model of ["deepseek.v3.2", "zai.glm-5", "moonshotai.kimi-k2.5"]) {
            expect(resolvePricing(model), `${model} lost its rate`).not.toBeNull();
        }
    });
});

// Behaviour the generated table has to preserve. Deliberately NOT a second copy
// of the rate card: every expectation below is derived from pricing.py via
// `py[...]`, never a literal. The test above already proves every rate matches,
// so hardcoding one here would mean a legitimate vendor repricing breaks the
// suite even after a correct regeneration — exactly the two-places problem
// generation exists to remove. What these pin is lookup *logic* the deep-equal
// cannot see: the undated fallback, a genuine zero surviving, float precision.
describe("resolvePricing over the generated table", () => {
    const py = readTable();

    test("a deleted dated key still prices via the undated fallback", () => {
        // "claude-opus-4-6-20250514" was a dead entry (absent from pricing.py)
        // and redundant: resolvePricing strips a trailing -YYYYMMDD. Deleting it
        // must change no rendered figure — the dated id still resolves, and to
        // exactly the undated key's rate.
        for (const undated of ["claude-opus-4-6", "claude-sonnet-4-6"]) {
            const dated = resolvePricing(`${undated}-20250514`);
            expect(dated, `${undated}-20250514 no longer resolves`).not.toBeNull();
            expect(dated).toEqual(resolvePricing(undated));
        }
    });

    test("a model the old allowlist suppressed is now priced", () => {
        // By far the most-run model in the recorded run data, yet it sat in the
        // old not-mirrored allowlist — so the evalboard rendered "—" for cost on
        // runs it actually executed. This is the regression that must not return.
        expect(resolvePricing("gpt-5.6-terra")).not.toBeNull();
    });

    test("a legitimately-zero rate survives as 0, not undefined", () => {
        // Bedrock publishes no prompt-cache rate for the open-weight models, so
        // cache-read is a real 0 — generation must not drop it as falsy. Asserted
        // against pricing.py's own value, and separately that it IS zero there,
        // so the test still means something if that rate ever changes.
        for (const model of ["deepseek.v3.2", "zai.glm-5"]) {
            expect(py[model][3]).toBe(0);
            expect(resolvePricing(model)?.cacheReadPerMTok).toBe(py[model][3]);
        }
    });

    test("fractional rates round-trip exactly through generation", () => {
        // The generator emits numbers via string interpolation, so a precision
        // loss would show up as generated !== parsed for these decimals.
        for (const model of ["gpt-5-codex", "codex-mini-latest", "zai.glm-5"]) {
            const p = resolvePricing(model);
            expect(p).not.toBeNull();
            expect([
                p!.inputPerMTok,
                p!.outputPerMTok,
                p!.cacheWritePerMTok,
                p!.cacheReadPerMTok,
            ]).toEqual(py[model]);
            // And that at least one of them really is fractional, so this test
            // cannot pass vacuously if pricing.py switches to round numbers.
            expect(
                py[model].some((r) => !Number.isInteger(r)),
                `${model} has no fractional rate left — pick another model`,
            ).toBe(true);
        }
    });
});
