import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, test } from "vitest";
import { PRICING } from "../pricing";

// Drift guard: lib/pricing.ts is a hand-copied mirror of the authoritative
// Python table in src/coder_eval/pricing.py. It exists so the frontend's
// "estimated" USD figures agree with the backend's authoritative Cost on the
// same tokens.
//
// Semantics are SUBSET, not exact-match: every model priced in lib/pricing.ts
// must exist in pricing.py with identical rates (a frontend rate that disagrees
// with the backend, or prices a model the backend doesn't, fails the build).
// The frontend is NOT required to mirror every backend model — it only needs to
// price the ones it displays, and the backend legitimately prices models the
// evalboard never renders. (Exact-match was too strict: it forced unrelated
// backend model additions into this file to keep the build green.)

const here = dirname(fileURLToPath(import.meta.url));
const PY_PATH = resolve(here, "../../../src/coder_eval/pricing.py");

// Match: "model-id": ModelPricing(1.25, 10.0, 1.25, 0.125),
const ROW_RE =
    /"([^"]+)":\s*ModelPricing\(\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)\s*\)/g;

function parsePythonTable(): Record<
    string,
    [number, number, number, number]
> {
    const src = readFileSync(PY_PATH, "utf8");
    const out: Record<string, [number, number, number, number]> = {};
    for (const m of src.matchAll(ROW_RE)) {
        out[m[1]] = [
            Number(m[2]),
            Number(m[3]),
            Number(m[4]),
            Number(m[5]),
        ];
    }
    return out;
}

describe("pricing.ts ↔ pricing.py parity", () => {
    const py = parsePythonTable();

    test("parses a non-trivial Python table", () => {
        // Guard against a regex/path regression silently passing the test.
        expect(Object.keys(py).length).toBeGreaterThan(10);
    });

    test("every model in lib/pricing.ts exists in pricing.py", () => {
        const orphans = Object.keys(PRICING).filter((m) => !(m in py));
        expect(
            orphans,
            `priced in lib/pricing.ts but absent from pricing.py: ${orphans.join(", ")}`,
        ).toEqual([]);
    });

    test("shared models have identical input/output/cacheWrite/cacheRead rates", () => {
        for (const [model, ts] of Object.entries(PRICING)) {
            const rates = py[model];
            expect(rates, `not priced in pricing.py: ${model}`).toBeDefined();
            expect([
                ts.inputPerMTok,
                ts.outputPerMTok,
                ts.cacheWritePerMTok,
                ts.cacheReadPerMTok,
            ]).toEqual(rates);
        }
    });

    // Python-priced models we deliberately do NOT mirror to the frontend: heavy
    // frontier Claude/GPT variants the evalboard never runs, so pricing them here
    // adds nothing. Kept explicit (not a blanket "ignore extras") so a NEW model
    // added to pricing.py that ISN'T here and ISN'T in PRICING breaks the build —
    // catching a real litellm-relevant omission (e.g. the Bedrock open-weight ids
    // that previously rendered "—" for cost).
    const DELIBERATELY_UNMIRRORED = new Set([
        "claude-sonnet-5",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5.4-pro",
        "gpt-5.5-pro",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ]);

    test("every pricing.py model is mirrored in pricing.ts or explicitly unmirrored", () => {
        const missing = Object.keys(py).filter((m) => !(m in PRICING) && !DELIBERATELY_UNMIRRORED.has(m));
        expect(
            missing,
            `priced in pricing.py but missing from pricing.ts — mirror it or add to DELIBERATELY_UNMIRRORED: ${missing.join(", ")}`,
        ).toEqual([]);
    });
});
