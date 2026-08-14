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

    test("parses every ModelPricing row in the Python table", () => {
        // A "> 10" floor is not enough: the guard NARROWS silently if the regex
        // stops matching some rows (a `ruff format` reflow onto several lines, a
        // switch to keyword args), and a narrowed guard stops reporting exactly
        // the class of omission this file exists to catch. Count the constructor
        // calls in the source and require the parse to have found all of them.
        const declared = (
            readFileSync(PY_PATH, "utf8").match(/^\s*"[^"]+":\s*ModelPricing\(/gm) ?? []
        ).length;
        expect(declared).toBeGreaterThan(10);
        expect(
            Object.keys(py).length,
            "ROW_RE missed a pricing.py row — the parity guard is narrower than it looks",
        ).toBe(declared);
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
    // frontier variants no harness runs, so pricing them here adds nothing. Kept
    // explicit (not a blanket "ignore extras") so a NEW model added to pricing.py
    // that ISN'T here and ISN'T in PRICING breaks the build — catching a real
    // litellm-relevant omission (e.g. the Bedrock open-weight ids that previously
    // rendered "—" for cost).
    //
    // KEEP THIS SET HONEST. It silences the drift guard, so a stale entry hides a
    // live bug rather than a non-issue: `claude-sonnet-5`, `gpt-5.6-sol`,
    // `gpt-5.6-terra` and `gpt-5.6-luna` sat here under "the evalboard never runs
    // them" while appearing ~32k / ~2k / ~17k / ~2k times in `runs-remote/`, so
    // every one of those runs rendered "—" for cost with nothing failing. Before
    // adding an id, grep the corpus for it — absence from run data is the ONLY
    // justification, and it expires the moment a harness adopts the model.
    const DELIBERATELY_UNMIRRORED = new Set([
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5.4-pro",
        "gpt-5.5-pro",
        // OpenRouter open-weight models: priced in pricing.py only for the Python
        // max_usd static fallback. The evalboard deliberately does NOT statically
        // price them — OpenRouter routes per-request, so it shows the captured
        // ACTUAL per-call cost instead (see the per-call table, provider_call_costs).
        // Same reasoning under the OpenCode harness, which addresses OpenRouter
        // natively: it reports the provider's own per-step cost, which the turn
        // carries as token_usage.total_cost_usd.
        "moonshotai/kimi-k3",
        "z-ai/glm-5.2",
        "deepseek/deepseek-v4-pro",
    ]);

    test("every DELIBERATELY_UNMIRRORED id still exists in pricing.py", () => {
        // Stale-membership guard. An exemption silences the drift guard for one
        // id forever; once the id leaves pricing.py the entry silences nothing
        // and only survives to be copied. Making that a build failure is what
        // forces the set to be re-read rather than appended to.
        const stale = [...DELIBERATELY_UNMIRRORED].filter((m) => !(m in py));
        expect(
            stale,
            `exempted from the mirror but no longer priced in pricing.py — drop them from DELIBERATELY_UNMIRRORED: ${stale.join(", ")}`,
        ).toEqual([]);
    });

    test("every pricing.py model is mirrored in pricing.ts or explicitly unmirrored", () => {
        const missing = Object.keys(py).filter((m) => !(m in PRICING) && !DELIBERATELY_UNMIRRORED.has(m));
        expect(
            missing,
            `priced in pricing.py but missing from pricing.ts — mirror it or add to DELIBERATELY_UNMIRRORED: ${missing.join(", ")}`,
        ).toEqual([]);
    });
});
